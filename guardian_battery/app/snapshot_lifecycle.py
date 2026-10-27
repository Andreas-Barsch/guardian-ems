"""Durable idempotent cross-store handoff for derived window snapshots only."""
import hashlib
from pathlib import Path
from projection_repository import Conflict

CATALOG_SCHEMA='''
CREATE TABLE IF NOT EXISTS window_owner(snapshot TEXT PRIMARY KEY REFERENCES window_snapshot,
 owner TEXT NOT NULL,proof TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active');
'''
REPOSITORY_SCHEMA='''
CREATE TABLE IF NOT EXISTS lifecycle_catalog(id TEXT PRIMARY KEY,path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lifecycle_snapshot_pending(kind TEXT,owner_key TEXT,sequence INTEGER DEFAULT 1,PRIMARY KEY(kind,owner_key));
CREATE TABLE IF NOT EXISTS lifecycle_snapshot_cursor(kind TEXT PRIMARY KEY,after_row INTEGER DEFAULT 0,ready INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS lifecycle_snapshot_binding(owner_table TEXT,owner_key TEXT,snapshot TEXT,catalog TEXT,
 PRIMARY KEY(owner_table,owner_key));
CREATE TABLE IF NOT EXISTS lifecycle_snapshot_release(id INTEGER PRIMARY KEY AUTOINCREMENT,owner_table TEXT,owner_key TEXT,snapshot TEXT,catalog TEXT,status TEXT DEFAULT 'pending',
 UNIQUE(owner_table,owner_key,snapshot,catalog));
CREATE INDEX IF NOT EXISTS lifecycle_snapshot_release_pending ON lifecycle_snapshot_release(status,id);
'''


def identity(path):
    path=Path(path).resolve();stat=path.stat()
    return hashlib.sha256(f'{path}|{stat.st_dev}|{stat.st_ino}'.encode()).hexdigest()


class SnapshotLifecycle:
    def __init__(self,repository,catalog):
        self.repository,self.catalog=repository,catalog
        self.catalog_id=identity(catalog.path);self.owner_id=identity(repository.path)
    def initialize(self):
        with self.catalog.connection() as db:db.executescript('BEGIN IMMEDIATE;'+CATALOG_SCHEMA+'COMMIT;')
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+REPOSITORY_SCHEMA+'COMMIT;')
        with self.repository._write() as db:
            db.execute('INSERT OR IGNORE INTO lifecycle_catalog VALUES(?,?)',(self.catalog_id,str(Path(self.catalog.path).resolve())))
            for kind in ('canonical_day_job','display_job'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(kind,)).fetchone():
                    db.execute('INSERT OR IGNORE INTO lifecycle_snapshot_cursor(kind) VALUES(?)',(kind,))
                    for suffix,event in [('insert','INSERT'),('update','UPDATE OF snapshot')]:
                        db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_snapshot_{kind}_{suffix} AFTER {event} ON {kind} BEGIN INSERT INTO lifecycle_snapshot_pending(kind,owner_key) VALUES('{kind}',json_array(NEW.id)) ON CONFLICT(kind,owner_key) DO UPDATE SET sequence=sequence+1; END")
    def _check(self):
        if identity(self.catalog.path)!=self.catalog_id or identity(self.repository.path)!=self.owner_id:raise Conflict('bound store identity changed')
    def bind_step(self,*,fault=lambda stage:None):
        import json
        self._check()
        with self.repository._connection() as db:
            pending=db.execute('SELECT * FROM lifecycle_snapshot_pending ORDER BY kind,owner_key LIMIT 1').fetchone()
            state=None
            if pending:
                table=pending['kind'];key=pending['owner_key']
                row=db.execute(f'SELECT rowid AS owner_row,* FROM {table} WHERE id=?',(json.loads(key)[0],)).fetchone()
            else:
                state=db.execute('SELECT * FROM lifecycle_snapshot_cursor WHERE ready=0 ORDER BY kind LIMIT 1').fetchone()
                if state is None:return {'complete':True,'rows':0}
                table=state['kind'];row=db.execute(f'SELECT rowid AS owner_row,* FROM {table} WHERE rowid>? ORDER BY rowid LIMIT 1',(state['after_row'],)).fetchone()
        if row is None:
            with self.repository._write() as db:
                if pending:db.execute('DELETE FROM lifecycle_snapshot_pending WHERE kind=? AND owner_key=? AND sequence=?',(table,key,pending['sequence']))
                else:db.execute('UPDATE lifecycle_snapshot_cursor SET ready=1 WHERE kind=? AND after_row=?',(table,state['after_row']))
            return {'complete':False,'rows':0}
        key=json.dumps([row['id']],separators=(',',':'),ensure_ascii=False);snapshot=row['snapshot'];owner=self.owner_id+'/'+table+'/'+key
        if snapshot is not None:
            with self.catalog.write() as db:
                if db.execute('SELECT 1 FROM window_snapshot WHERE id=?',(snapshot,)).fetchone() is None:
                    # Existing explicit close is not reconstructed or inferred.
                    pass  # Keep exact absent snapshot ID as an idempotent release receipt.
                else:
                    old=db.execute('SELECT owner,status FROM window_owner WHERE snapshot=?',(snapshot,)).fetchone()
                    if old and tuple(old)==('request:'+snapshot,'active'):
                        db.execute('UPDATE window_owner SET owner=?,proof=? WHERE snapshot=? AND owner=?',(owner,'durable '+table+' snapshot field',snapshot,'request:'+snapshot))
                        old=None
                    if old and tuple(old)!=(owner,'active'):raise Conflict('snapshot ownership conflict')
                    db.execute('INSERT OR IGNORE INTO window_owner(snapshot,owner,proof) VALUES(?,?,?)',(snapshot,owner,'durable '+table+' snapshot field'))
            fault('after_catalog_owner_commit')
        with self.repository._write() as db:
            current=db.execute(f'SELECT snapshot FROM {table} WHERE id=?',(row['id'],)).fetchone()
            if snapshot is not None:
                if current is None or current[0]!=snapshot:
                    db.execute('INSERT OR IGNORE INTO lifecycle_snapshot_release(owner_table,owner_key,snapshot,catalog) VALUES(?,?,?,?)',(table,key,snapshot,self.catalog_id))
                else:
                    prior=db.execute('SELECT snapshot,catalog FROM lifecycle_snapshot_binding WHERE owner_table=? AND owner_key=?',(table,key)).fetchone()
                    if prior and prior['snapshot']!=snapshot:
                        db.execute('INSERT OR IGNORE INTO lifecycle_snapshot_release(owner_table,owner_key,snapshot,catalog) VALUES(?,?,?,?)',(table,key,prior['snapshot'],prior['catalog']))
                    db.execute('INSERT INTO lifecycle_snapshot_binding VALUES(?,?,?,?) ON CONFLICT(owner_table,owner_key) DO UPDATE SET snapshot=excluded.snapshot,catalog=excluded.catalog',(table,key,snapshot,self.catalog_id))
            if pending:
                db.execute('DELETE FROM lifecycle_snapshot_pending WHERE kind=? AND owner_key=? AND sequence=?',(table,key,pending['sequence']))
            elif db.execute('UPDATE lifecycle_snapshot_cursor SET after_row=? WHERE kind=? AND after_row=?',(row['owner_row'],table,state['after_row'])).rowcount!=1:raise Conflict('snapshot binding cursor changed')
            fault('before_binding_commit')
        fault('after_binding_commit')
        return {'complete':False,'rows':1}
    @staticmethod
    def enqueue(db,table,key,row):
        if table not in ('canonical_day_job','display_job') or row['snapshot'] is None:return
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_snapshot_binding'").fetchone():raise Conflict('snapshot handoff not initialized')
        binding=db.execute('SELECT snapshot,catalog FROM lifecycle_snapshot_binding WHERE owner_table=? AND owner_key=?',(table,key)).fetchone()
        if binding is None or binding['snapshot']!=row['snapshot']:raise Conflict('snapshot handoff not bound; no orphan deletion')
        db.execute('INSERT OR IGNORE INTO lifecycle_snapshot_release(owner_table,owner_key,snapshot,catalog) VALUES(?,?,?,?)',(table,key,binding['snapshot'],binding['catalog']))
        db.execute('DELETE FROM lifecycle_snapshot_binding WHERE owner_table=? AND owner_key=?',(table,key))
    def release_step(self,*,limit=16,fault=lambda stage:None):
        if not 1<=limit<=64:raise ValueError('snapshot release page budget')
        self._check()
        with self.repository._connection() as db:
            task=db.execute("SELECT * FROM lifecycle_snapshot_release WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if task is None:return {'complete':True,'rows':0}
        if task['catalog']!=self.catalog_id:raise Conflict('wrong catalog for release')
        owner=self.owner_id+'/'+task['owner_table']+'/'+task['owner_key'];snapshot=task['snapshot']
        with self.catalog.write() as db:
            claim=db.execute('SELECT * FROM window_owner WHERE snapshot=?',(snapshot,)).fetchone()
            exists=db.execute('SELECT 1 FROM window_snapshot WHERE id=?',(snapshot,)).fetchone()
            if exists and (claim is None or claim['owner']!=owner):raise Conflict('snapshot release owner unknown/mismatched')
            rows=[]
            if exists:
                db.execute("UPDATE window_owner SET status='retiring' WHERE snapshot=?",(snapshot,))
                rows=db.execute('SELECT source FROM window_source WHERE snapshot=? ORDER BY source LIMIT ?',(snapshot,limit)).fetchall()
                for row in rows:db.execute('DELETE FROM window_source WHERE snapshot=? AND source=?',(snapshot,row[0]))
                remaining=db.execute('SELECT 1 FROM window_source WHERE snapshot=? LIMIT 1',(snapshot,)).fetchone()
                if not remaining:
                    db.execute('DELETE FROM window_owner WHERE snapshot=?',(snapshot,))
                    db.execute('DELETE FROM window_snapshot WHERE id=?',(snapshot,))
                done=not remaining
            else:done=True
            fault('before_snapshot_release_commit')
        fault('after_snapshot_release_commit')
        if done:
            with self.repository._write() as db:
                db.execute("UPDATE lifecycle_snapshot_release SET status='done' WHERE id=? AND status='pending'",(task['id'],))
                fault('before_snapshot_ack_commit')
            fault('after_snapshot_ack_commit')
        return {'complete':False,'rows':len(rows),'released':done}

    def retire_request_step(self,snapshot,*,proof,limit=16):
        """Exact abandoned request, explicit revocation; never infer from age.

        The snapshot ID is a random request generation. Durable job ownership
        cannot be overridden by this path. Existing unowned snapshots stay put.
        """
        import json
        if not 1<=limit<=64 or not proof.get('reason') or not proof.get('superseded_by'):raise ValueError('explicit request revocation required')
        encoded=json.dumps(proof,sort_keys=True,separators=(',',':'))
        if len(encoded.encode())>4096:raise ValueError('proof budget')
        self._check()
        with self.catalog.write() as db:
            row=db.execute('SELECT * FROM window_owner WHERE snapshot=?',(snapshot,)).fetchone()
            if row is None:
                if db.execute('SELECT 1 FROM window_snapshot WHERE id=?',(snapshot,)).fetchone():raise Conflict('unknown snapshot owner; preserve')
                return {'complete':True,'rows':0}
            if row['owner']!='request:'+snapshot:raise Conflict('durable job owns snapshot; retire job first')
            if row['status']=='retiring' and row['proof']!=encoded:raise Conflict('request revocation proof changed')
            db.execute("UPDATE window_owner SET status='retiring',proof=? WHERE snapshot=?",(encoded,snapshot))
            rows=db.execute('SELECT source FROM window_source WHERE snapshot=? ORDER BY source LIMIT ?',(snapshot,limit)).fetchall()
            for item in rows:db.execute('DELETE FROM window_source WHERE snapshot=? AND source=?',(snapshot,item[0]))
            done=not db.execute('SELECT 1 FROM window_source WHERE snapshot=? LIMIT 1',(snapshot,)).fetchone()
            if done:
                db.execute('DELETE FROM window_owner WHERE snapshot=?',(snapshot,))
                db.execute('DELETE FROM window_snapshot WHERE id=?',(snapshot,))
            return {'complete':done,'rows':len(rows)}

    def archive_release_page(self,*,after=0,limit=16):
        if not 1<=limit<=32:raise ValueError('release archive page budget')
        self._check()
        with self.repository._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM lifecycle_snapshot_release WHERE status='done' AND id>? ORDER BY id LIMIT ?",(after,limit))]

    def prune_release(self,record,*,archive_sha256,retry_closed):
        import json
        if retry_closed is not True or len(archive_sha256)!=64 or any(c not in '0123456789abcdef' for c in archive_sha256):raise ValueError('explicit archive and closed retry lifetime required')
        self._check()
        with self.repository._write() as db:
            row=db.execute('SELECT * FROM lifecycle_snapshot_release WHERE id=?',(record['id'],)).fetchone()
            if row is None:return
            if dict(row)!=record or row['status']!='done':raise Conflict('release audit changed or still pending')
            db.execute('DELETE FROM lifecycle_snapshot_release WHERE id=?',(record['id'],))
