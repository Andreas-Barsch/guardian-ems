"""Unactivated real config/position log adapters and selective dependency jobs.

Consumes confirmed RawRegistry line blocks only. Explicit registry/log mapping,
no product paths, discovery, writer activation or diagnosis implementation.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import json
import math
import sqlite3

from config_history import CONFIG_HISTORY_SCHEMA_VERSION, diagnostic_parameters, config_id
from position_history import PositionSnapshot
from raw_registry import ZERO,MAX_BLOCK,encoded,hashed,file_signature,RegistryConflict,RegistryCorrupt
from cell_window_adapter import utc_seconds


class EpochBudgetExceeded(RuntimeError):pass


def decode_epoch(kind,record):
    """Existing formats/tie ordering; no new effective-time default."""
    if kind=='config':
        if record.get('schema_version')!=CONFIG_HISTORY_SCHEMA_VERSION or not isinstance(record.get('parameters'),dict):
            raise ValueError('invalid config history record')
        stamp=utc_seconds(datetime.fromisoformat(str(record['timestamp']).replace('Z','+00:00')))
        identity=record['config_id']
        if not isinstance(identity,str):raise ValueError('invalid config ID')
        canonical=dict(record,timestamp=datetime.fromtimestamp(stamp,timezone.utc).isoformat())
        return stamp,identity,identity,canonical
    if kind=='position':
        snapshot=PositionSnapshot.from_dict(record);canonical=snapshot.to_dict()
        return utc_seconds(snapshot.effective_at),snapshot.created_at+'|'+snapshot.position_history_id,snapshot.position_history_id,canonical
    raise ValueError('unknown log kind')


SCHEMA='''
CREATE TABLE epoch_schema(version INTEGER PRIMARY KEY);
INSERT INTO epoch_schema VALUES(1);
CREATE TABLE log_cursor(registry TEXT PRIMARY KEY,mapping TEXT,seq INTEGER,offset INTEGER,previous_hash TEXT);
CREATE TABLE log_source(registry TEXT,source TEXT,revision TEXT,receipt TEXT,block_chain TEXT,frontier INTEGER,PRIMARY KEY(registry,source));
CREATE TABLE epoch(kind TEXT,stamp REAL,priority TEXT,identity TEXT,payload TEXT,hash TEXT,PRIMARY KEY(kind,stamp,priority));
CREATE INDEX epoch_time ON epoch(kind,stamp,priority);
CREATE UNIQUE INDEX position_identity ON epoch(identity) WHERE kind='position';
CREATE TABLE provenance(registry TEXT,seq INTEGER,offset INTEGER,kind TEXT,stamp REAL,priority TEXT,raw_hash TEXT,PRIMARY KEY(registry,seq,offset));
CREATE TABLE current_config(id INTEGER PRIMARY KEY CHECK(id=1),fingerprint TEXT,payload TEXT);
CREATE TABLE dependency(id INTEGER PRIMARY KEY AUTOINCREMENT,product TEXT,kind TEXT,start REAL,end REAL,context TEXT,fingerprint TEXT,UNIQUE(product,kind,start,end,context));
CREATE TABLE dependency_day(kind TEXT,day INTEGER,dependency INTEGER REFERENCES dependency,PRIMARY KEY(kind,day,dependency));
CREATE TABLE change(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,start REAL,end REAL,max_dependency INTEGER,day INTEGER,after_dependency INTEGER DEFAULT 0,finished INTEGER DEFAULT 0);
CREATE TABLE job(change INTEGER REFERENCES change,dependency INTEGER REFERENCES dependency,start REAL,end REAL,status TEXT DEFAULT 'pending',PRIMARY KEY(change,dependency));
'''


class EpochStore:
    def __init__(self,path):
        self.path=Path(path)
        with self.connection() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if not tables:
                db.execute('PRAGMA journal_mode=WAL');db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
            elif 'epoch_schema' not in tables or [r[0] for r in db.execute('SELECT version FROM epoch_schema')]!=[1]:
                raise ValueError('unsupported epoch database')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,isolation_level=None,timeout=1);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA synchronous=FULL')
        steps=0
        def budget():
            nonlocal steps
            steps+=100
            return int(steps>=100000)
        db.set_progress_handler(budget,100)
        try:yield db
        except sqlite3.OperationalError as exc:
            if steps>=100000:raise EpochBudgetExceeded('epoch SQL budget exhausted; no fallback') from exc
            raise
        finally:db.close()

    @contextmanager
    def write(self):
        from work_scheduler import atomic
        with atomic(), self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:yield db;db.execute('COMMIT')
            except BaseException:
                db.set_progress_handler(None,0)
                if db.in_transaction:db.execute('ROLLBACK')
                raise

    @staticmethod
    def _at(db,kind,stamp):
        return db.execute('SELECT * FROM epoch WHERE kind=? AND stamp<=? ORDER BY stamp DESC,priority DESC LIMIT 1',(kind,stamp)).fetchone()

    def at(self,kind,stamp):
        if kind not in ('config','position'):raise ValueError('invalid epoch kind')
        with self.connection() as db:
            row=self._at(db,kind,utc_seconds(stamp))
            return None if row is None else json.loads(row['payload'])

    def position_at(self,position,stamp):
        if str(position) not in ('1','2','3','4','5','6'):raise ValueError('invalid stack position')
        row=self.at('position',stamp)
        return (None,None) if row is None else (row['positions'][str(position)],row['position_history_id'])

    @staticmethod
    def _change(db,kind,start,end):
        maximum=db.execute('SELECT COALESCE(MAX(id),0) FROM dependency').fetchone()[0]
        db.execute('INSERT INTO change(kind,start,end,max_dependency,day) VALUES(?,?,?,?,?)',
                   (kind,start,end,maximum,math.floor(start/86400) if start is not None else
                    db.execute('SELECT COALESCE(MIN(day),0) FROM dependency_day WHERE kind=?',(kind,)).fetchone()[0]))

    def _ingest(self,db,kind,record,registry,seq,offset,raw_hash):
        stamp,priority,identity,canonical=decode_epoch(kind,record)
        payload=encoded(canonical).decode();fingerprint=hashed(payload.encode())
        if kind=='position' and db.execute("SELECT 1 FROM epoch WHERE kind='position' AND identity=?",(identity,)).fetchone():
            raise RegistryConflict('duplicate position history ID')
        old=db.execute('SELECT hash FROM epoch WHERE kind=? AND stamp=? AND priority=?',(kind,stamp,priority)).fetchone()
        if old and old['hash']!=fingerprint:raise RegistryConflict('ambiguous same-time epoch identity; preserve both logs, no invented winner')
        if not old:
            before=self._at(db,kind,stamp)
            db.execute('INSERT INTO epoch VALUES(?,?,?,?,?,?)',(kind,stamp,priority,identity,payload,fingerprint))
            after=self._at(db,kind,stamp)
            if kind=='position' or before is None or before['hash']!=after['hash']:
                successor=db.execute('SELECT MIN(stamp) FROM epoch WHERE kind=? AND stamp>?',(kind,stamp)).fetchone()[0]
                self._change(db,kind,stamp,successor)
        db.execute('INSERT INTO provenance VALUES(?,?,?,?,?,?,?)',(registry,seq,offset,kind,stamp,priority,raw_hash))

    @staticmethod
    def _position_trace(db,start,end):
        # IdentityEpochResolver consumes ordered occupancy transitions, including
        # same-effective-time intermediate changes. Winner-only fingerprints lose
        # these inputs even though position_at itself still returns the winner.
        prior=db.execute("SELECT MAX(stamp) FROM epoch WHERE kind='position' AND stamp<=?",(start,)).fetchone()[0]
        rows=db.execute("SELECT * FROM epoch WHERE kind='position' AND stamp>=? AND stamp<? ORDER BY stamp,priority LIMIT 257",
                        (start if prior is None else prior,end)).fetchall()
        if len(rows)>256:raise EpochBudgetExceeded('position trace exceeds transition budget')
        return rows

    def _vector(self,db,kind,start,end):
        if kind=='position':
            return [(start,end,[(r['stamp'],r['priority'],r['hash']) for r in self._position_trace(db,start,end)])]
        if kind=='current_config':
            row=db.execute('SELECT fingerprint FROM current_config WHERE id=1').fetchone()
            return [(start,end,None if row is None else row[0])]
        if kind not in ('config','position'):raise ValueError('invalid dependency kind')
        prior=self._at(db,kind,start)
        rows=db.execute('SELECT * FROM epoch WHERE kind=? AND stamp>? AND stamp<? ORDER BY stamp,priority LIMIT 257',(kind,start,end)).fetchall()
        if len(rows)>256:raise EpochBudgetExceeded('epoch window has more than 256 transitions/ties')
        effective={r['stamp']:r for r in rows};points=[(start,prior),*effective.items()]
        return [(t,points[i+1][0] if i+1<len(points) else end,None if r is None else r['hash']) for i,(t,r) in enumerate(points)]

    def fingerprint(self,kind,start,end):
        start,end=self._window(start,end)
        with self.connection() as db:
            db.execute('BEGIN')
            return hashed(encoded(self._vector(db,kind,start,end)))

    @staticmethod
    def _window(start,end):
        start=utc_seconds(start);end=utc_seconds(end)
        if not 0<end-start<=32*86400:raise ValueError('dependency window must be positive and <=32 days')
        return start,end

    def register_dependency(self,product,kind,start,end,*,context,expected_fingerprint=None):
        """Actual consumed interval/product identity supplied explicitly, not a whole-log signature."""
        start,end=self._window(start,end);context=encoded(context).decode()
        if not isinstance(product,str) or not product or len(product)>512 or len(context)>4096:raise ValueError('bounded dependency identity required')
        with self.write() as db:
            fingerprint=hashed(encoded(self._vector(db,kind,start,end)))
            if expected_fingerprint is not None and fingerprint!=expected_fingerprint:
                raise RegistryConflict('consumed epoch vector is stale; explicit retry required')
            old=db.execute('SELECT * FROM dependency WHERE product=? AND kind=? AND start=? AND end=? AND context=?',(product,kind,start,end,context)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint:raise RegistryConflict('immutable dependency changed; use a new publication identity')
                return old['id']
            row=db.execute('INSERT INTO dependency(product,kind,start,end,context,fingerprint) VALUES(?,?,?,?,?,?)',(product,kind,start,end,context,fingerprint))
            for day in range(math.floor(start/86400),math.floor(math.nextafter(end,-math.inf)/86400)+1):
                db.execute('INSERT INTO dependency_day VALUES(?,?,?)',(kind,day,row.lastrowid))
            return row.lastrowid

    def capture_window(self,kind,start,end):
        """Return bounded real epoch records and the exact consumed fingerprint."""
        start,end=self._window(start,end)
        if kind not in ('config','position'):raise ValueError('invalid captured epoch kind')
        with self.connection() as db:
            db.execute('BEGIN')
            vector=self._vector(db,kind,start,end)
            if kind=='position':
                return dict(kind=kind,start=start,end=end,fingerprint=hashed(encoded(vector)),
                            records=[json.loads(r['payload']) for r in self._position_trace(db,start,end)])
            before=self._at(db,kind,start)
            rows=db.execute('SELECT * FROM epoch WHERE kind=? AND stamp>? AND stamp<? ORDER BY stamp,priority LIMIT 257',
                            (kind,start,end)).fetchall()
            if len(rows)>256:raise EpochBudgetExceeded('epoch capture exceeds transition budget')
            return dict(kind=kind,start=start,end=end,fingerprint=hashed(encoded(vector)),
                        records=[json.loads(r['payload']) for r in ([before] if before else [])+list(rows)])

    @contextmanager
    def publication_guard(self,dependencies):
        """Hold epoch writes only across a bounded publication transaction.

        Dependencies must be durably registered BEFORE this guard. Thus a crash
        can leave a harmless unused dependency, never a published result without
        one. RawRegistry writers use a different lock/database and remain free.
        """
        if not 1<=len(dependencies)<=16:raise ValueError('bounded dependency set required')
        with self.write() as db:
            for dep in dependencies:
                row=db.execute('SELECT * FROM dependency WHERE id=?',(dep,)).fetchone()
                if row is None:raise RegistryConflict('publication dependency missing')
                current=hashed(encoded(self._vector(db,row['kind'],row['start'],row['end'])))
                if current!=row['fingerprint']:raise RegistryConflict('consumed epoch vector is stale; explicit retry required')
            yield

    def set_current_options(self,options,*,expected_fingerprint):
        """Explicit Current-Condition options; historical config append cannot call this."""
        parameters=diagnostic_parameters(options);fingerprint=config_id(parameters)
        with self.write() as db:
            old=db.execute('SELECT fingerprint FROM current_config WHERE id=1').fetchone()
            if (None if old is None else old[0])!=expected_fingerprint:raise RegistryConflict('current options changed')
            if old and old[0]==fingerprint:return fingerprint
            db.execute('INSERT OR REPLACE INTO current_config VALUES(1,?,?)',(fingerprint,encoded(parameters).decode()))
            self._change(db,'current_config',None,None)
        return fingerprint

    def changes(self,*,after=0,limit=64):
        if not 1<=limit<=256:raise ValueError('invalid change page')
        with self.connection() as db:return [dict(r) for r in db.execute('SELECT * FROM change WHERE id>? ORDER BY id LIMIT ?',(after,limit))]

    def plan_jobs(self,change,*,expected_cursor,limit=64,fault=lambda _:None):
        if not 1<=limit<=128:raise ValueError('invalid dependency page')
        with self.write() as db:
            c=db.execute('SELECT * FROM change WHERE id=?',(change,)).fetchone()
            if c is None or (c['day'],c['after_dependency'])!=expected_cursor:raise RegistryConflict('job planning cursor changed')
            if c['finished']:return dict(cursor=expected_cursor,finished=True,rows=0)
            lastday=math.floor(math.nextafter(c['end'],-math.inf)/86400) if c['end'] is not None else db.execute(
                'SELECT COALESCE(MAX(day),?) FROM dependency_day WHERE kind=?',(c['day'],c['kind'])).fetchone()[0]
            rows=db.execute('SELECT x.day,d.* FROM dependency_day x JOIN dependency d ON d.id=x.dependency '
                'WHERE x.kind=? AND (x.day,x.dependency)>(?,?) AND x.day<=? ORDER BY x.day,x.dependency LIMIT ?',
                (c['kind'],c['day'],c['after_dependency'],lastday,limit)).fetchall()
            for r in rows:
                if r['id']>c['max_dependency']:continue
                start=max(r['start'],c['start']) if c['start'] is not None else r['start']
                end=min(r['end'],c['end']) if c['end'] is not None else r['end']
                if start<end:db.execute('INSERT OR IGNORE INTO job(change,dependency,start,end) VALUES(?,?,?,?)',(change,r['id'],start,end))
            fault('after_jobs')
            cursor=(rows[-1]['day'],rows[-1]['id']) if rows else expected_cursor
            db.execute('UPDATE change SET day=?,after_dependency=?,finished=? WHERE id=?',(*cursor,int(len(rows)<limit),change))
            fault('after_job_cursor')
        fault('after_job_commit')
        return dict(cursor=cursor,finished=len(rows)<limit,rows=len(rows))

    def jobs(self,*,change,after=0,limit=64):
        if not 1<=limit<=256:raise ValueError('invalid job page')
        with self.connection() as db:return [dict(r) for r in db.execute('SELECT * FROM job WHERE change=? AND dependency>? ORDER BY dependency LIMIT ?',(change,after,limit))]


class EpochLogConsumer:
    """Registry-confirmed real config_history/position_history bytes, bounded resume."""
    def __init__(self,registry,store):self.registry=registry;self.store=store;self.identity=registry.config['registry_id']

    def initialize(self,mapping):
        if not mapping or len(mapping)>16 or any(v not in ('config','position',None) for v in mapping.values()):raise ValueError('explicit log source mapping required')
        text=encoded(mapping).decode()
        with self.store.write() as db:
            old=db.execute('SELECT mapping FROM log_cursor WHERE registry=?',(self.identity,)).fetchone()
            if old and old[0]!=text:raise RegistryConflict('log mapping immutable')
            db.execute('INSERT OR IGNORE INTO log_cursor VALUES(?,?,1,0,?)',(self.identity,text,ZERO))

    def cursor(self):
        with self.store.connection() as db:
            r=db.execute('SELECT seq,offset,previous_hash FROM log_cursor WHERE registry=?',(self.identity,)).fetchone()
            if r is None:raise RegistryConflict('log consumer uninitialized')
            return tuple(r)

    def step(self,*,expected_cursor,max_records=256,fault=lambda _:None):
        if not 1<=max_records<=256:raise ValueError('invalid log decode budget')
        if self.cursor()!=expected_cursor:raise RegistryConflict('log cursor changed')
        seq,offset,previous=expected_cursor
        page=self.registry.read_page(next_seq=seq,previous_hash=previous,max_records=1)
        if not page['records']:return dict(cursor=expected_cursor,caught_up=True,decodes=0,raw_bytes=0)
        r=page['records'][0];sig=file_signature(r['path'])
        if sig[:2]!=r['file_id'] or sig[2]<r['end']:raise RegistryConflict('log source changed')
        raw=self.registry._raw_range(r['path'],r['start'],r['end'])
        if hashed(raw)!=r['block_hash'] or r['block_chain']!=hashed(encoded([r['block_previous'],r['start'],r['end'],r['block_hash']])):
            raise RegistryCorrupt('log block proof mismatch')
        if not 0<=offset<=len(raw) or (offset and raw[offset-1:offset]!=b'\n') or (raw and not raw.endswith(b'\n')):
            raise RegistryCorrupt('log cursor is not a complete-line boundary')
        if raw.count(b'\n')!=r['lines']:raise RegistryCorrupt('log line count differs')
        lines=[part+b'\n' for part in raw[offset:].split(b'\n')[:-1]][:max_records]
        with self.store.connection() as db:
            mapping=json.loads(db.execute('SELECT mapping FROM log_cursor WHERE registry=?',(self.identity,)).fetchone()[0])
        if r['source'] not in mapping:raise RegistryConflict('unmapped log source; no implicit skipping')
        parsed=[(line,json.loads(line) if mapping[r['source']] is not None else None) for line in lines]
        end=offset+sum(len(line) for line in lines);done=end==len(raw)
        new_cursor=(seq+1,0,hashed(encoded(r))) if done else (seq,end,previous)
        with self.store.write() as db:
            row=db.execute('SELECT * FROM log_cursor WHERE registry=?',(self.identity,)).fetchone()
            if (row['seq'],row['offset'],row['previous_hash'])!=expected_cursor:raise RegistryConflict('log cursor changed')
            mapping=json.loads(row['mapping'])
            if r['source'] not in mapping:raise RegistryConflict('unmapped log source; no implicit skipping')
            prior=db.execute('SELECT * FROM log_source WHERE registry=? AND source=?',(self.identity,r['source'])).fetchone()
            if (None if prior is None else prior['receipt'])!=r['source_previous']:raise RegistryConflict('log source predecessor mismatch')
            if r['mode'] in ('register','replace'):
                if r['mode']=='replace' and mapping[r['source']] is not None:
                    raise RegistryConflict('epoch source replacement requires explicit correction')
                if raw or offset or r['block_previous']!=ZERO:raise RegistryCorrupt('invalid log registration')
            elif r['mode'] not in ('append','adopt') or prior is None or prior['revision']!=r['revision'] or prior['frontier']!=r['start'] or prior['block_chain']!=r['block_previous']:
                raise RegistryConflict('log source chain mismatch')
            if file_signature(r['path'])!=sig:raise RegistryConflict('log changed during proof')
            pos=offset
            for line,value in parsed:
                if mapping[r['source']] is not None:
                    self.store._ingest(db,mapping[r['source']],value,self.identity,seq,r['start']+pos,hashed(line))
                pos+=len(line)
            fault('after_epochs')
            if done:db.execute('INSERT OR REPLACE INTO log_source VALUES(?,?,?,?,?,?)',(self.identity,r['source'],r['revision'],hashed(encoded(r)),r['block_chain'],r['end']))
            db.execute('UPDATE log_cursor SET seq=?,offset=?,previous_hash=? WHERE registry=?',(*new_cursor,self.identity))
            fault('after_log_cursor')
        fault('after_log_commit')
        return dict(cursor=new_cursor,caught_up=done and page['caught_up'],decodes=len(parsed),raw_bytes=len(raw))
