"""Isolated derived source catalog. No discovery, runtime paths or writer calls.

Registration/append receipts are supplied explicitly by a future registry adapter.
They are a trust boundary, not a proof of fsync or absence of foreign rewrites.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import math
import sqlite3

from projection_repository import Conflict, Unavailable

MAX_PAGE = 256
MAX_BYTES = 65536


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def signature(path):
    s = Path(path).stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)


# Additive, explicit inventory-ingest capability within the unreleased schema2.
# Existing databases get this table only on explicit observation ingestion, not
# on a normal constructor/read. Existing source/receipt/index rows are untouched.
OBSERVATION_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS source_observation(inventory TEXT,entry_id INTEGER,"
    "source TEXT,observed_revision TEXT,catalog_revision TEXT,receipt TEXT,"
    "frontier INTEGER,index_cursor INTEGER,outcome TEXT,observation_hash TEXT,"
    "PRIMARY KEY(inventory,entry_id),FOREIGN KEY(inventory) REFERENCES inventory,"
    "FOREIGN KEY(entry_id) REFERENCES inventory_entry,"
    "FOREIGN KEY(source,catalog_revision,receipt) REFERENCES receipt)",
    "CREATE TRIGGER IF NOT EXISTS observation_immutable BEFORE UPDATE ON source_observation "
    "BEGIN SELECT RAISE(ABORT,'inventory observation immutable'); END"
)


SCHEMA = """
CREATE TABLE catalog_schema(version INTEGER PRIMARY KEY CHECK(version=2));
INSERT INTO catalog_schema VALUES(2);
CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL,
 sources INTEGER NOT NULL, revisions INTEGER NOT NULL, helpers INTEGER NOT NULL,
 pending INTEGER NOT NULL, errors INTEGER NOT NULL);
INSERT INTO state VALUES(1,0,0,0,0,0,0);
CREATE TABLE source(source TEXT, revision TEXT, path TEXT NOT NULL, namespace TEXT NOT NULL,
 writer_epoch TEXT NOT NULL, receipt TEXT NOT NULL, frontier INTEGER NOT NULL,
 dev INTEGER NOT NULL, ino INTEGER NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL, ctime INTEGER NOT NULL,
 cursor INTEGER NOT NULL DEFAULT 0, records INTEGER NOT NULL DEFAULT 0,
 errors INTEGER NOT NULL DEFAULT 0, context TEXT NOT NULL,
 PRIMARY KEY(source,revision));
CREATE TABLE active(source TEXT PRIMARY KEY, revision TEXT NOT NULL,
 FOREIGN KEY(source,revision) REFERENCES source);
CREATE TABLE entry(source TEXT,revision TEXT,offset INTEGER,end_offset INTEGER NOT NULL,
 day INTEGER NOT NULL, stamp REAL NOT NULL,module INTEGER,identity TEXT,hash TEXT NOT NULL,
 PRIMARY KEY(source,revision,offset),FOREIGN KEY(source,revision) REFERENCES source);
CREATE INDEX entry_window ON entry(day,stamp,source,revision,offset);
CREATE INDEX entry_identity_stamp ON entry(identity,stamp,source,revision,offset);
CREATE TABLE rs485_restore(adr INTEGER,source TEXT,revision TEXT,path TEXT,offset INTEGER,identity TEXT,
 PRIMARY KEY(adr,source));
CREATE INDEX rs485_restore_latest ON rs485_restore(adr,path DESC,offset DESC);
CREATE INDEX rs485_restore_source ON rs485_restore(source);
CREATE TABLE issue(source TEXT,revision TEXT,offset INTEGER,end_offset INTEGER NOT NULL,
 reason TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(source,revision,offset),
 FOREIGN KEY(source,revision) REFERENCES source);
CREATE TABLE migration(job TEXT PRIMARY KEY,cursor INTEGER NOT NULL, finished INTEGER NOT NULL DEFAULT 0);
CREATE TABLE helper(job TEXT,path TEXT,PRIMARY KEY(job,path));
CREATE TABLE receipt(source TEXT,revision TEXT,receipt TEXT,seq INTEGER NOT NULL,parent TEXT,
 frontier INTEGER NOT NULL,chain_hash TEXT NOT NULL,
 PRIMARY KEY(source,revision,receipt),UNIQUE(source,revision,seq),
 FOREIGN KEY(source,revision) REFERENCES source);
CREATE TRIGGER receipt_immutable BEFORE UPDATE ON receipt BEGIN SELECT RAISE(ABORT,'receipt immutable'); END;
CREATE TABLE window_snapshot(id TEXT PRIMARY KEY,start REAL,end REAL,modules TEXT,
 generation INTEGER,pending INTEGER,errors INTEGER);
CREATE TABLE window_source(snapshot TEXT REFERENCES window_snapshot ON DELETE CASCADE,
 source TEXT,revision TEXT,receipt TEXT,prefix_end INTEGER NOT NULL,
 PRIMARY KEY(snapshot,source),FOREIGN KEY(source,revision,receipt) REFERENCES receipt);
CREATE TABLE inventory(id TEXT PRIMARY KEY,status TEXT NOT NULL,current_root INTEGER NOT NULL DEFAULT 0,
 examined INTEGER NOT NULL DEFAULT 0,sources INTEGER NOT NULL DEFAULT 0,
 helpers INTEGER NOT NULL DEFAULT 0,unknown INTEGER NOT NULL DEFAULT 0);
CREATE TABLE inventory_root(inventory TEXT REFERENCES inventory,ordinal INTEGER,path TEXT,namespace TEXT,
 dev INTEGER,ino INTEGER,mtime INTEGER,PRIMARY KEY(inventory,ordinal));
CREATE TABLE inventory_entry(id INTEGER PRIMARY KEY AUTOINCREMENT,inventory TEXT REFERENCES inventory,
 root INTEGER,name TEXT,kind TEXT,item TEXT,UNIQUE(inventory,root,name));
CREATE INDEX inventory_page ON inventory_entry(inventory,id);
"""


def indexed_coordinates(value, context):
    """Existing source formats, explicitly declared by local preparation.

    No inference from filename; original bytes, hashes and identities unchanged.
    Cell is the existing default. No writer is selected by this decoder.
    """
    kind = context.get('record_format', 'cell')
    if kind in ('daily_rs485', 'daily_position', 'daily_config', 'daily_maintenance'):
        # Daily uses the original complete records (including RS485 state
        # changes and every maintenance revision), not History's frame DTOs.
        from daily_diagnostics import _timestamp
        field = {'daily_position': 'effective_at', 'daily_maintenance': 'occurred_at'}.get(kind, 'timestamp')
        stamp = _timestamp(value[field])
        if not math.isfinite(stamp):
            raise ValueError('invalid daily input timestamp')
        return stamp, None, json.dumps({'source_format': kind})
    if kind in ('hycube_system_observation', 'hycube_history_projection'):
        from datetime import datetime
        if value.get('schema_version') != 1 or value.get('record_type') != kind:
            raise ValueError('Hycube source contract mismatch')
        parsed = datetime.fromisoformat(value['received_at'].replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError('Hycube timestamp requires timezone')
        if kind == 'hycube_history_projection':
            from hycube_projection import parse_projection_record
            _, stamp, _ = parse_projection_record(value)
        else:
            parsed = datetime.fromisoformat(value['received_at'].replace('Z', '+00:00'))
            if parsed.tzinfo is None: raise ValueError('Hycube timestamp requires timezone')
            stamp = parsed.timestamp()
        if not math.isfinite(stamp): raise ValueError('invalid Hycube timestamp')
        return float(stamp), None, json.dumps({'source_format': kind},sort_keys=True)
    if kind == 'hycube_policy_observation':
        from datetime import datetime
        if value.get('schema_version') != 1 or value.get('record_type') != kind:
            raise ValueError('Hycube policy source contract mismatch')
        parsed = datetime.fromisoformat(value['observed_at'].replace('Z','+00:00'))
        if parsed.tzinfo is None or not math.isfinite(parsed.timestamp()):
            raise ValueError('Hycube policy timestamp requires timezone')
        return parsed.timestamp(), None, 'hycube_policy_valid' if value.get('parse_quality') == 'complete' else 'hycube_policy_invalid'
    if kind == 'rs485':
        from datetime import datetime
        if value.get('record_type') != 'frame': raise ValueError('RS485 frame required')
        parsed=datetime.fromisoformat(value['timestamp'].replace('Z','+00:00'))
        if parsed.tzinfo is None: raise ValueError('RS485 timestamp requires timezone')
        stamp=parsed.timestamp()
        if not math.isfinite(stamp): raise ValueError('invalid RS485 timestamp')
        return stamp,None,json.dumps({'source_format':'rs485'})
    if kind != 'cell': raise ValueError('unsupported declared source format')
    stamp=value['timestamp']
    if value.get('schema_version')!=1 or isinstance(stamp,bool) or not math.isfinite(float(stamp)):
        raise ValueError('invalid schema/timestamp')
    module=value.get('module');module=module if isinstance(module,int) and not isinstance(module,bool) else None
    identity=json.dumps({'module_serial':value.get('module_serial'),'identity_epoch':value.get('identity_epoch'),
                         'position_epoch':value.get('position_epoch')},sort_keys=True)
    return float(stamp),module,identity


class SourceCatalog:
    def __init__(self, path):
        self.path = Path(path)
        with self.connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if 'catalog_schema' not in tables or [r[0] for r in db.execute('SELECT version FROM catalog_schema')] != [2]:
                    raise ValueError('unsupported catalog; no migration attempted')
                if 'ctime' not in {r[1] for r in db.execute('PRAGMA table_info(source)')}:
                    raise Unavailable('catalog change tokens require explicit preparation')
            else:
                db.execute('PRAGMA journal_mode=WAL')
                db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + ';\n'.join(OBSERVATION_SCHEMA) + ';\nCOMMIT;')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        from work_scheduler import atomic
        with atomic(), self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    @staticmethod
    def _source(db, source, revision):
        row = db.execute('SELECT * FROM source WHERE source=? AND revision=?', (source,revision)).fetchone()
        if row is None:
            raise Unavailable('source revision unregistered')
        return row

    @staticmethod
    def _verify(row):
        observed = signature(row['path'])
        if observed != (row['dev'],row['ino'],row['size'],row['mtime']):
            raise Conflict('source metadata changed; explicit append receipt or new revision required')

    def _register(self, db, item):
        required = ('source','revision','path','namespace','writer_epoch','receipt','frontier')
        if any(k not in item for k in required) or item['namespace'] not in ('legacy-local','utc'):
            raise ValueError('explicit source registration required')
        for key in ('source','revision','writer_epoch','receipt'):
            if not isinstance(item[key],str) or not item[key] or len(item[key])>512:
                raise ValueError('nonempty bounded source identifiers required')
        frontier = item['frontier']
        if not isinstance(frontier,int) or isinstance(frontier,bool) or frontier < 0:
            raise ValueError('invalid confirmed frontier')
        path = Path(item['path'])
        if not path.is_absolute():
            raise ValueError('absolute explicit source path required')
        sig = signature(path)
        if 'observed_signature' in item and tuple(item['observed_signature']) != sig:
            raise Conflict('inventory observation changed; new explicit registration required')
        if frontier > sig[2]:
            raise Conflict('frontier beyond file')
        context = json.dumps(item.get('context',{}),sort_keys=True,allow_nan=False)
        if len(context.encode()) > 4096:
            raise ValueError('source context too large')
        values = (item['source'],item['revision'],str(path),item['namespace'],item['writer_epoch'],
                  item['receipt'],frontier,*sig,path.stat().st_ctime_ns,context)
        old = db.execute('SELECT * FROM source WHERE source=? AND revision=?', values[:2]).fetchone()
        if old:
            keys = ('source','revision','path','namespace','writer_epoch','receipt','frontier','dev','ino','size','mtime','ctime','context')
            if tuple(old[k] for k in keys) != values:
                raise Conflict('registered revision differs')
            return
        active = db.execute('SELECT revision FROM active WHERE source=?',(item['source'],)).fetchone()
        if (None if active is None else active[0]) != item.get('expected_revision'):
            raise Conflict('revision activation compare-and-swap failed')
        prior_pending = 0
        if active:
            prior = self._source(db,item['source'],active[0])
            prior_pending = int(prior['cursor'] < prior['frontier'])
        db.execute('INSERT INTO source(source,revision,path,namespace,writer_epoch,receipt,frontier,dev,ino,size,mtime,ctime,context) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
        chain = digest(json.dumps(values,sort_keys=True).encode())
        db.execute('INSERT INTO receipt VALUES(?,?,?,?,?,?,?)',
                   (item['source'],item['revision'],item['receipt'],0,None,frontier,chain))
        # Each source stores at most one direct identity per 8-bit ADR.
        # A revision replacement removes only that source's derived identities;
        # older active sources remain available through the ordered index.
        if item.get('context',{}).get('record_format')=='rs485':
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='rs485_restore'").fetchone() is None:
                raise Unavailable('RS485 restart index requires explicit preparation')
            db.execute('DELETE FROM rs485_restore WHERE source=?',(item['source'],))
        db.execute('INSERT INTO active VALUES(?,?) ON CONFLICT DO UPDATE SET revision=excluded.revision',values[:2])
        db.execute('UPDATE state SET generation=generation+1,sources=sources+?,revisions=revisions+1,pending=pending+? WHERE id=1',
                   (int(active is None),int(frontier>0)-prior_pending))

    def register(self, **item):
        with self.write() as db:
            self._register(db,item)

    def confirm_append(self, source, revision, *, expected_receipt, receipt, frontier, writer_epoch):
        """Trusted registry chain; not an automatic append inference from stat."""
        with self.write() as db:
            self._confirm_append(db,source,revision,expected_receipt=expected_receipt,
                                 receipt=receipt,frontier=frontier,writer_epoch=writer_epoch)

    def _confirm_append(self, db, source, revision, *, expected_receipt, receipt, frontier, writer_epoch):
        """Same validation, inside the explicit registry consumer transaction."""
        row = self._source(db,source,revision)
        active = db.execute('SELECT revision FROM active WHERE source=?',(source,)).fetchone()
        if active[0] != revision or row['receipt'] != expected_receipt or row['writer_epoch'] != writer_epoch or receipt == expected_receipt:
            raise Conflict('append receipt chain mismatch')
        if not isinstance(frontier,int) or isinstance(frontier,bool) or frontier < row['frontier']:
            raise Conflict('truncation requires new revision')
        sig = signature(row['path'])
        if sig[:2] != (row['dev'],row['ino']) or sig[2] < row['size'] or frontier > sig[2]:
            raise Conflict('rotation/truncation requires explicit registration')
        prior=db.execute('SELECT * FROM receipt WHERE source=? AND revision=? AND receipt=?',
                         (source,revision,expected_receipt)).fetchone()
        chain=digest(json.dumps([prior['chain_hash'],receipt,frontier,*sig]).encode())
        db.execute('INSERT INTO receipt VALUES(?,?,?,?,?,?,?)',
                   (source,revision,receipt,prior['seq']+1,expected_receipt,frontier,chain))
        db.execute('UPDATE source SET receipt=?,frontier=?,size=?,mtime=?,ctime=? WHERE source=? AND revision=?',
                   (receipt,frontier,sig[2],sig[3],Path(row['path']).stat().st_ctime_ns,source,revision))
        db.execute('UPDATE state SET generation=generation+1,pending=pending+? WHERE id=1',
                   (int(row['cursor']<frontier)-int(row['cursor']<row['frontier']),))

    @staticmethod
    def _legacy_context_matches(db,old,registration):
        """Recognize the specific earlier inventory-job encoding; never rewrite it."""
        context=json.loads(old['context'])
        if not isinstance(context,dict):return False
        old_job=context.pop('inventory',None)
        if context!=registration.get('context',{}):return False
        if old_job is None:return old['writer_epoch']==registration['writer_epoch']
        if old['writer_epoch']!='inventory:'+str(old_job):return False
        root=db.execute('SELECT ordinal FROM inventory_root WHERE inventory=? AND path=?',
                        (old_job,context.get('root'))).fetchone()
        if root is None:return False
        evidence=db.execute('SELECT item FROM inventory_entry WHERE inventory=? AND root=? AND name=?',
                            (old_job,root[0],context.get('filename'))).fetchone()
        if evidence is None:return False
        original=json.loads(evidence[0]).get('registration',{})
        return (original.get('source')==old['source'] and original.get('revision')==old['revision']
                and original.get('writer_epoch')==old['writer_epoch']
                and original.get('context')==json.loads(old['context']))

    def _observe_inventory(self,db,item):
        """Bind an immutable job observation to a source without resetting it.

        Only items verified against their closed local manifest use this path. Generic register
        remains strict. A changed but unconfirmed file becomes an explicit pending
        decision; unaffected neighbours can still be ingested in the same page.
        """
        observation=item['observation'];registration=item['registration']
        entry=db.execute('SELECT e.id,e.item,i.status FROM inventory_entry e JOIN inventory i ON i.id=e.inventory '
                         'WHERE e.inventory=? AND e.root=? AND e.name=?',
                         (observation['inventory'],observation['root'],observation['name'])).fetchone()
        if entry is None or entry['status']!='complete' or json.loads(entry['item'])!=item:
            raise Conflict('inventory observation does not match closed manifest')
        old_binding=db.execute('SELECT observation_hash FROM source_observation WHERE inventory=? AND entry_id=?',
                               (observation['inventory'],entry['id'])).fetchone()
        if old_binding:
            if old_binding[0]!=digest(entry['item'].encode()):
                raise Conflict('acknowledged inventory evidence changed')
            return  # Historical acknowledgement only, never new coverage.
        observed=tuple(registration['observed_signature'])
        if signature(registration['path'])!=observed:
            raise Conflict('inventory observation changed; new explicit observation required')
        old=db.execute('SELECT s.* FROM source s JOIN active a ON a.source=s.source AND a.revision=s.revision '
                       'WHERE s.source=?',(registration['source'],)).fetchone()
        if old is None:
            self._register(db,registration)
            bound=self._source(db,registration['source'],registration['revision'])
            outcome='registered'
        else:
            if ((old['path'],old['namespace'],old['dev'],old['ino']) !=
                    (registration['path'],registration['namespace'],observed[0],observed[1])
                    or not self._legacy_context_matches(db,old,registration)):
                raise Conflict('inventory source identity/namespace/provenance differs')
            bound=old
            if observed==(old['dev'],old['ino'],old['size'],old['mtime']):
                receipt=db.execute('SELECT frontier FROM receipt WHERE source=? AND revision=? AND receipt=?',
                                   (old['source'],old['revision'],old['receipt'])).fetchone()
                if receipt is None or receipt[0]!=old['frontier']:
                    raise Conflict('known source receipt missing or inconsistent')
                outcome='known_prefix' if old['frontier']==observed[2] else 'known_prefix_unconfirmed_tail'
            else:
                # Do not infer append from size, create a revision, advance a
                # frontier, or reset index output. The caller must decide explicitly.
                outcome='needs_receipt_or_revision'
        db.execute('INSERT INTO source_observation VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (observation['inventory'],entry['id'],registration['source'],registration['revision'],
                    bound['revision'],bound['receipt'],bound['frontier'],bound['cursor'],outcome,digest(entry['item'].encode())))

    def inventory_observations(self,inventory,*,after_entry=0,limit=MAX_PAGE):
        if not 1<=limit<=MAX_PAGE:raise ValueError('invalid observation page')
        with self.connection() as db:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_observation'").fetchone()
            if not exists:raise Unavailable('inventory observation capability not enabled')
            return [dict(r) for r in db.execute('SELECT * FROM source_observation WHERE inventory=? AND entry_id>? '
                                              'ORDER BY entry_id LIMIT ?',(inventory,after_entry,limit))]

    def ingest_page(self, job, *, expected_cursor, items, finish=False, fault=lambda _:None):
        """Consume a bounded page of an EXPLICIT stable inventory/registry manifest.

        The adapter owns enumeration; this method never scans a directory. Entries
        have kind=source and registration, or kind=helper and absolute path.
        Cursor counts manifest entries, not raw bytes or directory iterator offsets.
        """
        if len(items)>MAX_PAGE:
            raise ValueError('inventory page too large')
        with self.write() as db:
            row = db.execute('SELECT * FROM migration WHERE job=?',(job,)).fetchone()
            if (0 if row is None else row['cursor']) != expected_cursor or (row and row['finished']):
                raise Conflict('manifest checkpoint changed or finished')
            if any('observation' in item for item in items):
                for statement in OBSERVATION_SCHEMA:db.execute(statement)
            for item in items:
                if item['kind']=='source':
                    if 'observation' in item:self._observe_inventory(db,item)
                    else:self._register(db,item['registration'])
                elif item['kind']=='helper' and Path(item['path']).is_absolute():
                    added=db.execute('INSERT OR IGNORE INTO helper VALUES(?,?)',(job,item['path'])).rowcount
                    db.execute('UPDATE state SET helpers=helpers+? WHERE id=1',(added,))
                else:
                    raise ValueError('explicit source/helper classification required')
            fault('after_registrations')
            db.execute('INSERT INTO migration VALUES(?,?,?) ON CONFLICT DO UPDATE SET cursor=excluded.cursor,finished=excluded.finished',
                       (job,expected_cursor+len(items),int(finish)))
            fault('after_migration_cursor')
        fault('after_migration_commit')
        return expected_cursor+len(items)

    def migration_progress(self,job):
        with self.connection() as db:
            r=db.execute('SELECT * FROM migration WHERE job=?',(job,)).fetchone()
            return None if r is None else dict(r)

    def registered(self, *, after=('', ''), limit=64):
        if not 1<=limit<=MAX_PAGE: raise ValueError('invalid page size')
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM source WHERE (source,revision)>(?,?) ORDER BY source,revision LIMIT ?',(*after,limit))]

    def status(self):
        with self.connection() as db:
            return dict(db.execute('SELECT * FROM state WHERE id=1').fetchone())

    def progress(self,source,revision):
        with self.connection() as db:
            r=self._source(db,source,revision)
            return {k:r[k] for k in ('cursor','frontier','records','errors','receipt')}

    def index_step(self,source,revision,*,expected_cursor,max_bytes=MAX_BYTES,max_records=MAX_PAGE,fault=lambda _:None,prefix_reader=None):
        if not 1<=max_bytes<=MAX_BYTES or not 1<=max_records<=MAX_PAGE:
            raise ValueError('invalid index budget')
        with self.write() as db:
            row=self._source(db,source,revision)
            if row['cursor']!=expected_cursor: raise Conflict('index checkpoint changed')
            if prefix_reader is None:
                self._verify(row)
            if row['cursor']==row['frontier']:
                return {'cursor':row['cursor'],'files':0,'bytes':0,'decodes':0,'state':'caught_up'}
            if prefix_reader is not None:
                raw,physical_bytes=prefix_reader.read_index_block(db,row,max_bytes=max_bytes)
            else:
                with Path(row['path']).open('rb') as handle:
                    handle.seek(row['cursor']);raw=handle.read(min(max_bytes,row['frontier']-row['cursor']))
                self._verify(row)
                physical_bytes=len(raw)
            context=json.loads(row['context'])
            if context.get('record_format')=='rs485':
                db.execute('CREATE TABLE IF NOT EXISTS rs485_identity_cursor(source TEXT,revision TEXT,adr INTEGER,identity TEXT,PRIMARY KEY(source,revision,adr))')
            offset=row['cursor'];decoded=errors=0
            for fragment in raw.split(b'\n')[:-1]:
                if decoded>=max_records: break
                line=fragment+b'\n'
                end=offset+len(line); decoded+=1
                try:
                    value=json.loads(line)
                    stamp,module,identity=indexed_coordinates(value,context)
                    if context.get('record_format')=='rs485':
                        from rs485_evidence import Rs485HistorySeries
                        adr=int(value['adr'])
                        decoded_identity=Rs485HistorySeries._serial_decode(value)
                        if decoded_identity is not None:
                            db.execute('INSERT OR REPLACE INTO rs485_identity_cursor VALUES(?,?,?,?)',
                                (source,revision,adr,json.dumps(decoded_identity)))
                        from rs485_evidence import decode_identity_record
                        restored=decode_identity_record(value)
                        if restored is not None:
                            if not 0<=adr<=255:raise ValueError('RS485 address outside wire range')
                            active_revision=db.execute('SELECT revision FROM active WHERE source=?',(source,)).fetchone()[0]
                            if active_revision==revision:
                                db.execute('INSERT OR REPLACE INTO rs485_restore VALUES(?,?,?,?,?,?)',
                                    (adr,source,revision,row['path'],offset,json.dumps(restored)))
                        prior=db.execute('SELECT identity FROM rs485_identity_cursor WHERE source=? AND revision=? AND adr=?',
                            (source,revision,adr)).fetchone()
                        identity=json.dumps({'source_format':'rs485','bus_identity':json.loads(prior[0]) if prior else None})
                    db.execute('INSERT INTO entry VALUES(?,?,?,?,?,?,?,?,?)',
                               (source,revision,offset,end,math.floor(stamp/86400),stamp,module,identity,digest(line)))
                except (ValueError,TypeError,KeyError,AttributeError,OverflowError):
                    errors+=1
                    db.execute('INSERT INTO issue VALUES(?,?,?,?,?,?)',(source,revision,offset,end,'invalid_record',digest(line)))
                offset=end
            fault('after_index_rows')
            db.execute('UPDATE source SET cursor=?,records=records+?,errors=errors+? WHERE source=? AND revision=?',
                       (offset,decoded-errors,errors,source,revision))
            active=db.execute('SELECT revision FROM active WHERE source=?',(source,)).fetchone()[0]==revision
            db.execute('UPDATE state SET generation=generation+1,errors=errors+?,pending=pending-? WHERE id=1',
                       (errors,int(active and offset==row['frontier'])))
            fault('after_index_cursor')
        fault('after_index_commit')
        return {'cursor':offset,'files':1,'bytes':physical_bytes,'decodes':decoded,
                'state':'caught_up' if offset==row['frontier'] else ('partial_line_or_budget' if offset==expected_cursor else 'more')}
