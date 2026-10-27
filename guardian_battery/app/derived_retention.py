"""Explicit conservative retention of unreferenced derived count nodes.

No time/space defaults, Raw deletion, runtime activation or inferred ownership.
A durable reference-index job precedes deletion; every step has a row budget.
"""
import hashlib
import json
import uuid
from projection_repository import Conflict, _json
from research_budget import check_budget

SCHEMA = '''
CREATE TABLE IF NOT EXISTS retention_audit(plan TEXT PRIMARY KEY,nonce TEXT NOT NULL,proof TEXT);
CREATE TABLE IF NOT EXISTS retention_reference(
 owner_table TEXT, owner_id INTEGER, node INTEGER,
 PRIMARY KEY(owner_table,owner_id,node));
CREATE INDEX IF NOT EXISTS retention_incoming ON retention_reference(node);
CREATE TABLE IF NOT EXISTS retention_index(
 owner_table TEXT PRIMARY KEY, after_id INTEGER NOT NULL DEFAULT 0,
 complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS retention_plan(
 id TEXT PRIMARY KEY, policy TEXT NOT NULL, schema_hash TEXT NOT NULL,
 after_id INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
 complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS retention_owner(table_name TEXT PRIMARY KEY, columns_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retention_decision(
 plan TEXT, node INTEGER, digest TEXT, decision TEXT NOT NULL,
 PRIMARY KEY(plan,node));
CREATE INDEX IF NOT EXISTS retention_pending ON retention_decision(plan,decision,node);
'''


def schema_hash(db):
    rows=db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps([tuple(r) for r in rows],sort_keys=True,separators=(',',':')).encode()).hexdigest()


def reference_sql(table, columns, row):
    parts=[]
    for column in columns:
        parts.append(f"INSERT OR IGNORE INTO retention_reference SELECT '{table}',{row}.rowid,CAST(value AS INTEGER) FROM json_tree(CASE WHEN json_valid({row}.{column}) THEN {row}.{column} ELSE NULL END) WHERE key='$canonical_counts' AND type='integer';")
    return ''.join(parts)


class DerivedRetention:
    def __init__(self, repository): self.repository=repository

    def initialize(self):
        # Explicit schema-only action. Existing rows are NOT scanned here.
        with self.repository._connection() as db:
            db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
        with self.repository._write() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Inspect actual TEXT columns, including unanticipated owners. All
            # valid JSON descriptors are conservative references, even if a
            # future schema uses a field not in the original known inventory.
            for table in sorted(tables):
                if table.startswith(('retention_', 'sqlite_')) or table=='canonical_counts':continue
                if not table.replace('_','').isalnum():raise Conflict('unsupported table identifier')
                columns=tuple(r['name'] for r in db.execute(f'PRAGMA table_info({table})') if r['type'].upper()=='TEXT')
                if not columns:continue
                if any(not c.replace('_','').isalnum() for c in columns):raise Conflict('unsupported column identifier')
                db.execute('INSERT OR IGNORE INTO retention_owner VALUES(?,?)',(table,_json(columns)))
                db.execute('INSERT OR IGNORE INTO retention_index(owner_table) VALUES(?)',(table,))
                insert=reference_sql(table,columns,'NEW')
                delete=f"DELETE FROM retention_reference WHERE owner_table='{table}' AND owner_id=OLD.rowid;"
                for suffix,event,body in [('insert','INSERT',insert),('update','UPDATE',delete+insert),('delete','DELETE',delete)]:
                    db.execute(f'CREATE TRIGGER IF NOT EXISTS retention_{table}_{suffix} AFTER {event} ON {table} BEGIN {body} END')
            if 'canonical_counts' not in tables: raise Conflict('canonical count store absent')
            db.execute("INSERT OR IGNORE INTO retention_index(owner_table) VALUES('canonical_counts')")
            db.execute("CREATE TRIGGER IF NOT EXISTS retention_counts_insert AFTER INSERT ON canonical_counts BEGIN INSERT OR IGNORE INTO retention_reference SELECT 'canonical_counts',NEW.id,NEW.left_id WHERE NEW.left_id IS NOT NULL; INSERT OR IGNORE INTO retention_reference SELECT 'canonical_counts',NEW.id,NEW.right_id WHERE NEW.right_id IS NOT NULL; END")
            db.execute("CREATE TRIGGER IF NOT EXISTS retention_counts_delete AFTER DELETE ON canonical_counts BEGIN DELETE FROM retention_reference WHERE owner_table='canonical_counts' AND owner_id=OLD.id; END")

    def index_step(self, *, limit=16, fault=lambda stage:None):
        if not 1<=limit<=64: raise ValueError('reference page budget')
        check_budget()
        with self.repository._write() as db:
            state=db.execute('SELECT * FROM retention_index WHERE complete=0 ORDER BY owner_table LIMIT 1').fetchone()
            if state is None:return {'complete':True,'examined':0}
            table=state['owner_table']
            rows=db.execute(f'SELECT rowid AS owner_id,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?', (state['after_id'],limit)).fetchall()
            for row in rows:
                check_budget()
                # Reconcile under the same transaction as source read; triggers
                # cover concurrent/new writes before or after this cursor.
                db.execute('DELETE FROM retention_reference WHERE owner_table=? AND owner_id=?',(table,row['owner_id']))
                nodes=[]
                if table=='canonical_counts':nodes=[row['left_id'],row['right_id']]
                else:
                    columns=json.loads(db.execute('SELECT columns_json FROM retention_owner WHERE table_name=?',(table,)).fetchone()[0])
                    for column in columns:
                        raw=row[column]
                        if raw is None or not db.execute('SELECT json_valid(?)',(raw,)).fetchone()[0]:continue
                        if len(raw.encode())>65536:raise Conflict('owner payload exceeds migration byte budget')
                        nodes.extend(r[0] for r in db.execute("SELECT value FROM json_tree(?) WHERE key='$canonical_counts' AND type='integer'",(raw,)))
                for node in nodes:
                    if node is not None:db.execute('INSERT OR IGNORE INTO retention_reference VALUES(?,?,?)',(table,row['owner_id'],node))
            db.execute('UPDATE retention_index SET after_id=?,complete=? WHERE owner_table=?', (rows[-1]['owner_id'] if rows else state['after_id'],int(len(rows)<limit),table))
            fault('before_reference_commit')
        fault('after_reference_commit')
        return {'complete':False,'examined':len(rows),'table':table}

    def begin(self, identifier, *, policy):
        # A policy is a caller decision, not an automatic expiry. Scope is
        # deliberately limited to nodes with NO incoming owner/parent edges.
        if set(policy)!={'scope','reason','rollback_evidence'} or policy['scope']!='unreferenced_counts' or not policy['reason'] or not policy['rollback_evidence']:
            raise ValueError('explicit supported retention policy required')
        with self.repository._write() as db:
            if db.execute('SELECT 1 FROM retention_index WHERE complete=0 LIMIT 1').fetchone():raise Conflict('reference migration incomplete')
            fingerprint=schema_hash(db);encoded=_json(policy)
            old=db.execute('SELECT policy,schema_hash FROM retention_plan WHERE id=?',(identifier,)).fetchone()
            db.execute('INSERT OR IGNORE INTO retention_audit(plan,nonce) VALUES(?,?)',(identifier,uuid.uuid4().hex))
            if old:
                if tuple(old)!=(encoded,fingerprint):raise Conflict('retention plan contract changed')
                return
            db.execute('INSERT INTO retention_plan(id,policy,schema_hash) VALUES(?,?,?)',(identifier,encoded,fingerprint))

    def status(self, identifier):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM retention_plan WHERE id=?',(identifier,)).fetchone()
            if row is None:raise Conflict('retention plan absent')
            return dict(row)

    def decide_step(self, identifier, *, expected_version, limit=16, fault=lambda stage:None):
        if not 1<=limit<=64:raise ValueError('retention page budget')
        with self.repository._write() as db:
            state=db.execute('SELECT * FROM retention_plan WHERE id=?',(identifier,)).fetchone()
            if state is None or state['version']!=expected_version:raise Conflict('retention CAS changed')
            if state['schema_hash']!=schema_hash(db):raise Conflict('store schema changed; ownership review required')
            if state['complete']:return {'complete':True,'examined':0,'version':expected_version}
            rows=db.execute('SELECT id,digest FROM canonical_counts WHERE id>? ORDER BY id LIMIT ?',(state['after_id'],limit)).fetchall()
            for row in rows:
                check_budget()
                protected=db.execute('SELECT 1 FROM retention_reference WHERE node=? LIMIT 1',(row['id'],)).fetchone()
                db.execute('INSERT INTO retention_decision VALUES(?,?,?,?)',(identifier,row['id'],row['digest'],'retain' if protected else 'eligible'))
            db.execute('UPDATE retention_plan SET after_id=?,version=version+1,complete=? WHERE id=?',(rows[-1]['id'] if rows else state['after_id'],int(len(rows)<limit),identifier))
            fault('before_decision_commit')
        fault('after_decision_commit')
        return {'complete':len(rows)<limit,'examined':len(rows),'version':expected_version+1}

    def delete_step(self, identifier, *, limit=16, fault=lambda stage:None):
        if not 1<=limit<=64:raise ValueError('deletion page budget')
        with self.repository._write() as db:
            state=db.execute('SELECT * FROM retention_plan WHERE id=?',(identifier,)).fetchone()
            if state is None or not state['complete']:raise Conflict('finish separate retention decision first')
            if state['schema_hash']!=schema_hash(db):raise Conflict('store schema changed; ownership review required')
            rows=db.execute("SELECT * FROM retention_decision WHERE plan=? AND decision='eligible' ORDER BY node LIMIT ?",(identifier,limit)).fetchall()
            deleted=0
            for row in rows:
                check_budget()
                node=db.execute('SELECT digest FROM canonical_counts WHERE id=?',(row['node'],)).fetchone()
                protected=db.execute('SELECT 1 FROM retention_reference WHERE node=? LIMIT 1',(row['node'],)).fetchone()
                if node and node[0]!=row['digest']:raise Conflict('count node identity changed')
                if node and not protected:
                    db.execute('DELETE FROM canonical_counts WHERE id=?',(row['node'],));deleted+=1
                db.execute('UPDATE retention_decision SET decision=? WHERE plan=? AND node=?',('retain' if protected else 'deleted',identifier,row['node']))
            fault('before_delete_commit')
        fault('after_delete_commit')
        return {'examined':len(rows),'deleted':deleted}

    def audit_page(self,identifier,*,after=-1,limit=16):
        if not 1<=limit<=64:raise ValueError('audit page budget')
        with self.repository._connection() as db:
            state=db.execute('SELECT * FROM retention_plan WHERE id=?',(identifier,)).fetchone()
            audit=db.execute('SELECT * FROM retention_audit WHERE plan=?',(identifier,)).fetchone()
            if state is None or audit is None or not state['complete']:raise Conflict('terminal owned audit required')
            if db.execute("SELECT 1 FROM retention_decision WHERE plan=? AND decision='eligible' LIMIT 1",(identifier,)).fetchone():raise Conflict('deletion decisions unfinished')
            rows=db.execute('SELECT * FROM retention_decision WHERE plan=? AND node>? ORDER BY node LIMIT ?',(identifier,after,limit)).fetchall()
            return {'plan':dict(state),'identity':dict(audit),'rows':[dict(r) for r in rows],'next':rows[-1]['node'] if rows else None}

    def prune_audit_step(self,identifier,*,nonce,archive_sha256,retry_closed,limit=16):
        if retry_closed is not True or not 1<=limit<=64 or len(archive_sha256)!=64 or any(c not in '0123456789abcdef' for c in archive_sha256):raise ValueError('explicit archive/closed retry proof and page budget required')
        with self.repository._write() as db:
            state=db.execute('SELECT * FROM retention_plan WHERE id=?',(identifier,)).fetchone()
            if state is None:return {'complete':True,'rows':0}
            audit=db.execute('SELECT * FROM retention_audit WHERE plan=?',(identifier,)).fetchone()
            if audit is None or audit['nonce']!=nonce or not state['complete']:raise Conflict('stale archive proof or active retention')
            if audit['proof'] is not None and audit['proof']!=archive_sha256:raise Conflict('archive proof changed')
            if db.execute("SELECT 1 FROM retention_decision WHERE plan=? AND decision='eligible' LIMIT 1",(identifier,)).fetchone():raise Conflict('retention decisions unfinished')
            db.execute('UPDATE retention_audit SET proof=? WHERE plan=?',(archive_sha256,identifier))
            rows=db.execute('SELECT node FROM retention_decision WHERE plan=? ORDER BY node LIMIT ?',(identifier,limit)).fetchall()
            for row in rows:db.execute('DELETE FROM retention_decision WHERE plan=? AND node=?',(identifier,row[0]))
            if not rows:
                db.execute('DELETE FROM retention_plan WHERE id=?',(identifier,))
                db.execute('DELETE FROM retention_audit WHERE plan=?',(identifier,))
            return {'complete':not rows,'rows':len(rows)}
