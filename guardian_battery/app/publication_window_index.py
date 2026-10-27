"""Explicit, checkpointed read index over immutable repository publications.

Preparation is never performed by a GET. Originals remain authoritative. The
R-tree bounds SQL candidates by both publication and time; exact SQL predicates
remove its outward float rounding before payload decoding.
"""
import json
import math
import sqlite3
from datetime import datetime
from projection_repository import Conflict,Unavailable
from cell_source_resolver import CellSourceResolver
from research_budget import check_budget,ResearchQueryError
from canonical_day_repository import bounds

SCHEMA='''
CREATE TABLE IF NOT EXISTS window_index_job(bundle INTEGER,product TEXT,partition_key TEXT,
 kind TEXT,after_key TEXT NOT NULL DEFAULT '',complete INTEGER NOT NULL DEFAULT 0,
 metadata TEXT NOT NULL,pin TEXT NOT NULL,
 PRIMARY KEY(bundle,product,partition_key));
CREATE TABLE IF NOT EXISTS window_index_row(id INTEGER PRIMARY KEY,bundle INTEGER,product TEXT,
 partition_key TEXT,source_key TEXT,kind TEXT,start REAL,end REAL,module INTEGER,serial TEXT,
 metric TEXT,cell INTEGER,resolution TEXT,payload TEXT NOT NULL,
 UNIQUE(bundle,product,partition_key,source_key));
CREATE TABLE IF NOT EXISTS window_index_ready(product TEXT,partition_key TEXT,bundle INTEGER NOT NULL,
 PRIMARY KEY(product,partition_key),
 FOREIGN KEY(bundle,product,partition_key) REFERENCES window_index_job(bundle,product,partition_key));
CREATE INDEX IF NOT EXISTS window_index_ready_target ON window_index_ready(bundle,product,partition_key);
CREATE VIRTUAL TABLE IF NOT EXISTS window_index_bounds USING rtree(id,first,last,bundle_first,bundle_last);
'''

def epoch(value):return float(value) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


class PublicationWindowIndex:
    def __init__(self,repository):self.repository=repository

    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')

    def begin(self,bundle,product,day,kind):
        if kind not in ('display','canonical'):raise ValueError('unsupported publication kind')
        bounds(day)
        # Capture immutable metadata and keep the source alive until explicit GC.
        source=self.repository.read(bundle,product,day,limit=1)
        with self.repository._write() as db:
            if db.execute('SELECT 1 FROM window_index_job WHERE bundle=? AND product=? AND partition_key=?',(bundle,product,day)).fetchone():return
            import uuid
            pin=uuid.uuid4().hex
            db.execute('INSERT INTO pin VALUES(?,?,?)',(pin,bundle,'window-index'))
            db.execute('INSERT INTO window_index_job VALUES(?,?,?,?,?,0,?,?)',
                       (bundle,product,day,kind,'',json.dumps(source['metadata']),pin))

    def require_schema(self):
        from application_schema import require_schema
        with self.repository._connection() as db:
            require_schema(self,db,SCHEMA,('window_index_job','window_index_row','window_index_ready'))

    def state(self,bundle,product,day):
        self.require_schema()
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM window_index_job WHERE bundle=? AND product=? AND partition_key=?',(bundle,product,day)).fetchone()
            if row is None:raise Unavailable('publication window index needs explicit preparation')
            return dict(row)

    @staticmethod
    def _publish_ready(db,bundle,product,day):
        db.execute('INSERT INTO window_index_ready VALUES(?,?,?) ON CONFLICT(product,partition_key) '
                   'DO UPDATE SET bundle=excluded.bundle WHERE excluded.bundle>window_index_ready.bundle',
                   (product,day,bundle))

    @staticmethod
    def _entries(key,value,kind,day):
        first,last=bounds(day)
        if kind=='display':
            yield key,'display',value
        elif key.startswith('I'):
            yield key,value['kind'],value['value']
        elif key=='Zpending':
            for field,category in [('diagnostic_intervals','diagnostic'),('visual_intervals','visual')]:
                for number,item in enumerate(value[field]):yield key+field+str(number),category,item

    def step(self,bundle,product,day):
        check_budget();state=self.state(bundle,product,day)
        if state['complete']:
            # Explicit background step may acknowledge an already completed
            # index after local schema migration; never called by a GET.
            with self.repository._write() as db:self._publish_ready(db,bundle,product,day)
            return {'complete':True,'rows':0}
        page=self.repository.read(bundle,product,day,after_key=state['after_key'],limit=8)
        entries=[];first,last=bounds(day)
        for key,value in page['rows']:
            check_budget()
            for entry_key,kind,item in self._entries(key,value,state['kind'],day):
                check_budget()
                start=epoch(item.get('bucket_start',item.get('canonical_from',item.get('timestamp'))))
                end=epoch(item.get('bucket_end',item.get('canonical_to',item.get('timestamp'))) or last)
                entries.append((entry_key,kind,max(first,start),min(last,end),item))
        with self.repository._write() as db:
            current=db.execute('SELECT after_key,complete FROM window_index_job WHERE bundle=? AND product=? AND partition_key=?',(bundle,product,day)).fetchone()
            if tuple(current)!=(state['after_key'],0):raise Conflict('read index preparation changed')
            for key,kind,start,end,item in entries:
                if end<start:continue
                cursor=db.execute('INSERT INTO window_index_row(bundle,product,partition_key,source_key,kind,start,end,module,serial,metric,cell,resolution,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (bundle,product,day,key,kind,start,end,item.get('module_position'),item.get('physical_serial'),
                     item.get('metric'),item.get('cell_number',0),item.get('resolution'),json.dumps(item,separators=(',',':'))))
                db.execute('INSERT INTO window_index_bounds VALUES(?,?,?,?,?)',(cursor.lastrowid,start,end,bundle,bundle))
            after=page['rows'][-1][0] if page['rows'] else state['after_key']
            complete=int(len(page['rows'])<8)
            db.execute('UPDATE window_index_job SET after_key=?,complete=? WHERE bundle=? AND product=? AND partition_key=?',(after,complete,bundle,product,day))
            if complete:self._publish_ready(db,bundle,product,day)
        return {'complete':bool(complete),'rows':len(entries)}

    def query(self,bundle,product,day,start,end,*,modules=(),metric=None,cells=(),resolution=None,kind=None,max_rows=6000):
        with self.repository.lease(bundle,owner='window-reader'):
            return self._query(bundle,product,day,start,end,modules=modules,metric=metric,cells=cells,resolution=resolution,kind=kind,max_rows=max_rows)

    def _query(self,bundle,product,day,start,end,*,modules=(),metric=None,cells=(),resolution=None,kind=None,max_rows=6000):
        state=self.state(bundle,product,day)
        if not state['complete']:raise Unavailable('publication window index preparation incomplete')
        first,last=epoch(start),epoch(end)
        sql=('SELECT r.payload,r.kind FROM window_index_bounds b CROSS JOIN window_index_row r ON r.id=b.id '
             'WHERE b.first<=? AND b.last>=? AND b.bundle_first<=? AND b.bundle_last>=? '
             'AND r.bundle=? AND r.product=? AND r.partition_key=? AND r.start<=? AND r.end>=?')
        args=[last,first,bundle,bundle,bundle,product,day,last,first]
        if modules:sql+=' AND r.module IN ('+','.join('?' for _ in modules)+')';args.extend(modules)
        if metric is not None:sql+=' AND r.metric=?';args.append(metric)
        if cells:sql+=' AND r.cell IN ('+','.join('?' for _ in cells)+')';args.extend(cells)
        if resolution is not None:sql+=' AND r.resolution=?';args.append(resolution)
        if kind is not None:sql+=' AND r.kind=?';args.append(kind)
        sql+=' ORDER BY r.start,r.source_key LIMIT ?';args.append(max_rows+1)
        values=[];size=0;steps=0;failure=[]
        with self.repository._connection() as db:
            def progress():
                nonlocal steps
                steps+=100
                try:
                    check_budget()
                    if steps>100000:
                        raise ResearchQueryError('history_window_budget','Published SQL budget exhausted; no rebuild',503)
                except BaseException as exc:failure.append(exc);return 1
                return 0
            db.set_progress_handler(progress,100)
            try:
                for row in db.execute(sql,args):
                    check_budget();size+=len(row['payload'].encode())
                    if len(values)>=max_rows or size>8*1024*1024:
                        raise ResearchQueryError('history_window_budget','Published row/byte budget exceeded; no truncated success',503)
                    values.append((row['kind'],json.loads(row['payload'])))
            except sqlite3.OperationalError:
                if failure:raise failure[0]
                raise
            finally:db.set_progress_handler(None,0)
        return {'rows':values,'metadata':json.loads(state['metadata']),'bundle':bundle,
                'bytes':size,'decodes':len(values),'sql_vm_steps':steps}
