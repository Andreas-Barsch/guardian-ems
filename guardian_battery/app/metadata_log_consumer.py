"""Bounded real RawRegistry -> metadata revision adapter; reads only receipts.

Never registers/appends/adopts a productive source. The caller binds confirmed
sources and an explicit initial origin. Parent revisions reuse durable cursors.
"""
import json
from projection_repository import Conflict,_json
from raw_registry import ZERO,MAX_BLOCK,encoded,hashed,file_signature,RegistryCorrupt
from metadata_revision import MetadataRevision
from research_budget import check_budget

SCHEMA='''
CREATE TABLE IF NOT EXISTS metadata_registry_cursor(revision TEXT REFERENCES metadata_revision,
 registry TEXT,mapping TEXT,seq INTEGER,offset INTEGER,previous_hash TEXT,PRIMARY KEY(revision,registry));
CREATE TABLE IF NOT EXISTS metadata_registry_source(revision TEXT REFERENCES metadata_revision,
 registry TEXT,source TEXT,source_revision TEXT,receipt TEXT,block_chain TEXT,frontier INTEGER,lines INTEGER,
 PRIMARY KEY(revision,registry,source));
'''


class MetadataLogConsumer:
    def __init__(self,registry,revision):
        self.registry,self.revision=registry,revision
        self.identity=registry.config['registry_id'];self.repository=revision.repository
    def initialize(self,mapping,*,origin_proof):
        if not origin_proof or not mapping or len(mapping)>16 or any(v not in ('position','config','maintenance','retired','technical') for v in mapping.values()):raise ValueError('explicit supported source mapping and origin proof required')
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
        contract=_json({'mapping':mapping,'origin':origin_proof})
        with self.repository._write() as db:
            state=db.execute('SELECT parent,status FROM metadata_revision WHERE id=?',(self.revision.revision,)).fetchone()
            if state is None:raise Conflict('initialize revision first')
            old=db.execute('SELECT mapping FROM metadata_registry_cursor WHERE revision=? AND registry=?',(self.revision.revision,self.identity)).fetchone()
            if old:
                if old[0]!=contract:raise Conflict('registry mapping/provenance changed')
                return
            if state['status']!='preparing':raise Conflict('confirmed revision is immutable')
            parent=state['parent'];prior=None
            if parent:
                prior=db.execute('SELECT * FROM metadata_registry_cursor WHERE revision=? AND registry=?',(parent,self.identity)).fetchone()
                if prior is None or prior['mapping']!=contract:raise Conflict('parent registry origin/cursor unavailable; no old replay')
            db.execute('INSERT INTO metadata_registry_cursor VALUES(?,?,?,?,?,?)',(self.revision.revision,self.identity,contract,prior['seq'] if prior else 1,prior['offset'] if prior else 0,prior['previous_hash'] if prior else ZERO))
            if parent:
                rows=db.execute('SELECT * FROM metadata_registry_source WHERE revision=? AND registry=? ORDER BY source LIMIT 17',(parent,self.identity)).fetchall()
                if len(rows)>16:raise Conflict('source scope exceeds structural budget')
                db.executemany('INSERT INTO metadata_registry_source VALUES(?,?,?,?,?,?,?,?)',[(self.revision.revision,self.identity,r['source'],r['source_revision'],r['receipt'],r['block_chain'],r['frontier'],r['lines']) for r in rows])
    def cursor(self):
        with self.repository._connection() as db:
            row=db.execute('SELECT seq,offset,previous_hash FROM metadata_registry_cursor WHERE revision=? AND registry=?',(self.revision.revision,self.identity)).fetchone()
            if row is None:raise Conflict('metadata registry cursor absent')
            return tuple(row)
    def step(self,*,expected_cursor,max_records=16,fault=lambda stage:None):
        if not 1<=max_records<=16:raise ValueError('metadata receipt decode budget')
        if self.cursor()!=expected_cursor:raise Conflict('metadata input cursor changed')
        seq,offset,previous=expected_cursor
        page=self.registry.read_page(next_seq=seq,previous_hash=previous,max_records=1)
        if not page['records']:return {'caught_up':True,'cursor':expected_cursor,'decodes':0,'raw_bytes':0}
        receipt=page['records'][0];signature=file_signature(receipt['path'])
        if signature[:2]!=receipt['file_id'] or signature[2]<receipt['end']:raise Conflict('confirmed input file changed')
        raw=self.registry._raw_range(receipt['path'],receipt['start'],receipt['end'])
        if hashed(raw)!=receipt['block_hash'] or receipt['block_chain']!=hashed(encoded([receipt['block_previous'],receipt['start'],receipt['end'],receipt['block_hash']])):raise RegistryCorrupt('metadata input block proof mismatch')
        if not 0<=offset<=len(raw) or (offset and raw[offset-1:offset]!=b'\n') or (raw and not raw.endswith(b'\n')) or raw.count(b'\n')!=receipt['lines']:raise RegistryCorrupt('metadata input line boundary/count mismatch')
        lines=raw[offset:].splitlines(keepends=True)[:max_records]
        parsed=[];line_numbers=[];end=offset
        with self.repository._write() as db:
            row=db.execute('SELECT * FROM metadata_registry_cursor WHERE revision=? AND registry=?',(self.revision.revision,self.identity)).fetchone()
            if (row['seq'],row['offset'],row['previous_hash'])!=expected_cursor:raise Conflict('metadata input CAS changed')
            revision_state=db.execute('SELECT status,version FROM metadata_revision WHERE id=?',(self.revision.revision,)).fetchone()
            if revision_state['status']!='preparing':raise Conflict('metadata revision is already confirmed')
            mapping=json.loads(row['mapping'])['mapping']
            if receipt['source'] not in mapping:raise Conflict('unmapped metadata source; no implicit skip')
            prior=db.execute('SELECT * FROM metadata_registry_source WHERE revision=? AND registry=? AND source=?',(self.revision.revision,self.identity,receipt['source'])).fetchone()
            if (prior['receipt'] if prior else None)!=receipt['source_previous']:raise Conflict('metadata source predecessor changed')
            if receipt['mode'] in ('register','replace'):
                if raw or offset or receipt['block_previous']!=ZERO:raise Conflict('invalid source registration')
                if prior is not None:
                    if receipt['mode']!='replace' or mapping[receipt['source']]!='maintenance' or list(mapping.values()).count('maintenance')!=1:
                        raise Conflict('source replacement needs explicit origin migration')
                    if not self.revision.reset_maintenance_step(self.identity+'/'+receipt['source'],db=db):
                        return {'caught_up':False,'cursor':expected_cursor,'decodes':0,'raw_bytes':0,'status':'resetting_maintenance'}
                elif receipt['mode']=='replace':raise Conflict('maintenance replacement has no previous source')
            elif receipt['mode'] not in ('append','adopt') or prior is None or prior['source_revision']!=receipt['revision'] or prior['frontier']!=receipt['start'] or prior['block_chain']!=receipt['block_previous']:
                raise Conflict('metadata source chain mismatch')
            first_line=(prior['lines'] if prior else 0)+raw[:offset].count(b'\n')+1
            for ordinal,line in enumerate(lines):
                check_budget();end+=len(line)
                if not line.strip():continue
                parsed.append({'digest':line.decode('ascii').strip()} if mapping[receipt['source']]=='retired' else json.loads(line));line_numbers.append(first_line+ordinal)
            measured={'records':0,'node_reads':0,'node_writes':0}
            source=self.identity+'/'+receipt['source']
            if parsed:
                progress=db.execute('SELECT sequence FROM metadata_cursor WHERE revision=? AND source=?',(self.revision.revision,source)).fetchone()
                measured=self.revision.apply_page(mapping[receipt['source']],parsed,source=source,sequence=progress[0]+1 if progress else 1,
                    expected_version=revision_state['version'],provenance={'registry':self.identity,'receipt':hashed(encoded(receipt)),
                    'source_revision':receipt['revision'],'start':receipt['start']+offset,'end':receipt['start']+end,'line_numbers':line_numbers},_db=db)
            if file_signature(receipt['path'])!=signature:raise Conflict('input changed during page verification')
            done=end==len(raw)
            if done:db.execute('INSERT OR REPLACE INTO metadata_registry_source VALUES(?,?,?,?,?,?,?,?)',(self.revision.revision,self.identity,receipt['source'],receipt['revision'],hashed(encoded(receipt)),receipt['block_chain'],receipt['end'],(prior['lines'] if prior and receipt['mode'] not in ('register','replace') else 0)+receipt['lines']))
            cursor=(seq+1,0,hashed(encoded(receipt))) if done else (seq,end,previous)
            db.execute('UPDATE metadata_registry_cursor SET seq=?,offset=?,previous_hash=? WHERE revision=? AND registry=?',(*cursor,self.revision.revision,self.identity))
            fault('before_metadata_receipt_commit')
        fault('after_metadata_receipt_commit')
        return {**measured,'caught_up':done and page['caught_up'],'cursor':cursor,'decodes':len(parsed),'raw_bytes':len(raw)}
