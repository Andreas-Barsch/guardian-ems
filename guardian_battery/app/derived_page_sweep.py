"""Explicit bounded orphan proposals, never GC from a read or source deletion.

The sealed scan frontier is an exact maintenance scope. Each proposal is still
fenced/validated by StoreRetirement. Later writes cannot resurrect fenced pages.
"""
import json
from projection_repository import Conflict,_json
from store_retirement import StoreRetirement,row_hash,fingerprint

SCHEMA='''
CREATE TABLE IF NOT EXISTS lifecycle_sweep(plan TEXT PRIMARY KEY REFERENCES lifecycle_plan(id) ON DELETE CASCADE,table_name TEXT NOT NULL,ceiling INTEGER NOT NULL,after_id INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0);
'''


class DerivedPageSweep:
    def __init__(self,repository):self.repository=repository;self.life=StoreRetirement(repository)
    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
    def begin(self,identifier,table,*,policy):
        if table not in ('canonical_counts','metadata_node'):raise ValueError('derived immutable pages only')
        self.life.begin(identifier,policy=policy)
        with self.life._write() as db:
            plan=self.life._plan(db,identifier)
            old=db.execute('SELECT table_name FROM lifecycle_sweep WHERE plan=?',(identifier,)).fetchone()
            if old:
                if old[0]!=table:raise Conflict('sweep scope changed')
                return
            if plan['phase']!='selecting':raise Conflict('sweep already sealed')
            ceiling=db.execute(f'SELECT id FROM {table} ORDER BY id DESC LIMIT 1').fetchone()
            db.execute('INSERT INTO lifecycle_sweep(plan,table_name,ceiling) VALUES(?,?,?)',(identifier,table,ceiling[0] if ceiling else 0))
    def step(self,identifier,*,limit=16,fault=lambda stage:None):
        if not 1<=limit<=32:raise ValueError('sweep page budget')
        with self.life._write() as db:
            plan=self.life._plan(db,identifier)
            state=db.execute('SELECT * FROM lifecycle_sweep WHERE plan=?',(identifier,)).fetchone()
            if state is None:raise Conflict('sweep absent')
            if state['complete']:return {'complete':True,'examined':0,'proposed':0}
            if plan['phase']!='selecting':raise Conflict('selection already sealed')
            table=state['table_name']
            rows=db.execute(f'SELECT rowid AS _rowid,* FROM {table} WHERE id>? AND id<=? ORDER BY id LIMIT ?',(state['after_id'],state['ceiling'],limit)).fetchall()
            proposed=0
            for row in rows:
                key=_json([row['id']])
                if db.execute('SELECT 1 FROM lifecycle_edge WHERE target_table=? AND target_key=? LIMIT 1',(table,key)).fetchone():continue
                db.execute('INSERT OR IGNORE INTO lifecycle_candidate(plan,table_name,object_key,row_hash) VALUES(?,?,?,?)',(identifier,table,key,row_hash(row)));proposed+=1
            db.execute('UPDATE lifecycle_sweep SET after_id=?,complete=? WHERE plan=?',(rows[-1]['id'] if rows else state['after_id'],int(len(rows)<limit),identifier))
            fault('before_sweep_commit')
        fault('after_sweep_commit')
        return {'complete':len(rows)<limit,'examined':len(rows),'proposed':proposed}
    def close(self,identifier,*,proof,limit=16):
        # Snapshot of this manifest belongs in the externally archived plan.
        with self.repository._connection() as db:
            state=db.execute('SELECT complete FROM lifecycle_sweep WHERE plan=?',(identifier,)).fetchone()
            if state is not None and not state[0]:raise Conflict('sweep incomplete')
        result=self.life.prune_audit_step(identifier,proof=proof,limit=limit)
        return result
