"""Explicit durable revocation of local readers/private drafts; no time expiry."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from projection_repository import Conflict
from work_scheduler import WorkCancelled

_current=ContextVar('guardian_lifecycle_read_leases',default=())
READER_KINDS=('local-history-request','diagnostic-reader','diagnostic-analysis-reader','window-reader')
SCHEMA='''
CREATE TABLE IF NOT EXISTS lifecycle_owner(id TEXT PRIMARY KEY,nonce TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active',proof TEXT);
CREATE TABLE IF NOT EXISTS lifecycle_reservation(table_name TEXT,object_key TEXT,owner TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'held',PRIMARY KEY(table_name,object_key));
CREATE INDEX IF NOT EXISTS lifecycle_reservation_owner ON lifecycle_reservation(owner);
'''


def installed(db):return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_owner'").fetchone() is not None


def validate_current():
    for repository,token,table in _current.get():
        with repository._connection() as db:
            if not db.execute(f'SELECT 1 FROM {table} WHERE token=?',(token,)).fetchone():raise WorkCancelled('historical reader lease released')
            if installed(db):
                row=db.execute('SELECT state FROM lifecycle_owner WHERE id=?',(token,)).fetchone()
                if row is not None and row[0]!='active':raise WorkCancelled('historical reader lease fenced')


@contextmanager
def read_scope(repository,token,table='pin'):
    if table not in ('pin','metadata_reader','read_metadata_reader'):raise ValueError('unsupported lease table')
    reset=_current.set(_current.get()+((repository,token,table),))
    try:
        validate_current()
        yield
        validate_current()
    finally:_current.reset(reset)


class LeaseOwners:
    def __init__(self,repository):self.repository=repository
    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
        with self.repository._write() as db:
            for table,column,condition,kind,owner in [
                ('pin','token',"NEW.owner IN ('local-history-request','diagnostic-reader','diagnostic-analysis-reader','window-reader')",'NEW.owner','NEW.token'),
                ('draft','id','1',"'private-draft'","'draft:'||NEW.id"),
                ('metadata_reader','token','1',"'metadata-reader'",'NEW.token'),
                ('read_metadata_reader','token','1',"'metadata-reader'",'NEW.token')]:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():continue
                body=(f"INSERT OR IGNORE INTO lifecycle_owner(id,nonce,kind) SELECT {owner},lower(hex(randomblob(16))),{kind} WHERE {condition};"
                    f"INSERT OR IGNORE INTO lifecycle_reservation SELECT '{table}',json_array(NEW.{column}),{owner},'held' WHERE {condition};"
                    f"INSERT OR IGNORE INTO lifecycle_edge SELECT 'lifecycle_reservation',json_array('{table}',json_array(NEW.{column})),'{table}',json_array(NEW.{column}) WHERE {condition};")
                db.execute(f'CREATE TRIGGER IF NOT EXISTS lifecycle_reserve_{table} AFTER INSERT ON {table} BEGIN {body} END')
                db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_reserved_{table}_update BEFORE UPDATE ON {table} WHEN EXISTS(SELECT 1 FROM lifecycle_reservation r JOIN lifecycle_owner o ON o.id=r.owner WHERE r.table_name='{table}' AND r.object_key=json_array(OLD.{column}) AND o.state!='active') BEGIN SELECT RAISE(ABORT,'lifecycle owner fenced'); END")
                db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_reserved_{table}_delete BEFORE DELETE ON {table} WHEN EXISTS(SELECT 1 FROM lifecycle_reservation r JOIN lifecycle_owner o ON o.id=r.owner WHERE r.table_name='{table}' AND r.object_key=json_array(OLD.{column}) AND o.state!='active') AND NOT EXISTS(SELECT 1 FROM lifecycle_delete_permit WHERE table_name='{table}' AND object_key=json_array(OLD.{column})) BEGIN SELECT RAISE(ABORT,'lifecycle owner fenced'); END")
                db.execute(f"CREATE TRIGGER IF NOT EXISTS lifecycle_reserved_{table}_cleanup AFTER DELETE ON {table} BEGIN DELETE FROM lifecycle_edge WHERE owner_table='lifecycle_reservation' AND owner_key=json_array('{table}',json_array(OLD.{column})); DELETE FROM lifecycle_reservation WHERE table_name='{table}' AND object_key=json_array(OLD.{column}); DELETE FROM lifecycle_owner WHERE id={owner.replace('NEW.','OLD.')} AND NOT EXISTS(SELECT 1 FROM lifecycle_reservation WHERE owner={owner.replace('NEW.','OLD.')}); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS lifecycle_revoked_reference BEFORE INSERT ON lifecycle_edge WHEN NEW.owner_table!='lifecycle_reservation' AND NOT EXISTS(SELECT 1 FROM lifecycle_edge e WHERE e.owner_table=NEW.owner_table AND e.owner_key=NEW.owner_key AND e.target_table=NEW.target_table AND e.target_key=NEW.target_key) AND EXISTS(SELECT 1 FROM lifecycle_reservation r JOIN lifecycle_owner o ON o.id=r.owner WHERE r.table_name=NEW.target_table AND r.object_key=NEW.target_key AND o.state!='active') BEGIN SELECT RAISE(ABORT,'lifecycle owner fenced'); END")
    def adopt_page(self,table,*,after='',limit=16):
        # Existing reservations are not guessed dead. They remain active until
        # an explicit caller supplies revocation and the exact durable nonce.
        if table not in ('pin','draft','metadata_reader','read_metadata_reader') or not 1<=limit<=32:raise ValueError('lease adoption page budget')
        column='id' if table=='draft' else 'token'
        with self.repository._write() as db:
            rows=db.execute(f'SELECT * FROM {table} WHERE {column}>? ORDER BY {column} LIMIT ?',(after,limit)).fetchall()
            for row in rows:
                if table=='pin' and row['owner'] not in READER_KINDS:continue
                token='draft:'+row[column] if table=='draft' else row[column]
                kind=row['owner'] if table=='pin' else ('private-draft' if table=='draft' else 'metadata-reader')
                key=json.dumps([row[column]],separators=(',',':'),ensure_ascii=False)
                db.execute('INSERT OR IGNORE INTO lifecycle_owner(id,nonce,kind) VALUES(?,lower(hex(randomblob(16))),?)',(token,kind))
                db.execute('INSERT OR IGNORE INTO lifecycle_reservation VALUES(?,?,?,?)',(table,key,token,'held'))
                db.execute('INSERT OR IGNORE INTO lifecycle_edge VALUES(?,?,?,?)',('lifecycle_reservation',json.dumps([table,json.loads(key)],separators=(',',':'),ensure_ascii=False),table,key))
            return {'next':rows[-1][column] if rows else after,'complete':len(rows)<limit,'rows':len(rows)}
    def owner(self,identifier):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM lifecycle_owner WHERE id=?',(identifier,)).fetchone()
            return dict(row) if row else None
    def fence(self,identifier,*,expected_nonce,proof):
        if not isinstance(proof,dict) or not proof.get('reason') or not proof.get('superseded_by'):raise ValueError('explicit revocation proof required; no inferred timeout')
        encoded=json.dumps(proof,sort_keys=True,separators=(',',':'))
        if len(encoded.encode())>4096:raise ValueError('revocation proof budget')
        with self.repository._write() as db:
            row=db.execute('SELECT * FROM lifecycle_owner WHERE id=?',(identifier,)).fetchone()
            if row is None or row['nonce']!=expected_nonce:raise Conflict('owner generation changed or absent')
            if row['state']=='fenced':
                if row['proof']!=encoded:raise Conflict('owner revocation proof changed')
                return
            db.execute("UPDATE lifecycle_owner SET state='fenced',proof=? WHERE id=? AND nonce=?",(encoded,identifier,expected_nonce))
    def release_page(self,identifier,*,expected_nonce,limit=16):
        if not 1<=limit<=32:raise ValueError('reservation release page budget')
        with self.repository._write() as db:
            row=db.execute('SELECT state,nonce FROM lifecycle_owner WHERE id=?',(identifier,)).fetchone()
            if row is None or tuple(row)!=('fenced',expected_nonce):raise Conflict('only explicitly fenced owner may release reservations')
            rows=db.execute("SELECT table_name,object_key FROM lifecycle_reservation WHERE owner=? AND status='held' ORDER BY table_name,object_key LIMIT ?",(identifier,limit)).fetchall()
            for item in rows:
                db.execute('DELETE FROM lifecycle_edge WHERE owner_table=? AND owner_key=?',('lifecycle_reservation',json.dumps([item[0],json.loads(item[1])],separators=(',',':'),ensure_ascii=False)))
                db.execute("UPDATE lifecycle_reservation SET status='released' WHERE table_name=? AND object_key=?",tuple(item))
            return {'complete':len(rows)<limit,'rows':len(rows)}


def release_reader(repository,token,table='metadata_reader'):
    if table not in ('pin','metadata_reader','read_metadata_reader'):raise ValueError('unsupported lease table')
    key=json.dumps([token],separators=(',',':'))
    with repository._write() as db:
        managed=installed(db)
        if managed:db.execute('INSERT OR IGNORE INTO lifecycle_delete_permit VALUES(?,?)',(table,key))
        db.execute(f'DELETE FROM {table} WHERE token=?',(token,))
        if managed:db.execute('DELETE FROM lifecycle_delete_permit WHERE table_name=? AND object_key=?',(table,key))
