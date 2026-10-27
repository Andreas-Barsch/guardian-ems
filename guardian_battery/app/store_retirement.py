"""Explicit fenced retirement in one derived repository, with bounded steps.

Caller selects exact objects; no implicit age/size policy or Raw deletion.
Native FK relationships and packed counts references are indexed durably.
"""
import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from projection_repository import Conflict, _json
from research_budget import check_budget

SCHEMA='''
CREATE TABLE IF NOT EXISTS lifecycle_layout(name TEXT PRIMARY KEY,keys TEXT,columns TEXT,after_row INTEGER DEFAULT 0,ready INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS lifecycle_edge(owner_table TEXT,owner_key TEXT,target_table TEXT,target_key TEXT,PRIMARY KEY(owner_table,owner_key,target_table,target_key));
CREATE INDEX IF NOT EXISTS lifecycle_target ON lifecycle_edge(target_table,target_key,owner_table,owner_key);
CREATE TABLE IF NOT EXISTS lifecycle_generation(owner_table TEXT,owner_key TEXT,name TEXT,generation INTEGER,PRIMARY KEY(owner_table,owner_key));
CREATE INDEX IF NOT EXISTS lifecycle_generation_window ON lifecycle_generation(name,generation);
CREATE TABLE IF NOT EXISTS lifecycle_analysis_root(job TEXT PRIMARY KEY,active INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS lifecycle_analysis_active ON lifecycle_analysis_root(active,job);
CREATE TABLE IF NOT EXISTS lifecycle_plan(id TEXT PRIMARY KEY,policy TEXT,schema_hash TEXT,phase TEXT DEFAULT 'selecting',version INTEGER DEFAULT 0,contract TEXT DEFAULT 'guardian-derived-lifecycle-1',nonce TEXT,archive_proof TEXT,after_table TEXT DEFAULT '',after_key TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS lifecycle_candidate(plan TEXT,table_name TEXT,object_key TEXT,row_hash TEXT,status TEXT DEFAULT 'selected',after_owner_table TEXT DEFAULT '',after_owner_key TEXT DEFAULT '',PRIMARY KEY(plan,table_name,object_key));
CREATE INDEX IF NOT EXISTS lifecycle_pending ON lifecycle_candidate(plan,status,table_name,object_key);
CREATE TABLE IF NOT EXISTS lifecycle_delete_permit(table_name TEXT,object_key TEXT,PRIMARY KEY(table_name,object_key));
CREATE TABLE IF NOT EXISTS lifecycle_fence(table_name TEXT,object_key TEXT,plan TEXT,PRIMARY KEY(table_name,object_key));
'''
# Only derived objects with stable semantics are eligible. Root/current state,
# original log/source receipts and unrecognized tables can never be selected.
ELIGIBLE={'pin','bundle','member','bundle_cursor','dependency','publication','value_version',
'draft','draft_partition','draft_value','draft_cursor','replacement_work','canonical_counts',
'window_index_job','window_index_row','canonical_day_job','canonical_publication_dependency',
'diagnostic_analysis_job','diagnostic_analysis_result','diagnostic_condition_cache','diagnostic_row',
'display_job','display_bucket','display_cadence','display_replacement','canonical_correction',
'canonical_correction_done','canonical_correction_request','canonical_epoch_job','preparation_job','generation',
'read_metadata_reader','read_metadata','read_serial','read_topology','read_position','read_identity_epoch','read_timeline','metadata_migration','metadata_input','metadata_position_stage','metadata_page_proof',
'metadata_reader','metadata_revision','metadata_root','metadata_cursor','metadata_node','metadata_registry_cursor','metadata_registry_source','metadata_maintenance_reset'}


def fingerprint(db):
    rows=db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps([tuple(r) for r in rows],separators=(',',':')).encode()).hexdigest()


def key_sql(keys,prefix=''):
    return 'json_array('+','.join(prefix+k for k in keys)+')'


def row_hash(row):return hashlib.sha256(_json(dict(row)).encode()).hexdigest()


class StoreRetirement:
    def __init__(self,repository):self.repository=repository

    @contextmanager
    def _write(self):
        steps=0
        with self.repository._write() as db:
            def progress():
                nonlocal steps
                steps+=100
                return int(steps>=200000)
            db.set_progress_handler(progress,100)
            try:
                yield db
            except sqlite3.OperationalError as exc:
                if steps>=200000:raise Conflict('lifecycle SQL budget exhausted; no automatic retry/full scan') from exc
                raise
            finally:
                db.set_progress_handler(None,0)
                self.last_sql_vm_steps=steps

    @staticmethod
    def _known_shapes():
        from projection_repository import _SCHEMA as kernel
        from canonical_day_repository import SCHEMA as canonical
        from canonical_checkpoint_storage import SCHEMA as counts
        from display_day_repository import SCHEMA as display
        from diagnostic_repository import SCHEMA as diagnostic
        from history_repository_preparation import SCHEMA as preparation
        from publication_window_index import SCHEMA as windows
        from read_metadata_index import SCHEMA as metadata
        from metadata_migration import SCHEMA as migration
        from metadata_tree import SCHEMA as tree
        from metadata_revision import SCHEMA as revisions
        from metadata_log_consumer import SCHEMA as registry_metadata
        from application_tail import SCHEMA as application_tail
        from application_metadata import SCHEMA as application_metadata
        from application_migration import SCHEMA as application_adoption
        db=sqlite3.connect(':memory:')
        try:
            for schema in (kernel,canonical,counts,display,diagnostic,preparation,windows,metadata,migration,tree,revisions,registry_metadata,application_tail,application_metadata,application_adoption):db.executescript(schema)
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {name:([tuple(r) for r in db.execute(f'PRAGMA table_info({name})')],[tuple(r) for r in db.execute(f'PRAGMA foreign_key_list({name})')]) for name in tables}
        finally:db.close()

    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
        with self._write() as db:
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'lifecycle_%' AND name NOT LIKE 'retention_%' ORDER BY name")]
            layouts={};known=self._known_shapes()
            for table in tables:
                if not table.replace('_','').isalnum():raise Conflict('unsupported table name')
                info=list(db.execute(f'PRAGMA table_info({table})'))
                if table not in known or ([tuple(r) for r in info],[tuple(r) for r in db.execute(f'PRAGMA foreign_key_list({table})')])!=known[table]:raise Conflict('unknown/changed store schema; explicit ownership adapter required: '+table)
                keys=[r['name'] for r in sorted(info,key=lambda r:r['pk']) if r['pk']]
                if not keys:keys=['rowid']
                columns=[r['name'] for r in info if r['type'].upper()=='TEXT']
                layouts[table]=(keys,columns)
                prior=db.execute('SELECT keys,columns FROM lifecycle_layout WHERE name=?',(table,)).fetchone()
                if prior and tuple(prior)!=(_json(keys),_json(columns)):raise Conflict('ownership layout migration required')
                db.execute('INSERT OR IGNORE INTO lifecycle_layout(name,keys,columns) VALUES(?,?,?)',(table,_json(keys),_json(columns)))
        for table,(keys,columns) in layouts.items():
            with self._write() as db:
                # Virtual Rtree tables have no trigger support. The ordinary
                # owner rows are fenced; bounds are explicitly removed with them.
                sql=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]
                if 'VIRTUAL TABLE' in (sql or '').upper() or table.startswith('window_index_bounds_'):
                    db.execute('UPDATE lifecycle_layout SET ready=1 WHERE name=?',(table,));continue
                newkey=key_sql(keys,'NEW.');oldkey=key_sql(keys,'OLD.')
                expressions=[]
                foreign={}
                for fk in db.execute(f'PRAGMA foreign_key_list({table})'):foreign.setdefault(fk['id'],[]).append(fk)
                for group in foreign.values():
                    group.sort(key=lambda r:r['seq']);target=group[0]['table']
                    if target not in layouts:raise Conflict('unknown foreign ownership target')
                    targetkeys=layouts[target][0]
                    # The current repository schemas reference primary keys.
                    names=[r['to'] or targetkeys[i] for i,r in enumerate(group)]
                    if names!=targetkeys:raise Conflict('non-primary foreign ownership requires explicit adapter')
                    values=','.join('NEW.'+r['from'] for r in group)
                    condition=' AND '.join('NEW.'+r['from']+' IS NOT NULL' for r in group)
                    expressions.append(f"SELECT '{table}',{newkey},'{target}',json_array({values}) WHERE {condition}")
                for column in columns:
                    expressions.append(f"SELECT '{table}',{newkey},'canonical_counts',json_array(value) FROM json_tree(CASE WHEN json_valid(NEW.{column}) THEN NEW.{column} ELSE NULL END) WHERE key='$canonical_counts' AND type='integer'")
                # Relationships in the real schemas that predate native FKs.
                extra={'window_index_job':[('bundle','bundle')], 'window_index_row':[('bundle','bundle')],
                    'canonical_day_job':[('predecessor','bundle'),('base','bundle'),('bundle','bundle'),('work','draft')],
                    'canonical_publication_dependency':[('bundle','bundle')],
                    'diagnostic_composition':[('bundle','bundle'),('work','draft'),('private_gen','generation')],
                    'diagnostic_row':[('born','generation'),('retired','generation')],
                    'diagnostic_analysis_job':[('bundle','bundle')],
                    'diagnostic_analysis_result':[('job','diagnostic_analysis_job')],
                    'display_job':[('base','bundle')], 'display_current':[('job','display_job')],
                    'display_bucket':[('job','display_job')], 'display_cadence':[('job','display_job')],
                    'display_replacement':[('job','display_job'),('work','draft')],
                    'canonical_correction_done':[('job','canonical_day_job'),('old_bundle','bundle'),('correction','canonical_correction')],
                    'canonical_correction_request':[('id','canonical_correction')],
                    'canonical_epoch_job':[('correction','canonical_correction')],
                    'canonical_counts':[('left_id','canonical_counts'),('right_id','canonical_counts')]}
                if table in ('read_serial','read_topology','read_position','read_identity_epoch','read_timeline','metadata_migration','metadata_input','metadata_position_stage','metadata_page_proof'):
                    extra[table]=[('revision','read_metadata')]
                for column,target in extra.get(table,[]):
                    expressions.append(f"SELECT '{table}',{newkey},'{target}',json_array(NEW.{column}) WHERE NEW.{column} IS NOT NULL")
                # Captured canonical inputs of private display jobs remain roots.
                if table=='display_job':
                    expressions.append(f"SELECT '{table}',{newkey},'bundle',json_array(json_extract(value,'$.bundle')) FROM json_each(NEW.canonical) WHERE json_extract(value,'$.bundle') IS NOT NULL")
                if table=='diagnostic_row':
                    expressions.append(f"SELECT '{table}',{newkey},'diagnostic_row',json_array(NEW.name,'sample',json_extract(NEW.payload,'$.sample_key'),json_extract(NEW.payload,'$.sample_born')) WHERE NEW.kind='history_slot' AND json_extract(NEW.payload,'$.sample_key') IS NOT NULL")
                insert=''.join('INSERT OR IGNORE INTO lifecycle_edge '+v+';' for v in expressions)
                clear=f"DELETE FROM lifecycle_edge WHERE owner_table='{table}' AND owner_key={oldkey};"
                if table in ('publication','draft_partition'):
                    insert+=f"INSERT OR REPLACE INTO lifecycle_generation SELECT '{table}',{newkey},substr(NEW.product,12),json_extract(NEW.metadata,'$.row_generation') WHERE NEW.product LIKE 'diagnostic:%' AND json_type(NEW.metadata,'$.row_generation')='integer';"
                    clear+=f"DELETE FROM lifecycle_generation WHERE owner_table='{table}' AND owner_key={oldkey};"
                if table=='diagnostic_composition':
                    insert+=f"INSERT OR REPLACE INTO lifecycle_generation SELECT '{table}',{newkey},NEW.name,NEW.private_gen WHERE NEW.private_gen IS NOT NULL;"
                    clear+=f"DELETE FROM lifecycle_generation WHERE owner_table='{table}' AND owner_key={oldkey};"
                if table=='diagnostic_analysis_job':
                    insert+="INSERT OR REPLACE INTO lifecycle_analysis_root VALUES(NEW.id,NEW.status!='complete');"
                    clear+="DELETE FROM lifecycle_analysis_root WHERE job=OLD.id;"
                for suffix,event,body in [('insert','INSERT',insert),('update','UPDATE',clear+insert),('delete','DELETE',clear)]:
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS lifecycle_{table}_{suffix} AFTER {event} ON {table} BEGIN {body or "SELECT 1;"} END')
                db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_{table}_fenced BEFORE UPDATE ON {table} WHEN EXISTS(SELECT 1 FROM lifecycle_fence WHERE table_name='{table}' AND object_key={oldkey}) BEGIN SELECT RAISE(ABORT,'lifecycle object fenced'); END")
                db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_{table}_delete_guard BEFORE DELETE ON {table} WHEN EXISTS(SELECT 1 FROM lifecycle_fence WHERE table_name='{table}' AND object_key={oldkey}) AND NOT EXISTS(SELECT 1 FROM lifecycle_delete_permit WHERE table_name='{table}' AND object_key={oldkey}) BEGIN SELECT RAISE(ABORT,'lifecycle deletion fenced'); END")
        with self._write() as db:
            db.execute("CREATE TRIGGER IF NOT EXISTS lifecycle_reference_fence BEFORE INSERT ON lifecycle_edge WHEN EXISTS(SELECT 1 FROM lifecycle_fence f WHERE f.table_name=NEW.target_table AND f.object_key=NEW.target_key AND NOT EXISTS(SELECT 1 FROM lifecycle_candidate c WHERE c.plan=f.plan AND c.table_name=NEW.owner_table AND c.object_key=NEW.owner_key AND c.status!='deleted')) BEGIN SELECT RAISE(ABORT,'lifecycle reference fenced'); END")

        from lifecycle_leases import LeaseOwners
        LeaseOwners(self.repository).initialize()

    def _layout(self,db,table):
        row=db.execute('SELECT * FROM lifecycle_layout WHERE name=?',(table,)).fetchone()
        if row is None:raise Conflict('unknown ownership schema')
        return json.loads(row['keys']),json.loads(row['columns'])

    def _row(self,db,table,key):
        keys,_=self._layout(db,table);values=json.loads(key)
        if len(values)!=len(keys):raise ValueError('object key shape')
        return db.execute(f'SELECT rowid AS _rowid,* FROM {table} WHERE '+ ' AND '.join(k+' IS ?' for k in keys),values).fetchone()

    def index_step(self,*,limit=8,fault=lambda stage:None):
        if not 1<=limit<=32:raise ValueError('ownership page budget')
        # Backfill by replaying INSERT trigger SELECTs against existing rows,
        # without updating immutable application objects.
        with self._write() as db:
            state=db.execute('SELECT * FROM lifecycle_layout WHERE ready=0 ORDER BY name LIMIT 1').fetchone()
            if state is None:return {'complete':True,'rows':0}
            table=state['name'];keys=json.loads(state['keys']);columns=json.loads(state['columns'])
            rows=db.execute(f'SELECT rowid AS _rowid,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?',(state['after_row'],limit)).fetchall()
            trigger=db.execute('SELECT sql FROM sqlite_master WHERE name=?',('lifecycle_'+table+'_insert',)).fetchone()[0]
            body=trigger[trigger.index(' BEGIN ')+7:];body=body.rsplit(' END',1)[0]
            reservation=db.execute('SELECT sql FROM sqlite_master WHERE name=?',('lifecycle_reserve_'+table,)).fetchone()
            if reservation:
                sql=reservation[0];body+=sql[sql.index(' BEGIN ')+7:].rsplit(' END',1)[0]
            for row in rows:
                check_budget()
                if sum(len(str(row[c]).encode()) for c in columns if row[c] is not None)>262144:raise Conflict('ownership payload byte budget')
                for statement in body.split(';'):
                    if not statement.strip():continue
                    # Replace qualified column tokens by named parameters,
                    # retaining SQL operators and JSON expressions exactly.
                    import re
                    statement=re.sub(r'NEW\.([A-Za-z_][A-Za-z_0-9]*)',lambda m:':'+m[1],statement)
                    values=dict(row);values['rowid']=row['_rowid']
                    db.execute(statement,values)
            db.execute('UPDATE lifecycle_layout SET after_row=?,ready=? WHERE name=?',(rows[-1]['_rowid'] if rows else state['after_row'],int(len(rows)<limit),table))
            fault('before_ownership_commit')
        fault('after_ownership_commit')
        return {'complete':False,'rows':len(rows),'table':table}

    def begin(self,identifier,*,policy):
        if not policy.get('reason') or not policy.get('rollback_evidence'):raise ValueError('explicit policy and rollback evidence required')
        with self._write() as db:
            if db.execute('SELECT 1 FROM lifecycle_layout WHERE ready=0 LIMIT 1').fetchone():raise Conflict('ownership migration incomplete')
            old=db.execute('SELECT policy FROM lifecycle_plan WHERE id=?',(identifier,)).fetchone()
            if old:
                if old[0]!=_json(policy):raise Conflict('policy changed')
                return
            db.execute('INSERT INTO lifecycle_plan(id,policy,schema_hash,nonce) VALUES(?,?,?,?)',(identifier,_json(policy),fingerprint(db),uuid.uuid4().hex))

    def select(self,identifier,table,key):
        if table not in ELIGIBLE:raise Conflict('authoritative/current/unknown object is not retireable')
        key=json.dumps(key,ensure_ascii=False,separators=(',',':'))
        with self._write() as db:
            plan=self._plan(db,identifier)
            if plan['phase']!='selecting':raise Conflict('selection sealed')
            row=self._row(db,table,key)
            if row is None:raise Conflict('object absent')
            if table=='draft':
                permit=db.execute("SELECT 1 FROM lifecycle_reservation r JOIN lifecycle_owner o ON o.id=r.owner WHERE r.table_name='draft' AND r.object_key=? AND r.status='released' AND o.state='fenced'",(key,)).fetchone()
                if not permit:raise Conflict('active/unknown private draft owner protected')
            if table=='window_index_job' and not row['complete']:raise Conflict('active publication index protected')
            if table in ('canonical_day_job','display_job','diagnostic_analysis_job','preparation_job','canonical_epoch_job','canonical_correction','metadata_migration') and json.loads(plan['policy']).get('retry_closed') is not True:
                raise Conflict('completed job retry lifetime must be explicitly closed')
            if table=='pin' and not (row['owner']=='window-index' or row['owner'] in ('display revision','display correction') or row['owner'].startswith('canonical revision ') or row['token'].startswith('diagnostic-analysis:')):
                permit=db.execute("SELECT 1 FROM lifecycle_reservation r JOIN lifecycle_owner o ON o.id=r.owner WHERE r.table_name='pin' AND r.object_key=? AND r.status='released' AND o.state='fenced'",(key,)).fetchone()
                if not permit:raise Conflict('reader/rollback/unknown pin cannot be retired')
            if table in ('metadata_reader','read_metadata_reader'):
                permit=db.execute("SELECT 1 FROM lifecycle_reservation r JOIN lifecycle_owner o ON o.id=r.owner WHERE r.table_name=? AND r.object_key=? AND r.status='released' AND o.state='fenced'",(table,key,)).fetchone()
                if not permit:raise Conflict('active metadata reader protected')
            if table in ('canonical_day_job','display_job') and row['status'] not in ('published_closed','stale'):
                superseded=False
                if row['status']=='caught_up':
                    if table=='display_job':
                        current=db.execute('SELECT job FROM display_current WHERE day=?',(row['day'],)).fetchone()
                        superseded=current is not None and current[0]!=row['id']
                    else:
                        superseded=db.execute('SELECT 1 FROM canonical_day_job WHERE stream=? AND day=? AND revision>? LIMIT 1',(row['stream'],row['day'],row['revision'])).fetchone() is not None
                if not superseded:raise Conflict('active job protected')
            if table=='diagnostic_analysis_job' and row['status']!='complete':raise Conflict('active analysis protected')
            if table=='canonical_correction' and not row['finished']:raise Conflict('active correction protected')
            if table=='preparation_job' and row['status']!='done':raise Conflict('active preparation protected')
            if table == 'preparation_job' and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_tail'").fetchone():
                if db.execute('SELECT 1 FROM application_tail WHERE after_job<? LIMIT 1', (row['id'],)).fetchone():
                    raise Conflict('application subscriber has not acknowledged preparation job')
            if table=='diagnostic_row':
                if row['retired'] is None:raise Conflict('current logical diagnostic evidence is protected')
                if db.execute('SELECT 1 FROM lifecycle_generation WHERE name=? AND generation>=? AND generation<? LIMIT 1',(row['name'],row['born'],row['retired'])).fetchone():raise Conflict('retained generation protects diagnostic version; retire eligible publications first')
            if table=='diagnostic_condition_cache' and db.execute('SELECT 1 FROM lifecycle_analysis_root WHERE active=1 LIMIT 1').fetchone():raise Conflict('active analysis protects statistic contributions')
            if table=='read_metadata' and (not row['identity_ready'] or not row['timeline_ready']):raise Conflict('active classic metadata protected')
            if table=='metadata_migration' and row['phase']!='complete':raise Conflict('active migration protected')
            if table=='metadata_revision' and row['status']!='ready':raise Conflict('active metadata revision protected')
            if table=='canonical_epoch_job' and row['status'] not in ('done','complete'):raise Conflict('active epoch job protected')
            db.execute('INSERT OR IGNORE INTO lifecycle_candidate(plan,table_name,object_key,row_hash,status) VALUES(?,?,?,?,?)',(identifier,table,key,row_hash(row),'selected'))

    def _plan(self,db,identifier):
        row=db.execute('SELECT * FROM lifecycle_plan WHERE id=?',(identifier,)).fetchone()
        if row is None or row['contract']!='guardian-derived-lifecycle-1' or row['schema_hash']!=fingerprint(db):raise Conflict('retirement schema/plan changed')
        return row

    def fence_step(self,identifier,*,limit=8,fault=lambda stage:None):
        if not 1<=limit<=32:raise ValueError('fence page budget')
        with self._write() as db:
            plan=self._plan(db,identifier)
            if plan['phase'] not in ('selecting','fencing'):return {'complete':True,'rows':0}
            db.execute("UPDATE lifecycle_plan SET phase='fencing' WHERE id=?",(identifier,))
            rows=db.execute("SELECT * FROM lifecycle_candidate WHERE plan=? AND status='selected' ORDER BY table_name,object_key LIMIT ?",(identifier,limit)).fetchall()
            for item in rows:
                row=self._row(db,item['table_name'],item['object_key'])
                if row is None or row_hash(row)!=item['row_hash']:raise Conflict('selected object changed')
                db.execute('INSERT INTO lifecycle_fence VALUES(?,?,?)',(item['table_name'],item['object_key'],identifier))
                db.execute("UPDATE lifecycle_candidate SET status='fenced' WHERE plan=? AND table_name=? AND object_key=?",(identifier,item['table_name'],item['object_key']))
            if not rows:db.execute("UPDATE lifecycle_plan SET phase='validating' WHERE id=?",(identifier,))
            fault('before_fence_commit')
        fault('after_fence_commit')
        return {'complete':not rows,'rows':len(rows)}

    def validate_step(self,identifier,*,limit=8,fault=lambda stage:None):
        if not 1<=limit<=32:raise ValueError('reference validation page budget')
        with self._write() as db:
            plan=self._plan(db,identifier)
            if plan['phase']=='deleting':return {'complete':True,'edges':0}
            if plan['phase']!='validating':raise Conflict('finish fencing first')
            item=db.execute("SELECT * FROM lifecycle_candidate WHERE plan=? AND status='fenced' ORDER BY table_name,object_key LIMIT 1",(identifier,)).fetchone()
            if item is None:
                db.execute("UPDATE lifecycle_plan SET phase='deleting' WHERE id=?",(identifier,));return {'complete':True,'edges':0}
            table=item['table_name'];key=item['object_key'];row=self._row(db,table,key)
            # Retire complete groups. A live publication lease protects all its
            # index/job payloads, not only the bundle row itself.
            required={'window_index_job':('bundle','bundle'),'window_index_row':('bundle','bundle'),
                'diagnostic_analysis_job':('bundle','bundle'),'diagnostic_analysis_result':('job','diagnostic_analysis_job'),
                'display_bucket':('job','display_job'),'display_cadence':('job','display_job'),
                'display_replacement':('job','display_job'),'canonical_correction_done':('correction','canonical_correction'),
                'canonical_correction_request':('id','canonical_correction'),'canonical_epoch_job':('correction','canonical_correction'),
                'metadata_reader':('revision','metadata_revision'),'metadata_root':('revision','metadata_revision'),'metadata_cursor':('revision','metadata_revision'),
                'metadata_registry_cursor':('revision','metadata_revision'),'metadata_registry_source':('revision','metadata_revision')}
            if table in ('read_metadata_reader','read_serial','read_topology','read_position','read_identity_epoch','read_timeline','metadata_migration','metadata_input','metadata_position_stage','metadata_page_proof'):
                required[table]=('revision','read_metadata')
            if table in required:
                column,parent=required[table]
                if row[column] is not None:
                    parent_key=json.dumps([row[column]],ensure_ascii=False,separators=(',',':'))
                    if not db.execute('SELECT 1 FROM lifecycle_candidate WHERE plan=? AND table_name=? AND object_key=?',(identifier,parent,parent_key)).fetchone():raise Conflict('retirement must include publication ownership group')
            edges=db.execute('SELECT owner_table,owner_key FROM lifecycle_edge WHERE target_table=? AND target_key=? AND (owner_table,owner_key)>(?,?) ORDER BY owner_table,owner_key LIMIT ?',
                (table,key,item['after_owner_table'],item['after_owner_key'],limit)).fetchall()
            for edge in edges:
                check_budget()
                if not db.execute('SELECT 1 FROM lifecycle_candidate WHERE plan=? AND table_name=? AND object_key=?',(identifier,*edge)).fetchone():raise Conflict('external owner/reader/rollback root protects selected object')
            if len(edges)<limit:
                db.execute("UPDATE lifecycle_candidate SET status='validated' WHERE plan=? AND table_name=? AND object_key=?",(identifier,table,key))
            elif edges:
                db.execute('UPDATE lifecycle_candidate SET after_owner_table=?,after_owner_key=? WHERE plan=? AND table_name=? AND object_key=?',(*edges[-1],identifier,table,key))
            fault('before_validate_commit')
        fault('after_validate_commit')
        return {'complete':False,'edges':len(edges)}

    def abort_step(self,identifier,*,limit=8):
        if not 1<=limit<=32:raise ValueError('abort page budget')
        with self._write() as db:
            plan=self._plan(db,identifier)
            if plan['phase'] not in ('selecting','fencing','validating','aborting'):raise Conflict('deletion has begun; resume instead of releasing partial group')
            db.execute("UPDATE lifecycle_plan SET phase='aborting' WHERE id=?",(identifier,))
            rows=db.execute("SELECT * FROM lifecycle_candidate WHERE plan=? AND status IN ('selected','fenced','validated') ORDER BY status,table_name,object_key LIMIT ?",(identifier,limit)).fetchall()
            for row in rows:
                db.execute('DELETE FROM lifecycle_fence WHERE plan=? AND table_name=? AND object_key=?',(identifier,row['table_name'],row['object_key']))
                db.execute("UPDATE lifecycle_candidate SET status='aborted' WHERE plan=? AND table_name=? AND object_key=?",(identifier,row['table_name'],row['object_key']))
            if not rows:db.execute("UPDATE lifecycle_plan SET phase='aborted' WHERE id=?",(identifier,))
            return {'complete':not rows,'rows':len(rows)}

    def delete_step(self,identifier,*,limit=8,fault=lambda stage:None):
        if not 1<=limit<=32:raise ValueError('delete page budget')
        with self._write() as db:
            plan=self._plan(db,identifier)
            if plan['phase']!='deleting':raise Conflict('finish durable fencing first')
            # Only unreferenced objects: no FK cascade or unbounded owner closure.
            rows=db.execute("SELECT * FROM lifecycle_candidate WHERE plan=? AND status='validated' AND (table_name,object_key)>(?,?) ORDER BY table_name,object_key LIMIT ?",(identifier,plan['after_table'],plan['after_key'],limit)).fetchall()
            deleted=0
            for item in rows:
                if db.execute('SELECT 1 FROM lifecycle_edge WHERE target_table=? AND target_key=? LIMIT 1',(item['table_name'],item['object_key'])).fetchone():continue
                check_budget();table=item['table_name'];key=item['object_key'];row=self._row(db,table,key)
                if row is None or row_hash(row)!=item['row_hash']:raise Conflict('fenced object changed')
                if table=='value_version':
                    if db.execute('SELECT 1 FROM publication WHERE product=? AND partition_key=? AND lineage=? AND read_gen>=? AND (? IS NULL OR read_gen<?) LIMIT 1',
                        (row['product'],row['partition_key'],row['lineage'],row['born'],row['retired'],row['retired'])).fetchone():continue
                    if db.execute('SELECT 1 FROM replacement_work WHERE lineage=? LIMIT 1',(row['lineage'],)).fetchone():continue
                if table=='diagnostic_row':
                    if row['retired'] is None:raise Conflict('current diagnostic evidence cannot be deleted')
                    if db.execute('SELECT 1 FROM lifecycle_generation WHERE name=? AND generation>=? AND generation<? LIMIT 1',(row['name'],row['born'],row['retired'])).fetchone():continue
                if table=='diagnostic_condition_cache':
                    if db.execute('SELECT 1 FROM lifecycle_analysis_root WHERE active=1 LIMIT 1').fetchone():continue
                db.execute('INSERT INTO lifecycle_delete_permit VALUES(?,?)',(table,key))
                # Rtree companion row is bounded 1:1; no cascading query work.
                if table=='window_index_row':db.execute('DELETE FROM window_index_bounds WHERE id=?',(row['id'],))
                if table in ('canonical_day_job','display_job'):
                    from snapshot_lifecycle import SnapshotLifecycle
                    SnapshotLifecycle.enqueue(db,table,key,row)
                keys,_=self._layout(db,table)
                db.execute(f'DELETE FROM {table} WHERE '+' AND '.join(k+' IS ?' for k in keys),json.loads(key))
                deleted+=1
                db.execute('DELETE FROM lifecycle_delete_permit WHERE table_name=? AND object_key=?',(table,key))
                db.execute("UPDATE lifecycle_candidate SET status='deleted' WHERE plan=? AND table_name=? AND object_key=?",(identifier,table,key))
                db.execute('DELETE FROM lifecycle_fence WHERE table_name=? AND object_key=? AND plan=?',(table,key,identifier))
            db.execute('UPDATE lifecycle_plan SET after_table=?,after_key=? WHERE id=?',(rows[-1]['table_name'] if rows else '',rows[-1]['object_key'] if rows else '',identifier))
            pending=db.execute("SELECT 1 FROM lifecycle_candidate WHERE plan=? AND status='validated' LIMIT 1",(identifier,)).fetchone()
            if not pending:db.execute("UPDATE lifecycle_plan SET phase='complete' WHERE id=?",(identifier,))
            fault('before_retirement_commit')
        fault('after_retirement_commit')
        return {'complete':not pending,'examined':len(rows),'deleted':deleted,'blocked':bool(pending and not rows)}

    def audit_page(self,identifier,*,after=('', ''),limit=16):
        if not 1<=limit<=32:raise ValueError('audit page budget')
        with self.repository._connection() as db:
            plan=self._plan(db,identifier)
            if plan['phase'] not in ('complete','aborted'):raise Conflict('only terminal audit can be exported')
            rows=db.execute('SELECT table_name,object_key,row_hash,status FROM lifecycle_candidate WHERE plan=? AND (table_name,object_key)>(?,?) ORDER BY table_name,object_key LIMIT ?',
                (identifier,*after,limit)).fetchall()
            return {'plan':dict(plan),'rows':[dict(r) for r in rows],
                'next':(rows[-1]['table_name'],rows[-1]['object_key']) if rows else None}

    def prune_audit_step(self,identifier,*,proof,limit=16,fault=lambda stage:None):
        # The explicit caller attests an external archive and closes the retry
        # lifetime. No background expiry; no assertion that an archive was fsynced
        # by this method. The nonce prevents an old retry deleting a reused ID.
        if not 1<=limit<=32 or set(proof)!={'nonce','sha256','retry_closed'} or proof['retry_closed'] is not True:
            raise ValueError('explicit archival and retry-closure proof required')
        if len(proof['sha256'])!=64 or any(c not in '0123456789abcdef' for c in proof['sha256']):raise ValueError('archive SHA-256 required')
        encoded=_json(proof)
        with self._write() as db:
            present=db.execute('SELECT * FROM lifecycle_plan WHERE id=?',(identifier,)).fetchone()
            if present is None:return {'complete':True,'rows':0}
            plan=self._plan(db,identifier)
            if plan['phase'] not in ('complete','aborted') or plan['nonce']!=proof['nonce']:raise Conflict('audit is active or proof targets another run')
            if plan['archive_proof'] and plan['archive_proof']!=encoded:raise Conflict('archive proof changed')
            db.execute('UPDATE lifecycle_plan SET archive_proof=? WHERE id=?',(encoded,identifier))
            rows=db.execute('SELECT table_name,object_key FROM lifecycle_candidate WHERE plan=? ORDER BY table_name,object_key LIMIT ?',(identifier,limit)).fetchall()
            for row in rows:db.execute('DELETE FROM lifecycle_candidate WHERE plan=? AND table_name=? AND object_key=?',(identifier,*row))
            if not rows:db.execute('DELETE FROM lifecycle_plan WHERE id=?',(identifier,))
            fault('before_audit_prune_commit')
        fault('after_audit_prune_commit')
        return {'complete':not rows,'rows':len(rows)}
