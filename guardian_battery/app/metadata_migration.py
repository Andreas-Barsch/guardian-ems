"""Explicit resumable preparation of immutable metadata inputs; never a GET.

Only caller-selected frozen JSONL inputs are read. No discovery, defaults,
legacy-cache trust or productive migration. Publication is all-or-nothing.
"""
import hashlib
import json
from pathlib import Path
from position_history import PositionSnapshot
from projection_repository import Conflict, _json
from read_metadata_index import PreparedMetadata, stamp
from research_identity import ResearchIdentityResolver
from research_budget import check_budget
from timeline import TechnicalEventSource, TimelineEvent

SEMANTICS = 'prepared-metadata-v1/research-identity-v1/timeline-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata_migration(
 revision TEXT PRIMARY KEY, contract TEXT NOT NULL, phase TEXT NOT NULL,
 source INTEGER NOT NULL DEFAULT 0, offset INTEGER NOT NULL DEFAULT 0,
 line INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
 after_stamp REAL NOT NULL DEFAULT -1e300, after_priority TEXT NOT NULL DEFAULT '',
 previous TEXT NOT NULL DEFAULT '{}', ordinal INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS metadata_input(
 revision TEXT, ordinal INTEGER, manifest TEXT NOT NULL, chain TEXT NOT NULL,
 PRIMARY KEY(revision,ordinal));
CREATE TABLE IF NOT EXISTS metadata_position_stage(
 revision TEXT, stamp REAL, priority TEXT, payload TEXT NOT NULL,
 PRIMARY KEY(revision,stamp,priority));
CREATE TABLE IF NOT EXISTS metadata_page_proof(
 revision TEXT, source INTEGER, offset INTEGER, end_offset INTEGER,
 digest TEXT NOT NULL, PRIMARY KEY(revision,source,offset));
'''


def signature(path):
    value = Path(path).stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


class MetadataMigration:
    """One bounded input page or one bounded ordered projection page per step.

    Full scope (including explicit empty inputs) and authoritative origin must
    be supplied. Frozen source changes invalidate this job, never silently
    reinterpret already consumed rows. A completed job resumes in O(1).
    """
    def __init__(self, repository, revision):
        self.repository, self.revision = repository, revision

    def initialize(self):
        PreparedMetadata(self.repository, self.revision).initialize()
        with self.repository._connection() as db:
            db.executescript('BEGIN IMMEDIATE;' + SCHEMA + 'COMMIT;')

    def begin(self, sources, *, provenance, semantics=SEMANTICS):
        if semantics != SEMANTICS or not provenance or not 0 <= len(sources) <= 16:
            raise ValueError('explicit supported contract and bounded scope required')
        inputs = []
        for source in sources:
            if source['kind'] not in ('position', 'technical', 'timeline_dto'):
                raise ValueError('unsupported input; no inferred legacy conversion')
            path = str(Path(source['path']).resolve())
            if not source.get('origin'):
                raise ValueError('confirmed origin required; never infer an epoch origin')
            inputs.append(dict(source, path=path, signature=signature(path)))
        contract = _json({'semantics': semantics, 'provenance': provenance, 'inputs': inputs})
        with self.repository._write() as db:
            old = db.execute('SELECT contract FROM metadata_migration WHERE revision=?', (self.revision,)).fetchone()
            if old:
                if old[0] != contract: raise Conflict('migration contract changed')
                return
            meta = db.execute('SELECT * FROM read_metadata WHERE revision=?', (self.revision,)).fetchone()
            if meta['identity_input'] or meta['timeline_input'] or meta['identity_ready'] or meta['timeline_ready']:
                raise Conflict('metadata revision already claimed')
            db.execute('INSERT INTO metadata_migration(revision,contract,phase) VALUES(?,?,?)', (self.revision,contract,'ingest'))
            for i, source in enumerate(inputs):
                db.execute('INSERT INTO metadata_input VALUES(?,?,?,?)', (self.revision,i,_json(source),'0'*64))
            digest = hashlib.sha256(contract.encode()).hexdigest()
            db.execute('UPDATE read_metadata SET identity_input=?,timeline_input=? WHERE revision=?', (digest,digest,self.revision))

    def status(self):
        with self.repository._connection() as db:
            row = db.execute('SELECT * FROM metadata_migration WHERE revision=?', (self.revision,)).fetchone()
            if row is None: raise Conflict('migration not begun')
            return dict(row)

    def step(self, *, expected_version, rows=16, byte_limit=65536, fault=lambda stage: None):
        if not 1 <= rows <= 64 or not 1 <= byte_limit <= 65536:
            raise ValueError('structural page limits exceeded')
        state = self.status()
        if state['version'] != expected_version: raise Conflict('migration CAS changed')
        if state['phase'] == 'complete': return {'complete':True,'rows':0,'bytes':0,'version':state['version']}
        check_budget()
        page=[]; used=0; next_offset=state['offset']; line=state['line']; source=None
        if state['phase']=='ingest':
            with self.repository._connection() as db:
                source=db.execute('SELECT * FROM metadata_input WHERE revision=? AND ordinal=?',(self.revision,state['source'])).fetchone()
            if source:
                manifest=json.loads(source['manifest']); path=Path(manifest['path'])
                if signature(path)!=manifest['signature']: raise Conflict('frozen metadata source changed')
                with path.open('rb') as stream:
                    stream.seek(state['offset'])
                    for _ in range(rows):
                        start=stream.tell(); raw=stream.readline(byte_limit-used+1)
                        if not raw: break
                        if len(raw)>byte_limit-used:
                            if not page: raise ValueError('metadata record exceeds page byte budget')
                            break
                        if not raw.endswith(b'\n'): raise ValueError('unconfirmed partial metadata record')
                        used+=len(raw);next_offset=stream.tell();line+=1
                        if not raw.strip():
                            page.append((start,next_offset,hashlib.sha256(raw).hexdigest(),None))
                            continue
                        item=json.loads(raw)
                        if manifest['kind']=='position': item=PositionSnapshot.from_dict(item).to_dict()
                        elif manifest['kind']=='technical': item=TechnicalEventSource._project(item,line).to_dict()
                        else: item=TimelineEvent(**item).to_dict()
                        page.append((start,next_offset,hashlib.sha256(raw).hexdigest(),item))
                        check_budget()
                if signature(path)!=manifest['signature']: raise Conflict('metadata changed during page read')
        with self.repository._write() as db:
            current=db.execute('SELECT version FROM metadata_migration WHERE revision=?',(self.revision,)).fetchone()[0]
            if current!=expected_version: raise Conflict('migration CAS changed')
            if state['phase']=='ingest':
                if source is None:
                    db.execute("UPDATE metadata_migration SET phase='project' WHERE revision=?",(self.revision,))
                else:
                    chain=source['chain']
                    for start,end,digest,item in page:
                        chain=hashlib.sha256((chain+digest).encode()).hexdigest()
                        db.execute('INSERT INTO metadata_page_proof VALUES(?,?,?,?,?)',(self.revision,state['source'],start,end,digest))
                        if item is None:continue
                        payload=_json(item)
                        if manifest['kind']=='position':
                            priority=item['created_at']+'|'+item['position_history_id']
                            db.execute('INSERT INTO metadata_position_stage VALUES(?,?,?,?)',(self.revision,stamp(item['effective_at']),priority,payload))
                        else:
                            db.execute('INSERT OR IGNORE INTO read_timeline VALUES(?,?,?,?)',(self.revision,stamp(item['timestamp']),hashlib.sha256(payload.encode()).hexdigest(),payload))
                    db.execute('UPDATE metadata_input SET chain=? WHERE revision=? AND ordinal=?',(chain,self.revision,state['source']))
                    done=next_offset==manifest['signature'][2]
                    db.execute('UPDATE metadata_migration SET source=?,offset=?,line=? WHERE revision=?',
                        (state['source']+int(done),0 if done else next_offset,0 if done else line,self.revision))
            else:
                page=db.execute('SELECT * FROM metadata_position_stage WHERE revision=? AND (stamp,priority)>(?,?) ORDER BY stamp,priority LIMIT ?',
                    (self.revision,state['after_stamp'],state['after_priority'],rows)).fetchall()
                previous=json.loads(state['previous']);ordinal=state['ordinal']
                for row in page:
                    check_budget();item=json.loads(row['payload']);positions=item['positions']
                    for serial in sorted(set(previous.values())|set(positions.values()),key=lambda s:s or ''):
                        if serial is None: continue
                        old=next((int(k) for k,v in previous.items() if v==serial),None)
                        new=next((int(k) for k,v in positions.items() if v==serial),None)
                        prior=db.execute('SELECT * FROM read_identity_epoch WHERE revision=? AND serial=? ORDER BY stamp DESC, rowid DESC LIMIT 1',(self.revision,serial)).fetchone()
                        if prior and old==new: continue
                        if prior:
                            payload=json.loads(prior['payload']);payload['valid_to']=item['effective_at']
                            db.execute('UPDATE read_identity_epoch SET payload=? WHERE revision=? AND serial=? AND stamp=? AND identity=?',(_json(payload),self.revision,serial,prior['stamp'],prior['identity']))
                        epoch={'physical_serial':serial,'position':new,'valid_from':item['effective_at'],'valid_to':None,'position_history_id':item['position_history_id'],
                            'identity_epoch_id':ResearchIdentityResolver._epoch_id(serial,item['effective_at'],new)}
                        ordinal+=1
                        db.execute('INSERT OR IGNORE INTO read_serial VALUES(?,?)',(self.revision,serial))
                        db.execute('INSERT INTO read_identity_epoch VALUES(?,?,?,?,?)',(self.revision,serial,row['stamp'],f'{ordinal:020d}:'+epoch['identity_epoch_id'],_json(epoch)))
                    topology={'timestamp':item['effective_at'],'position_history_id':item['position_history_id'],'positions':[]}
                    for pos,serial in positions.items():
                        epoch=db.execute('SELECT payload FROM read_identity_epoch WHERE revision=? AND serial=? ORDER BY stamp DESC,rowid DESC LIMIT 1',(self.revision,serial)).fetchone() if serial else None
                        topology['positions'].append({'position':int(pos),'physical_serial':serial,'resolved':serial is not None,'position_history_id':item['position_history_id'],'identity_epoch_id':json.loads(epoch[0])['identity_epoch_id'] if epoch else None})
                    db.execute('INSERT OR REPLACE INTO read_topology VALUES(?,?,?)',(self.revision,row['stamp'],_json(topology)))
                    db.execute('INSERT INTO read_position VALUES(?,?,?,?)',(self.revision,row['stamp'],row['priority'],row['payload']))
                    previous=positions
                if page:
                    db.execute('UPDATE metadata_migration SET after_stamp=?,after_priority=?,previous=?,ordinal=? WHERE revision=?',(page[-1]['stamp'],page[-1]['priority'],_json(previous),ordinal,self.revision))
                else:
                    # Freeze validation is bounded by the explicit <=16 source scope.
                    for row in db.execute('SELECT manifest FROM metadata_input WHERE revision=?',(self.revision,)):
                        source_manifest=json.loads(row[0])
                        if signature(source_manifest['path'])!=source_manifest['signature']:raise Conflict('metadata changed before publication')
                    db.execute('UPDATE read_metadata SET identity_ready=1,timeline_ready=1 WHERE revision=?',(self.revision,))
                    db.execute("UPDATE metadata_migration SET phase='complete' WHERE revision=?",(self.revision,))
            db.execute('UPDATE metadata_migration SET version=version+1 WHERE revision=?',(self.revision,))
            fault('before_page_commit')
        fault('after_page_commit')
        return {'complete':self.status()['phase']=='complete','rows':len(page),'bytes':used,'version':expected_version+1}
