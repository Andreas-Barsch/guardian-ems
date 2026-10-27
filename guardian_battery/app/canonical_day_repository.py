"""Explicit local composition of the real canonical engine and projection kernel.

One caller-selected physical identity epoch per stream. No factory, clock,
production path, discovery, acquisition or implicit bootstrap/repair.
"""
from datetime import date,datetime,timedelta,timezone
import json
import uuid
import hashlib
import math

from canonical_phase import (CanonicalPhaseMachine,CanonicalPhaseProjection,
    IdentityEpochResolver,clip_interval,SEMANTICS_VERSION,ALGORITHM_VERSION)
from canonical_checkpoint_storage import CountSequence,SCHEMA as COUNTS_SCHEMA,pack,unpack
from cell_window_adapter import CellWindowAdapter
from position_history import PositionSnapshot
from projection_repository import Conflict,Unavailable,MAX_ITEMS
from raw_registry import RegistryConflict,encoded,hashed

SCHEMA='''
CREATE TABLE IF NOT EXISTS canonical_stream(stream TEXT PRIMARY KEY,origin TEXT,identity TEXT,serial TEXT,parameters TEXT);
CREATE TABLE IF NOT EXISTS canonical_day_job(id TEXT PRIMARY KEY,stream TEXT,day TEXT,revision INTEGER,
 status TEXT,target_closed INTEGER,base INTEGER,predecessor INTEGER,predecessor_head TEXT,work TEXT,snapshot TEXT,checkpoint TEXT,after_key TEXT,
 row_number INTEGER,inputs TEXT,captures TEXT,prepared TEXT,bundle INTEGER,version INTEGER DEFAULT 0,
 UNIQUE(stream,day,revision));
CREATE INDEX IF NOT EXISTS canonical_day_lookup ON canonical_day_job(stream,day,revision DESC);
CREATE TABLE IF NOT EXISTS canonical_publication_dependency(dependency INTEGER,bundle INTEGER,stream TEXT,day TEXT,PRIMARY KEY(dependency,bundle));
CREATE TABLE IF NOT EXISTS canonical_epoch_job(change INTEGER,dependency INTEGER,correction TEXT,status TEXT,PRIMARY KEY(change,dependency));
CREATE TABLE IF NOT EXISTS canonical_correction_done(correction TEXT,day TEXT,job TEXT,old_bundle INTEGER,PRIMARY KEY(correction,day));
CREATE TABLE IF NOT EXISTS canonical_correction_request(id TEXT PRIMARY KEY,stream TEXT,day TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS canonical_correction(id TEXT PRIMARY KEY,stream TEXT,next_day TEXT,reason TEXT,finished INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS canonical_correction_stream ON canonical_correction(stream,finished,next_day);
'''


def bounds(day):
    start=datetime.combine(date.fromisoformat(day),datetime.min.time(),timezone.utc).timestamp()
    return start,start+86400


def previous(day):return (date.fromisoformat(day)-timedelta(days=1)).isoformat()
def following(day):return (date.fromisoformat(day)+timedelta(days=1)).isoformat()


class CanonicalDayRepository:
    def __init__(self,repository,catalog,epochs):
        self.repository=repository;self.catalog=catalog;self.epochs=epochs;self.reader=CellWindowAdapter(catalog)

    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+COUNTS_SCHEMA+SCHEMA+'COMMIT;')

    def define_stream(self,stream,*,origin_day,identity_epoch_id,physical_serial,parameters=None):
        bounds(origin_day)
        if not all(isinstance(v,str) and 0<len(v)<256 for v in (stream,identity_epoch_id)):raise ValueError('explicit bounded identity required')
        if physical_serial is None:
            if identity_epoch_id not in {f'unresolved-position-{i}' for i in range(1,7)}:raise ValueError('explicit existing unresolved-position identity required')
        elif not isinstance(physical_serial,str) or not 0<len(physical_serial)<256:raise ValueError('invalid physical serial')
        values=(stream,origin_day,identity_epoch_id,physical_serial,encoded(parameters or {}).decode())
        with self.repository._write() as db:
            old=db.execute('SELECT * FROM canonical_stream WHERE stream=?',(stream,)).fetchone()
            if old and tuple(old)!=values:raise Conflict('stream definition immutable')
            db.execute('INSERT OR IGNORE INTO canonical_stream VALUES(?,?,?,?,?)',values)

    def _stream(self,stream):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM canonical_stream WHERE stream=?',(stream,)).fetchone()
            if row is None:raise Unavailable('canonical stream absent')
            return dict(row)

    @staticmethod
    def _head(stream,day):return 'canonical:'+stream+':'+day
    @staticmethod
    def _product(stream,day):return 'canonical:'+stream+':'+day

    def _job(self,job):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM canonical_day_job WHERE id=?',(job,)).fetchone()
            if row is None:raise Unavailable('canonical job absent')
            return dict(row)

    def state(self,stream,day):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM canonical_day_job WHERE stream=? AND day=? ORDER BY revision DESC LIMIT 1',(stream,day)).fetchone()
            return None if row is None else dict(row)

    def published_state(self,stream,day,bundle=None):
        bundle=self.repository.head(self._head(stream,day)) if bundle is None else bundle
        if bundle is None:return None
        page=self.repository.read(bundle,self._product(stream,day),day,after_key='Y',limit=2)
        rows=dict(page['rows'])
        return {'bundle':bundle,'metadata':page['metadata'],'checkpoint':rows['Zstate']}

    def begin_day(self,stream,day,*,bootstrap=False,correction=False,_correction=None,identity_predecessor=None):
        definition=self._stream(stream);start,end=bounds(day)
        existing=self.state(stream,day)
        if existing and existing['status'] not in ('published_closed','stale'):
            return {'job':existing['id'],'status':existing['status']}
        base=self.repository.head(self._head(stream,day))
        if base is not None and not correction:return {'job':existing['id'],'status':'published_closed','bundle':base}
        prior=self.published_state(stream,previous(day))
        if prior is None or prior['metadata']['status']!='published_closed':
            if not bootstrap or day!=definition['origin']:return {'status':'awaiting_predecessor','day':day}
            identity_state=None;predecessor=None;predecessor_head=None
            if identity_predecessor is not None:
                seed_stream,seed_bundle=identity_predecessor
                if self._stream(seed_stream)['serial']!=definition['serial']:raise Conflict('identity predecessor serial differs')
                seed=self.published_state(seed_stream,previous(day),seed_bundle)
                if seed is None or seed['metadata']['status']!='published_closed':raise Conflict('identity predecessor is not closed')
                identity_state=seed['checkpoint']['identity_state'];predecessor=seed_bundle
                predecessor_head=self._head(seed_stream,previous(day))
                if self.repository.head(predecessor_head)!=seed_bundle:raise Conflict('identity predecessor is no longer current')
            else:
                with self.epochs.connection() as db:
                    first=db.execute("SELECT MIN(stamp) FROM epoch WHERE kind='position'").fetchone()[0]
                if definition['serial'] is not None and first is not None and first<start:return {'status':'awaiting_identity_origin','day':day}
            checkpoint={'engine':None,'identity_state':identity_state,'processed':0,'input_hash':'0'*64}
        else:
            checkpoint=json.loads(json.dumps(prior['checkpoint']))
            checkpoint['processed']=0;checkpoint['input_hash']='0'*64
            predecessor=prior['bundle'];predecessor_head=self._head(stream,previous(day))
        captures={k:self.epochs.capture_window(k,start,end) for k in ('config','position')}
        snapshot=self.reader.open(start,end)
        work=self.repository.begin(self._head(stream,day),expected_bundle=base)
        revision=1 if existing is None else existing['revision']+1
        target_closed=int(correction and self.published_state(stream,day,base)['metadata']['status']=='published_closed')
        if correction:self.repository.begin_replacement(work,self._product(stream,day),day,metadata={'status':'correcting'})
        job=uuid.uuid4().hex
        with self.repository._write() as db:
            db.execute('INSERT INTO canonical_day_job(id,stream,day,revision,status,target_closed,base,predecessor,predecessor_head,work,snapshot,checkpoint,after_key,row_number,inputs,captures,prepared) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (job,stream,day,revision,'correcting' if correction else 'open',target_closed,base,predecessor,predecessor_head,work,snapshot,
                 encoded(checkpoint).decode(),None,0,'{}',encoded(captures).decode(),None))
            if _correction is not None:
                db.execute('INSERT INTO canonical_correction_done VALUES(?,?,?,?)',(_correction,day,job,base))
        return {'job':job,'status':'correcting' if correction else 'open'}

    def request_close(self,job):
        row=self._job(job)
        if row['status']=='published_closed':return
        if row['prepared'] is not None:raise Conflict('finish prepared page before requesting closure')
        # Explicitly freeze the latest indexed/confirmed prefix, not a wall-clock
        # assertion that all future measurements or external sources are known.
        snap=self.reader.open(*bounds(row['day']))
        with self.repository._write() as db:
            changed=db.execute("UPDATE canonical_day_job SET status='closing_requested',target_closed=1,snapshot=?,version=version+1 WHERE id=? AND version=?",(snap,job,row['version'])).rowcount
            if changed!=1:raise Conflict('canonical job changed')
        self.reader.close(row['snapshot'])

    def _check_late(self,row,refs):
        """Reject known newly indexed records behind an already consumed sort key."""
        if row['after_key'] is None:return
        after=tuple(json.loads(row['after_key']));inputs=json.loads(row['inputs']);start,end=bounds(row['day'])
        # Bounded SQL budget and source prefix set come from the frozen resolver.
        with self.catalog.connection() as db:
            members=db.execute('SELECT source,revision,prefix_end FROM window_source WHERE snapshot=? LIMIT 257',(row['snapshot'],)).fetchall()
            if len(members)>256:raise Conflict('too many canonical sources')
            for member in members:
                old=inputs.get(encoded([member['source'],member['revision']]).decode(),{}).get('end',0)
                rows,_=self.reader.resolver._budgeted(db,
                    'SELECT day,stamp,source,revision,offset FROM entry WHERE source=? AND revision=? AND offset>=? AND stamp>=? AND stamp<? AND (day,stamp,source,revision,offset)<=(?,?,?,?,?) LIMIT 1',
                    (member['source'],member['revision'],old,start,end,*after),50000)
                if rows:raise Conflict('late indexed record requires explicit day correction')

    def step(self,job,*,limit=32,fault=lambda _:None):
        if not 1<=limit<=128:raise ValueError('canonical step limit must be 1..128')
        row=self._job(job)
        if row['status']=='published_closed':return {'status':'published_closed','bundle':row['bundle'],'raw_bytes':0,'decodes':0}
        if row['prepared'] is not None:
            result=self._commit_prepared(row,fault)
            return dict(result,raw_bytes=0,decodes=0,replayed_prepared_page=True)
        definition=self._stream(row['stream']);captures=json.loads(row['captures'])
        self._check_late(row,None)
        after=None if row['after_key'] is None else tuple(json.loads(row['after_key']))
        refs=self.reader.resolver.plan_snapshot(row['snapshot'],after=after,limit=limit)['refs']
        refmap={(r.source_id,r.revision,r.start):r for r in refs}
        page=self.reader.page(row['snapshot'],after=after,limit=limit)
        if page.get('budget_blocked'):return {'status':'budget_blocked','raw_bytes':0,'decodes':0}
        finalize=row['status'] in ('closing_requested','correcting') and page['complete']
        closing=bool(finalize and row['target_closed'])
        if not page['records'] and not finalize and row['bundle'] is not None:return {'status':'caught_up','raw_bytes':0,'decodes':0}
        start,end=bounds(row['day']);inputs=json.loads(row['inputs'])
        resolver=IdentityEpochResolver();resolver.snapshots=[PositionSnapshot.from_dict(v) for v in captures['position']['records']]
        with self.repository._write() as db:
            latest=db.execute('SELECT version FROM canonical_day_job WHERE id=?',(job,)).fetchone()[0]
            if latest!=row['version']:raise Conflict('canonical job changed')
            checkpoint=unpack(db,json.loads(row['checkpoint']))
            factory=lambda values:CountSequence.from_values(db,values)
            engine=(CanonicalPhaseMachine.restore(checkpoint['engine'],parameters=json.loads(definition['parameters']),timestamp_sequence_factory=factory)
                    if checkpoint['engine'] else CanonicalPhaseMachine(definition['identity'],definition['serial'],parameters=json.loads(definition['parameters']),timestamp_sequence_factory=factory))
            changes={};number=row['row_number'];identity_state=checkpoint['identity_state']
            for record in page['records']:
                key=encoded([record['source_id'],record['revision']]).decode()
                # Source cursor is the complete physical indexed record end, not
                # a timestamp or an inferred filename-day frontier.
                source_ref=refmap[(record['source_id'],record['revision'],record['byte_offset'])]
                prior=inputs.get(key,{});inputs[key]={'source':record['source_id'],'revision':record['revision'],'end':max(prior.get('end',0),source_ref.end),'frontier':source_ref.prefix_end,'namespace':record['namespace']}
                checkpoint['input_hash']=hashed(encoded([checkpoint['input_hash'],record['source_id'],record['revision'],record['byte_offset'],record['record_hash']]))
                if record['module_serial']!=definition['serial']:continue
                sample={'schema_version':1,'timestamp':record['timestamp'],'module':record['module'],'module_serial':record['module_serial'],**record['measurements']}
                identity,serial,identity_state=resolver.resolve_with_checkpoint(sample,record['timestamp'],identity_state)
                if identity!=definition['identity']:continue  # Explicit separate physical occupancy epoch stream.
                cfg=None
                for candidate in captures['config']['records']:
                    if datetime.fromisoformat(candidate['timestamp']).timestamp()<=record['timestamp']:cfg=candidate
                engine.process(sample,None if cfg is None else cfg['parameters'],config_id=None if cfg is None else cfg['config_id'],
                    source_cursor={'source_day':record['source_id'],'source_offset':record['byte_offset']},
                    source_signature={'source_id':record['source_id'],'revision':record['revision'],'record_hash':record['record_hash']})
                checkpoint['processed']+=1
            for kind,items in [('diagnostic',engine.final_diagnostic),('visual',engine.final_visual),('endpoint',engine.endpoints)]:
                for item in items:
                    number+=1;changes[f'I{number:012d}']={'kind':kind,'value':pack(item)}
            engine.final_diagnostic=[];engine.final_visual=[];engine.endpoints=[]
            pending={'diagnostic_intervals':pack([engine.pending_diagnostic] if engine.pending_diagnostic else []),'visual_intervals':pack(engine.visual_segments)}
            if closing:
                _,_,identity_state=resolver.resolve_with_checkpoint({'module_serial':definition['serial'],'module':engine.current_position or 1},math.nextafter(end,-math.inf),identity_state)
                CanonicalPhaseProjection._clear_daily_counts(engine)
            checkpoint.update(engine=pack(engine.snapshot()),identity_state=identity_state)
            metadata={'status':'published_closed' if closing else 'open','day':row['day'],'revision':row['revision'],
                'identity':definition['identity'],'semantics_version':SEMANTICS_VERSION,'algorithm_version':ALGORITHM_VERSION,
                'processed':checkpoint['processed'],'coverage':page['coverage'],'pending_sources':page['pending_sources'],
                'invalid_records':page['invalid_records'],'input_hash':checkpoint['input_hash'],
                'epoch_vector':{k:v['fingerprint'] for k,v in captures.items()},'state_hash':hashed(encoded({'engine':pack(checkpoint['engine']),'identity_state':identity_state}))}
            changes['Zpending']=pending;changes['Zstate']=pack(checkpoint)
            prepared={'changes':changes,'metadata':metadata,'closing':closing,'finalize':finalize,'raw_bytes':page['raw_bytes'],'decodes':page['decodes']}
            new_after=list(source_ref.key) if page['records'] else after
            # Use the last returned row even when it belongs to another selected identity.
            if page['records']:
                last=page['records'][-1]
                new_after=[math.floor(last['timestamp']/86400),last['timestamp'],last['source_id'],last['revision'],last['byte_offset']]
            db.execute('UPDATE canonical_day_job SET checkpoint=?,after_key=?,row_number=?,inputs=?,prepared=?,version=version+1 WHERE id=?',
                (encoded(pack(checkpoint)).decode(),None if new_after is None else encoded(new_after).decode(),number,encoded(inputs).decode(),encoded(prepared).decode(),job))
            fault('after_private_checkpoint')
        fault('after_private_commit')
        return self._commit_prepared(self._job(job),fault)

    def _commit_prepared(self,row,fault):
        prepared=json.loads(row['prepared']);day=row['day'];product=self._product(row['stream'],day);work=row['work']
        captures=json.loads(row['captures']);deps=[]
        for kind,capture in captures.items():
            deps.append(self.epochs.register_dependency(work,kind,capture['start'],capture['end'],context={'stream':row['stream'],'day':day},expected_fingerprint=capture['fingerprint']))
        for source in json.loads(row['inputs']).values():
            with self.repository._connection() as db:
                prior=db.execute('SELECT durable_end FROM source_revision WHERE source=? AND revision=?',(source['source'],source['revision'])).fetchone()
            if prior is None:self.repository.register_source(source['source'],source['revision'],metadata={'namespace':source['namespace']},durable_end=source['frontier'])
            elif prior[0]<source['frontier']:self.repository.confirm_source(source['source'],source['revision'],expected_end=prior[0],durable_end=source['frontier'])
            old=self.repository.cursor(product,source['source'],source['revision'])
            self.repository.stage_cursor(work,product,source['source'],source['revision'],expected=old,next_offset=max(source['end'],old.offset if old else 0))
        with self.repository._connection() as db:
            replacement=db.execute('SELECT 1 FROM replacement_work WHERE draft_id=?',(work,)).fetchone() is not None
        if replacement:
            rows={k:v for k,v in prepared['changes'].items() if k.startswith('I') or prepared['finalize']}
            if rows:
                progress=self.repository.replacement_progress(work,product,day)
                if progress.last_key<max(rows):self.repository.stage_replacement(work,product,day,rows,expected=progress)
            if not prepared['finalize']:
                with self.repository._write() as db:db.execute('UPDATE canonical_day_job SET prepared=NULL,version=version+1 WHERE id=? AND version=?',(row['id'],row['version']))
                return {'status':'correcting','raw_bytes':prepared['raw_bytes'],'decodes':prepared['decodes']}
            progress=self.repository.replacement_progress(work,product,day)
            if not progress.sealed:self.repository.seal_replacement(work,product,day,expected=progress)
            with self.repository._write() as db:db.execute('UPDATE draft_partition SET metadata=? WHERE draft_id=? AND product=? AND partition_key=?',(encoded(prepared['metadata']).decode(),work,product,day))
        else:self.repository.stage(work,product,day,prepared['changes'],metadata=prepared['metadata'])
        fault('before_epoch_guard')
        with self.epochs.publication_guard(deps):
            with self.catalog.write() as source_db:
                self.reader.resolver._snapshot(source_db,row['snapshot'])
                with self.repository._write() as db:
                    current=db.execute('SELECT version,prepared FROM canonical_day_job WHERE id=?',(row['id'],)).fetchone()
                    if current['version']!=row['version'] or current['prepared']!=row['prepared']:raise Conflict('prepared canonical cursor changed')
                    if row['predecessor'] is not None:
                        prior=db.execute('SELECT bundle_id FROM head WHERE name=?',(row['predecessor_head'],)).fetchone()
                        if prior is None or prior[0]!=row['predecessor']:raise Conflict('predecessor publication changed; explicit stale/retry required')
                    bundle=self.repository._publish(db,work,requires=(() if row['predecessor'] is None else (row['predecessor'],)),fault=fault)
                    db.execute('INSERT INTO pin VALUES(?,?,?)',(uuid.uuid4().hex,bundle,'canonical revision '+row['id']))
                    db.executemany('INSERT INTO canonical_publication_dependency VALUES(?,?,?,?)',[(dep,bundle,row['stream'],day) for dep in deps])
                    status='published_closed' if prepared['closing'] else ('open' if replacement else row['status'])
                    nextwork=None
                    if not prepared['closing']:
                        nextwork=uuid.uuid4().hex;db.execute('INSERT INTO draft VALUES(?,?,?)',(nextwork,self._head(row['stream'],day),bundle))
                    db.execute('UPDATE canonical_day_job SET status=?,bundle=?,base=?,work=?,prepared=NULL,version=version+1 WHERE id=?',(status,bundle,bundle,nextwork,row['id']))
                    fault('after_canonical_cursor')
        fault('after_canonical_commit')
        return {'status':status,'bundle':bundle,'raw_bytes':prepared['raw_bytes'],'decodes':prepared['decodes']}

    def refresh(self,job):
        row=self._job(job)
        if row['status']!='open' or row['prepared'] is not None:raise Conflict('only idle open job can refresh')
        snapshot=self.reader.open(*bounds(row['day']))
        with self.repository._write() as db:
            if db.execute('UPDATE canonical_day_job SET snapshot=?,version=version+1 WHERE id=? AND version=?',(snapshot,job,row['version'])).rowcount!=1:
                raise Conflict('canonical job changed')
        self.reader.close(row['snapshot'])

    def invalidate(self,job):
        """Explicit stale/retry; discard only private work, never a publication."""
        row=self._job(job)
        with self.repository._write() as db:
            if db.execute("UPDATE canonical_day_job SET status='stale',version=version+1 WHERE id=? AND version=?",(job,row['version'])).rowcount!=1:
                raise Conflict('canonical job changed')
            if row['work'] is not None:db.execute('DELETE FROM draft WHERE id=?',(row['work'],))

    def request_correction(self,stream,day,*,reason,request_id=None):
        if not isinstance(reason,str) or not reason or len(reason)>512:raise ValueError('explicit bounded correction reason required')
        self._stream(stream);bounds(day)
        if request_id is not None:
            if not isinstance(request_id,str) or str(uuid.UUID(request_id))!=request_id:raise ValueError('canonical correction request id must be a UUID')
            with self.repository._connection() as db:
                old=db.execute('SELECT * FROM canonical_correction WHERE id=?',(request_id,)).fetchone()
                if old:
                    proof=db.execute('SELECT stream,day,reason FROM canonical_correction_request WHERE id=?',(request_id,)).fetchone()
                    if proof is None or tuple(proof)!=(stream,day,reason):raise Conflict('correction request identity reused')
                    return request_id
        current=self.state(stream,day)
        if current is None or current['status']!='published_closed':raise Unavailable('correction needs a closed publication')
        identifier=request_id or uuid.uuid4().hex
        with self.repository._write() as db:
            db.execute('INSERT INTO canonical_correction VALUES(?,?,?,?,0)',(identifier,stream,day,reason))
            if request_id is not None:db.execute('INSERT INTO canonical_correction_request VALUES(?,?,?,?)',(request_id,stream,day,reason))
        return identifier

    def correction_step(self,identifier,*,limit=32,fault=lambda _:None):
        with self.repository._connection() as db:
            value=db.execute('SELECT * FROM canonical_correction WHERE id=?',(identifier,)).fetchone()
            if value is None:raise Unavailable('correction absent')
            correction=dict(value)
        if correction['finished']:return {'status':'converged_or_frontier','raw_bytes':0,'decodes':0}
        stream,day=correction['stream'],correction['next_day']
        state=self.state(stream,day)
        if state is None:
            with self.repository._write() as db:db.execute('UPDATE canonical_correction SET finished=1 WHERE id=?',(identifier,))
            return {'status':'confirmed_frontier','raw_bytes':0,'decodes':0}
        with self.repository._connection() as db:
            marker=db.execute('SELECT * FROM canonical_correction_done WHERE correction=? AND day=?',(identifier,day)).fetchone()
        if marker is None:
            if state['status'] not in ('published_closed','open','stale'):
                raise Conflict('successor already has another private correction')
            if state['status']=='open':self.invalidate(state['id'])
            begin=self.begin_day(stream,day,bootstrap=day==self._stream(stream)['origin'],correction=True,_correction=identifier)
            if begin['status'].startswith('awaiting'):return begin
            state=self._job(begin['job'])
        else:
            state=self._job(marker['job'])
            if state['bundle'] is not None and state['bundle']!=marker['old_bundle'] and state['status'] in ('published_closed','open'):
                return self._advance_correction(correction,fault)
        result=self.step(state['id'],limit=limit,fault=fault)
        if result['status'] in ('published_closed','open'):
            advanced=self._advance_correction(correction,fault);return dict(result,correction_status=advanced['status'])
        return result

    def _advance_correction(self,correction,fault):
        stream,day=correction['stream'],correction['next_day']
        with self.repository._connection() as db:
            marker=db.execute('SELECT * FROM canonical_correction_done WHERE correction=? AND day=?',(correction['id'],day)).fetchone()
        if marker is None:raise Conflict('correction provenance absent')
        current=self.published_state(stream,day);old=self.published_state(stream,day,marker['old_bundle'])
        a,b=current['metadata'],old['metadata']
        converged=all(a[k]==b[k] for k in ('state_hash','input_hash','epoch_vector'))
        open_frontier=a['status']=='open'
        with self.repository._write() as db:
            if db.execute('UPDATE canonical_correction SET next_day=?,finished=? WHERE id=? AND next_day=?',
                          (following(day),int(converged or open_frontier),correction['id'],day)).rowcount!=1:raise Conflict('correction cursor changed')
            fault('after_correction_cursor')
        fault('after_correction_commit')
        return {'status':'confirmed_open_frontier' if open_frontier else ('converged' if converged else 'continue_successor'),'day':day}

    def read_page(self,stream,day,*,bundle=None,after_key='',limit=64):
        """Bounded published rows; interval timestamp ropes remain descriptors."""
        bundle=self.repository.head(self._head(stream,day)) if bundle is None else bundle
        if bundle is None:raise Unavailable('day publication absent')
        page=self.repository.read(bundle,self._product(stream,day),day,after_key=after_key,limit=limit)
        page['validity']=self.publication_validity(stream,day,bundle,page['metadata'])
        return page

    def clipped(self,interval,day):
        """Existing canonical clipping/count semantics with half-open day counts."""
        start,end=bounds(day)
        with self.repository._connection() as db:
            return clip_interval(unpack(db,interval),start,math.nextafter(end,-math.inf))

    def enqueue_epoch_job(self,change,dependency):
        """Explicit bounded bridge from a real EpochStore job to a published day.

        Unpublished or superseded dependency reservations cannot create a false
        current-publication invalidation. Replaying the same job is idempotent.
        """
        with self.epochs.connection() as db:
            event=db.execute('SELECT * FROM job WHERE change=? AND dependency=?',(change,dependency)).fetchone()
            if event is None:raise Unavailable('epoch invalidation job absent')
        with self.repository._write() as db:
            old=db.execute('SELECT * FROM canonical_epoch_job WHERE change=? AND dependency=?',(change,dependency)).fetchone()
            if old:return dict(old)
            published=db.execute('SELECT p.* FROM canonical_publication_dependency p JOIN head h ON h.bundle_id=p.bundle WHERE p.dependency=? LIMIT 2',(dependency,)).fetchall()
            if len(published)>1:raise Conflict('ambiguous publication dependency')
            identifier=None;status='superseded_or_unpublished'
            if published:
                p=published[0];current=db.execute('SELECT status FROM canonical_day_job WHERE stream=? AND day=? ORDER BY revision DESC LIMIT 1',(p['stream'],p['day'])).fetchone()
                if current['status']!='published_closed':raise Conflict('open day requires explicit stale/retry before correction')
                identifier=uuid.uuid4().hex;status='queued'
                db.execute('INSERT INTO canonical_correction VALUES(?,?,?,?,0)',(identifier,p['stream'],p['day'],'epoch change '+str(change)))
            db.execute('INSERT INTO canonical_epoch_job VALUES(?,?,?,?)',(change,dependency,identifier,status))
            return {'change':change,'dependency':dependency,'correction':identifier,'status':status}

    def publication_validity(self,stream,day,bundle,metadata):
        """Label provenance/current dependency status; never claim latest Raw coverage."""
        with self.repository._connection() as db:
            pending=db.execute('SELECT 1 FROM canonical_correction WHERE stream=? AND finished=0 AND next_day<=? LIMIT 1',(stream,day)).fetchone() is not None
        start,end=bounds(day)
        with self.epochs.connection() as epoch_db:
            epoch_db.execute('BEGIN')
            changed=any(hashed(encoded(self.epochs._vector(epoch_db,kind,start,end)))!=fingerprint for kind,fingerprint in metadata['epoch_vector'].items())
        current_bundle=self.repository.head(self._head(stream,day))
        historical=bundle!=current_bundle
        # Appends and closure retain the same canonical job revision. A
        # completed replacement starts a new revision, even after its pending
        # correction marker has cleared. Read the published head, not private
        # work, so an unfinished replacement cannot masquerade as complete.
        replaced=False
        if historical and current_bundle is not None:
            current=self.published_state(stream,day,current_bundle)
            replaced=current['metadata']['revision']!=metadata['revision']
        return {'historical_revision':historical,
                'stale_dependencies':changed or pending or replaced,'correction_pending':pending,
                'replaced_revision':replaced,
                'raw_coverage':'as_of_frozen_confirmed_inputs; latest_raw_not_asserted'}
