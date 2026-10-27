"""Explicit local durable preparation queue; not selected by production startup.

Each step performs one bounded engine/publication page. No worker is started by
construction or status. Persisted jobs, never an in-memory live replay queue, own
catchup. A confirmed UTC frontier is an explicit input, not the machine clock.
"""
import json
import os
import fcntl
import uuid
from datetime import datetime, timezone
from pathlib import Path
from canonical_day_repository import CanonicalDayRepository,bounds,following,previous
from display_day_repository import DisplayDayRepository
from projection_repository import ProjectionRepository,Conflict,Unavailable
from source_catalog import SourceCatalog
from history_epoch_adapter import EpochStore
from legacy_source_inventory import LegacySourceInventory
from raw_registry import encoded,RawRegistry
from raw_registry_catalog import RegistryCatalogConsumer
from research_budget import check_budget,ResearchQueryError
from diagnostic_startup_io import checkpoint

SCHEMA='''
CREATE TABLE IF NOT EXISTS preparation_state(id INTEGER PRIMARY KEY,paused INTEGER,frontier TEXT,proof TEXT,epoch_after INTEGER DEFAULT 0);
INSERT OR IGNORE INTO preparation_state(id,paused) VALUES(1,0);
CREATE TABLE IF NOT EXISTS preparation_job(id INTEGER PRIMARY KEY AUTOINCREMENT,token TEXT UNIQUE,kind TEXT,status TEXT,payload TEXT,error TEXT);
CREATE INDEX IF NOT EXISTS preparation_queue ON preparation_job(status,id);
CREATE TABLE IF NOT EXISTS preparation_stream(stream TEXT PRIMARY KEY,next_day TEXT);
CREATE TABLE IF NOT EXISTS preparation_watch(id INTEGER PRIMARY KEY,root TEXT);
CREATE TABLE IF NOT EXISTS preparation_display(day TEXT PRIMARY KEY,status TEXT);
'''

class RepositoryPreparation:
    def __init__(self,repository,catalog,epochs,canonical,display):
        self.repository=repository;self.catalog=catalog;self.epochs=epochs
        self.canonical=canonical;self.display=display;self.inventory=LegacySourceInventory(catalog)

    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')

    def _enqueue(self,db,kind,payload,token):
        if len(encoded(payload))>60000:raise ValueError('preparation request budget')
        db.execute("INSERT OR IGNORE INTO preparation_job(token,kind,status,payload) VALUES(?,?,'pending',?)",(token,kind,encoded(payload).decode()))
        return db.execute('SELECT id FROM preparation_job WHERE token=?',(token,)).fetchone()[0]

    def enroll(self,stream):
        definition=self.canonical._stream(stream)
        with self.repository._write() as db:
            db.execute('INSERT OR IGNORE INTO preparation_stream VALUES(?,?)',(stream,definition['origin']))
        return self.status()

    def confirm_frontier(self,day,*,proof):
        """Caller-confirmed day, never a completeness assertion for raw coverage.

        The current day stays open. Earlier days close using frozen indexed input.
        Repeating the same proof is idempotent; a new proof requests bounded deltas.
        """
        bounds(day)
        if not isinstance(proof,dict) or not proof or len(encoded(proof))>4096:raise ValueError('explicit bounded frontier provenance required')
        token=encoded([day,proof]).decode()
        with self.repository._write() as db:
            old=db.execute('SELECT frontier FROM preparation_state WHERE id=1').fetchone()[0]
            if old is not None and day<old:raise Conflict('frontier cannot move backwards')
            db.execute('UPDATE preparation_state SET frontier=?,proof=? WHERE id=1',(day,encoded(proof).decode()))
            self._enqueue(db,'dispatch',{'after':'','frontier':day},'frontier:'+token)

    def watch_registry(self,root):
        registry=RawRegistry(root);consumer=RegistryCatalogConsumer(registry,self.catalog);consumer.initialize()
        with self.repository._write() as db:
            old=db.execute('SELECT root FROM preparation_watch WHERE id=1').fetchone()
            if old and old[0]!=str(Path(root)):raise Conflict('registry watch immutable')
            db.execute('INSERT OR IGNORE INTO preparation_watch VALUES(1,?)',(str(Path(root)),))

    def _poll_registry(self):
        with self.repository._connection() as db:watch=db.execute('SELECT root FROM preparation_watch WHERE id=1').fetchone()
        if watch is None:return False
        registry=RawRegistry(watch[0]);consumer=RegistryCatalogConsumer(registry,self.catalog)
        cursor=consumer.cursor();page=registry.read_page(next_seq=cursor[0],previous_hash=cursor[1],max_records=1)
        if not page['records']:return False
        receipt=page['records'][0]
        with self.repository._write() as db:self._enqueue(db,'registry',{'root':watch[0],'receipt':receipt,'phase':'consume','cursor':list(cursor),'after':receipt['start']},'registry:'+registry.config['registry_id']+':'+str(receipt['seq']))
        return True

    def request_correction(self,stream,day,*,reason,token):
        self.canonical._stream(stream);bounds(day)
        with self.repository._write() as db:return self._enqueue(db,'correction',{'stream':stream,'day':day,'reason':reason},'correction:'+token)

    def request_inventory(self,roots):
        inventory=self.inventory.create(roots)
        with self.repository._write() as db:return self._enqueue(db,'inventory',{'inventory':inventory,'phase':'discover','after':0,'cursor':0},'inventory:'+inventory)

    def request_epoch_dispatch(self):
        # One durable job resumes at the saved change cursor. It is explicitly
        # resubmitted by the local composition after confirmed epoch ingestion.
        with self.repository._write() as db:return self._enqueue(db,'epochs',{},'epochs:'+uuid.uuid4().hex)

    def pause(self):
        with self.repository._write() as db:db.execute('UPDATE preparation_state SET paused=1 WHERE id=1')

    def resume(self):
        with self.repository._write() as db:db.execute('UPDATE preparation_state SET paused=0 WHERE id=1')

    def retry(self,job):
        with self.repository._write() as db:
            if db.execute("UPDATE preparation_job SET status='pending',error=NULL WHERE id=? AND status='blocked'",(job,)).rowcount!=1:raise Conflict('job is not blocked')

    def status(self):
        # Two small indexed reads; no source enumeration, raw read or rebuild.
        with self.repository._connection() as db:
            state=dict(db.execute('SELECT * FROM preparation_state WHERE id=1').fetchone())
            job=db.execute("SELECT id,kind,status,error FROM preparation_job WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            blocked=db.execute("SELECT id,kind,status,error FROM preparation_job WHERE status='blocked' ORDER BY id LIMIT 1").fetchone()
        return {'state':'paused' if state['paused'] else ('pending' if job else 'blocked' if blocked else 'caught_up'),
                'frontier':state['frontier'],'job':None if job is None else dict(job),
                'blocked':None if blocked is None else dict(blocked),
                'coverage':'confirmed indexed inputs; undiscovered sources remain unknown',
                'legacy_diagnostic_cache_adopted':False}

    def step(self,*,fault=lambda _:None):
        check_budget();checkpoint()
        lock_path=self.repository.path.with_name(self.repository.path.name+'.preparation.lock')
        descriptor=os.open(lock_path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'r+b') as handle:
            try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'status':'busy','raw_bytes':0,'decodes':0}
            try:return self._locked_step(fault=fault)
            finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def _locked_step(self,*,fault=lambda _:None):
        check_budget();checkpoint()
        with self.repository._connection() as db:
            state=dict(db.execute('SELECT * FROM preparation_state WHERE id=1').fetchone())
            if state['paused']:return {'status':'paused','raw_bytes':0,'decodes':0}
            row=db.execute("SELECT * FROM preparation_job WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            changes=self.epochs.changes(after=state['epoch_after'],limit=1)
            if changes:
                with self.repository._write() as db:self._enqueue(db,'epochs',{},'auto-epochs:'+str(changes[0]['id']))
            self._poll_registry()
            return dict(self.status(),raw_bytes=0,decodes=0)
        row=dict(row);payload=json.loads(row['payload'])
        try:result=self._step(row,payload,state,fault)
        except ResearchQueryError as exc:
            if exc.code not in ('cancelled','timeout'):raise
            self.pause()
            return {'status':'paused','reason':exc.code,'job':row['id']}
        except (Conflict,Unavailable,ValueError) as exc:
            with self.repository._write() as db:db.execute("UPDATE preparation_job SET status='blocked',error=? WHERE id=?",(str(exc)[:1024],row['id']))
            return {'status':'blocked','reason':str(exc),'job':row['id']}
        return result

    def _save(self,row,payload,*,done=False,fault=lambda _:None,epoch_after=None):
        with self.repository._write() as db:
            if db.execute('UPDATE preparation_job SET payload=?,status=? WHERE id=? AND payload=? AND status=?',
                          (encoded(payload).decode(),'done' if done else 'pending',row['id'],row['payload'],'pending')).rowcount!=1:raise Conflict('job cursor changed')
            if epoch_after is not None:db.execute('UPDATE preparation_state SET epoch_after=? WHERE id=1',(epoch_after,))
            fault('after_preparation_cursor')
        fault('after_preparation_commit')

    def _dispatch_inventory(self,row,p,page,fault):
        """Replayable cross-store handoff; queue work and its ACK commit together.

        The immutable catalog observation is the authority, not the manifest's
        stat-derived candidate revision. No source receipt/frontier is advanced.
        """
        sources=[item for item in page if item['item']['kind']=='source']
        observations=(self.catalog.inventory_observations(p['inventory'],after_entry=p['after'],limit=32) if sources else [])
        bindings={o['entry_id']:o for o in observations}
        actions=[]
        for item in sources:
            binding=bindings.get(item['id']);candidate=item['item']['registration']
            if (binding is None or binding['source']!=candidate['source'] or
                    binding['observed_revision']!=candidate['revision']):
                raise Conflict('committed inventory binding missing or mismatched')
            outcome=binding['outcome']
            if outcome not in ('registered','known_prefix','known_prefix_unconfirmed_tail','needs_receipt_or_revision'):
                raise Conflict('unsupported inventory binding outcome')
            suffix=p['inventory']+':'+str(item['id'])
            if outcome!='needs_receipt_or_revision':
                actions.append(('catalog_index',{'source':binding['source'],'revision':binding['catalog_revision'],
                    'binding':binding,'phase':'index','after':0},'legacy-index:'+suffix,False))
            if outcome in ('needs_receipt_or_revision','known_prefix_unconfirmed_tail'):
                actions.append(('inventory_decision',{'binding':binding},'legacy-decision:'+suffix,True))
        next_payload={**p,'cursor':p['cursor']+len(page),'after':page[-1]['id']}
        fault('before_inventory_enqueue')
        with self.repository._write() as db:
            for kind,payload,token,blocked in actions:
                job=self._enqueue(db,kind,payload,token)
                if blocked:
                    db.execute("UPDATE preparation_job SET status='blocked',error=? WHERE id=? AND status='pending'",
                               (payload['binding']['outcome']+': explicit source decision required',job))
            fault('after_inventory_enqueue')
            fault('before_inventory_queue_cursor')
            if db.execute("UPDATE preparation_job SET payload=? WHERE id=? AND payload=? AND status='pending'",
                          (encoded(next_payload).decode(),row['id'],row['payload'])).rowcount!=1:
                raise Conflict('inventory dispatch cursor changed')
            fault('after_preparation_cursor')
        fault('after_inventory_dispatch_commit')
        fault('after_preparation_commit')
        return {'status':'more','raw_bytes':0,'decodes':0,'dispatched_items':len(page),'jobs':len(actions)}

    def _step(self,row,p,state,fault):
        kind=row['kind'];result={'status':'more','raw_bytes':0,'decodes':0}
        if kind=='dispatch':
            with self.repository._write() as db:
                stream=db.execute('SELECT c.stream,COALESCE(p.next_day,c.origin) AS next_day FROM canonical_stream c LEFT JOIN preparation_stream p ON p.stream=c.stream WHERE c.stream>? ORDER BY c.stream LIMIT 1',(p['after'],)).fetchone()
                if stream:
                    db.execute('INSERT OR IGNORE INTO preparation_stream VALUES(?,?)',(stream['stream'],stream['next_day']))
                    self._enqueue(db,'canonical',{'stream':stream['stream'],'day':stream['next_day'],'frontier':p['frontier']},'range:'+row['token']+':'+stream['stream'])
                    p['after']=stream['stream']
            self._save(row,p,done=stream is None,fault=fault)
        elif kind=='canonical':
            stream,day=p['stream'],p['day'];definition=self.canonical._stream(stream)
            if day>p['frontier']:
                self._save(row,p,done=True,fault=fault);return result
            current=self.canonical.state(stream,day)
            if current is None or current['status']=='stale':
                begin=self.canonical.begin_day(stream,day,bootstrap=day==definition['origin'])
                if begin['status'].startswith('awaiting'):raise Unavailable(begin['status'])
                current=self.canonical._job(begin['job'])
            if current['status']=='open' and not p.get('refreshed'):
                if current['prepared'] is None:self.canonical.refresh(current['id']);p['refreshed']=True
            if day<p['frontier'] and current['status']=='open' and current['prepared'] is None:self.canonical.request_close(current['id'])
            result=self.canonical.step(current['id'],limit=32,fault=fault)
            complete=result['status']=='published_closed';caught=result['status']=='caught_up'
            if complete or caught:
                with self.repository._write() as db:
                    db.execute("INSERT INTO preparation_display VALUES(?,'pending') ON CONFLICT(day) DO UPDATE SET status='pending'",(day,))
                    db.execute('UPDATE preparation_stream SET next_day=? WHERE stream=?',(following(day) if complete else day,stream))
                    self._enqueue(db,'display',{'day':day},'display:'+row['token']+':'+day)
                if complete:p.update(day=following(day));p.pop('refreshed',None)
            self._save(row,p,done=caught or p['day']>p['frontier'],fault=fault)
        elif kind=='display':
            day=p['day']
            with self.repository._connection() as db:
                streams=[r[0] for r in db.execute('SELECT p.stream FROM preparation_stream p JOIN canonical_stream c ON c.stream=p.stream WHERE c.origin<=? ORDER BY p.stream LIMIT 65',(day,))]
            if len(streams)>64:raise ValueError('display dependency set budget')
            current=self.display.state(day)
            if current is None:self.display.begin(day,streams)
            elif current['status'] in ('caught_up','published_closed'):
                old=self.display.read_page(day,limit=1)
                if old['validity']['stale_dependencies'] or old['validity'].get('newer_canonical_available',False):
                    if current['status']=='caught_up':
                        try:self.display.refresh(day,streams)
                        except Conflict:self.display.begin(day,streams,correction=True)
                    else:self.display.begin(day,streams,correction=True)
                else:
                    self._save(row,p,done=True,fault=fault);return result
            # At most eight scalar channels from one already bounded record.
            # One durable publication amortizes the commit; checkpoints between
            # channels preserve cancellation and Raw priority. No second record
            # is read in this turn, and the original bucket math is unchanged.
            result=self.display.step(day,fault=fault,max_channels=8)
            done=result['status'] in ('caught_up','published_closed')
            self._save(row,p,done=done,fault=fault)
        elif kind=='correction':
            current=self.canonical.state(p['stream'],p['day'])
            if 'open_job' in p or ('correction' not in p and current and current['status']!='published_closed'):
                if 'open_job' not in p:
                    if current['status']!='correcting':self.canonical.invalidate(current['id'])
                    begin=self.canonical.begin_day(p['stream'],p['day'],bootstrap=p['day']==self.canonical._stream(p['stream'])['origin'],correction=current['base'] is not None)
                    if begin['status'].startswith('awaiting'):raise Unavailable(begin['status'])
                    p['open_job']=begin['job'];self._save(row,p,fault=fault);return result
                result=self.canonical.step(p['open_job'],limit=32,fault=fault)
                done=result['status'] in ('open','caught_up')
                if done:
                    with self.repository._write() as db:self._enqueue(db,'display',{'day':p['day']},'display-open-correction:'+row['token'])
                self._save(row,p,done=done,fault=fault);return result
            if 'correction' not in p:
                p['correction']=self.canonical.request_correction(p['stream'],p['day'],reason=p['reason'],request_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'preparation:'+row['token'])))
                self._save(row,p,fault=fault);return result
            with self.repository._connection() as db:
                day=db.execute('SELECT next_day FROM canonical_correction WHERE id=?',(p['correction'],)).fetchone()[0]
            result=self.canonical.correction_step(p['correction'],fault=fault)
            if result.get('bundle'):
                with self.repository._write() as db:self._enqueue(db,'display',{'day':day},'display-correction:'+str(result['bundle']))
            done=result.get('correction_status') in ('converged','confirmed_open_frontier') or result['status'] in ('confirmed_frontier','converged_or_frontier')
            self._save(row,p,done=done,fault=fault)
        elif kind=='inventory':
            if p['phase']=='discover':
                result=self.inventory.step(p['inventory'],max_entries=32,fault=fault)
                if result['status']=='complete':p['phase']='ingest'
                elif result['status']!='private':raise Conflict('legacy inventory '+result['status'])
            else:
                migration='inventory:'+p['inventory'];progress=self.catalog.migration_progress(migration)
                # Catalog acknowledgement is not a dispatch acknowledgement.
                # Replay every committed but unacknowledged item before ingesting
                # another page, including when the catalog marked the manifest done.
                cursor=0 if progress is None else progress['cursor']
                lag=cursor-p['cursor']
                if lag<0:raise Conflict('inventory catalog cursor moved backwards')
                if lag:
                    page=self.inventory.page(p['inventory'],after_id=p['after'],limit=min(lag,32))
                    if len(page)!=min(lag,32):raise Conflict('inventory dispatch evidence missing')
                    return self._dispatch_inventory(row,p,page,fault)
                if progress and progress['finished']:
                    self._save(row,p,done=True,fault=fault);return result
                page=self.inventory.page(p['inventory'],after_id=p['after'],limit=32)
                self.catalog.ingest_page(migration,expected_cursor=cursor,items=[r['item'] for r in page],finish=not page,fault=fault)
                if page:return self._dispatch_inventory(row,p,page,fault)
                self._save(row,p,done=True,fault=fault);return result
            self._save(row,p,done=p['phase']=='ingest' and result.get('finished',False),fault=fault)
        elif kind=='revision':
            with self.catalog.connection() as db:
                entry=db.execute('SELECT day,end_offset FROM entry WHERE source=? AND revision=? AND offset>=? ORDER BY offset LIMIT 1',(p['source'],p['revision'],p['after'])).fetchone()
            if entry is None:self._save(row,p,done=True,fault=fault);return result
            day=datetime.fromtimestamp(entry['day']*86400,timezone.utc).date().isoformat()
            with self.repository._connection() as db:
                streams=[r[0] for r in db.execute('SELECT stream FROM canonical_stream ORDER BY stream LIMIT 65')]
            if len(streams)>64:raise ValueError('identity dispatch budget exceeded')
            for stream in streams:
                if self.canonical.published_state(stream,day) is not None:
                    self.request_correction(stream,day,reason='confirmed source revision',token=row['token']+':'+stream+':'+day)
            p['after']=entry['end_offset'];self._save(row,p,fault=fault)
        elif kind=='inventory_decision':
            raise Unavailable(p['binding']['outcome']+': explicit source decision and fresh inventory observation required')
        elif kind=='catalog_index':
            source,revision=p['source'],p['revision']
            binding=p.get('binding')
            if binding is None:raise Unavailable('index job lacks committed observation binding; fresh inventory required')
            target=binding['frontier']
            with self.catalog.connection() as db:
                active=db.execute('SELECT revision FROM active WHERE source=?',(source,)).fetchone()
                proof=db.execute('SELECT frontier FROM receipt WHERE source=? AND revision=? AND receipt=?',(source,revision,binding['receipt'])).fetchone()
            if active is None or active[0]!=revision:raise Conflict('inventory binding superseded; fresh inventory required')
            if proof is None or proof[0]!=target:raise Conflict('inventory binding receipt/frontier mismatch')
            if p['phase']=='index':
                progress=self.catalog.progress(source,revision)
                if progress['cursor']<target:
                    result=self.catalog.index_step(source,revision,expected_cursor=progress['cursor'],max_records=32,max_bytes=min(65536,target-progress['cursor']),fault=fault)
                else:p['phase']='frontier'
            else:
                with self.catalog.connection() as db:
                    entries=db.execute('SELECT day,end_offset FROM entry WHERE source=? AND revision=? AND offset>=? AND end_offset<=? ORDER BY offset LIMIT 32',(source,revision,p['after'],target)).fetchall()
                if entries:
                    for entry in entries:
                        day=datetime.fromtimestamp(entry['day']*86400,timezone.utc).date().isoformat()
                        p['frontier']=max(day,p.get('frontier',day))
                    p['after']=entries[-1]['end_offset']
                else:
                    if p.get('frontier'):
                        self.confirm_frontier(max(state['frontier'] or p['frontier'],p['frontier']),proof={'source':source,'revision':revision,'receipt':binding['receipt'],'confirmed_frontier':target,'inventory':binding['inventory'],'entry_id':binding['entry_id'],'outcome':binding['outcome'],'provenance':'legacy_observation_not_writer_fsync'})
                    self._save(row,p,done=True,fault=fault);return result
            self._save(row,p,fault=fault)
        elif kind=='registry':
            receipt=p['receipt'];source,revision=receipt['source'],receipt['revision']
            registry=RawRegistry(p['root']);consumer=RegistryCatalogConsumer(registry,self.catalog)
            if p['phase']=='consume':
                current=consumer.cursor()
                if current==tuple(p['cursor']):result=consumer.step(expected_cursor=current,max_records=1,fault=fault)
                elif current[0]!=p['cursor'][0]+1:raise Conflict('registry consumer ownership changed')
                if receipt['mode']=='register' and receipt.get('previous_revision'):
                    with self.repository._write() as db:self._enqueue(db,'revision',{'source':source,'revision':receipt['previous_revision'],'after':0},'revision:'+registry.config['registry_id']+':'+str(receipt['seq']))
                p['phase']='index'
            elif p['phase']=='index':
                from confirmed_prefix_reader import ConfirmedPrefixReader
                progress=self.catalog.progress(source,revision)
                if progress['cursor']<receipt['end']:
                    result=self.catalog.index_step(source,revision,expected_cursor=progress['cursor'],max_records=32,fault=fault,prefix_reader=ConfirmedPrefixReader(registry))
                else:p['phase']='dispatch'
            else:
                with self.catalog.connection() as db:
                    rows=db.execute('SELECT * FROM entry WHERE source=? AND revision=? AND offset>=? AND end_offset<=? ORDER BY offset LIMIT 1',(source,revision,p['after'],receipt['end'])).fetchall()
                if not rows:
                    if p.get('frontier'):
                        self.confirm_frontier(max(p['frontier'],state['frontier'] or p['frontier']),proof={'registry':registry.config['registry_id'],'receipt':receipt['seq']})
                    self._save(row,p,done=True,fault=fault);return result
                entry=rows[0];day=datetime.fromtimestamp(entry['stamp'],timezone.utc).date().isoformat()
                p['frontier']=max(day,p.get('frontier',day));p['after']=entry['end_offset']
                # All existing identity streams see the source vector. Do not
                # silently choose a new physical identity when origin is absent.
                with self.repository._connection() as db:
                    streams=[dict(r) for r in db.execute('SELECT * FROM canonical_stream ORDER BY stream LIMIT 65')]
                if len(streams)>64:raise ValueError('identity dispatch budget exceeded')
                serial=json.loads(entry['identity']).get('module_serial')
                if not any(v['serial']==serial and (serial is not None or v['identity']==f"unresolved-position-{entry['module']}") for v in streams):raise Unavailable('awaiting explicit physical identity origin for '+str(serial))
                for stream in streams:
                    published=self.canonical.published_state(stream['stream'],day)
                    current=self.canonical.state(stream['stream'],day)
                    late=False
                    if current and current['after_key']:
                        late=(entry['day'],entry['stamp'],source,revision,entry['offset'])<=tuple(json.loads(current['after_key']))
                    if published and (published['metadata']['status']=='published_closed' or late):
                        self.request_correction(stream['stream'],day,reason='confirmed source append',token=registry.config['registry_id']+':'+str(receipt['seq'])+':'+stream['stream']+':'+day)
            self._save(row,p,fault=fault)
        elif kind=='epochs':
            changes=self.epochs.changes(after=state['epoch_after'],limit=1)
            if not changes:self._save(row,p,done=True,fault=fault);return result
            change=changes[0]
            if not change['finished']:
                self.epochs.plan_jobs(change['id'],expected_cursor=(change['day'],change['after_dependency']),limit=32,fault=fault)
                return result
            after=p.get('after_dependency',0);jobs=self.epochs.jobs(change=change['id'],after=after,limit=1)
            if jobs:
                j=jobs[0]
                try:dispatch=self.canonical.enqueue_epoch_job(j['change'],j['dependency'])
                except Conflict as exc:
                    if 'open day requires' not in str(exc):raise
                    with self.repository._connection() as db:
                        publication=db.execute('SELECT p.* FROM canonical_publication_dependency p JOIN head h ON h.bundle_id=p.bundle WHERE p.dependency=?',(j['dependency'],)).fetchone()
                    if publication is None:raise
                    self.request_correction(publication['stream'],publication['day'],reason='epoch change',token='open-epoch:'+str(j['change'])+':'+str(j['dependency']))
                    dispatch={}

                if dispatch.get('correction'):
                    with self.repository._write() as db:
                        c=db.execute('SELECT * FROM canonical_correction WHERE id=?',(dispatch['correction'],)).fetchone()
                        self._enqueue(db,'correction',{'stream':c['stream'],'day':c['next_day'],'correction':c['id']},'epoch-correction:'+c['id'])
                p['after_dependency']=j['dependency']
            else:
                p.pop('after_dependency',None)
                self._save(row,p,epoch_after=change['id'],fault=fault)
                return result
            self._save(row,p,fault=fault)
        else:raise ValueError('unknown preparation kind')
        return result


def local_composition(*,repository_path,catalog_path,epoch_path,initialize=False):
    """Explicit test/local paths only; no defaults, thread, Raw writes or main hook."""
    paths=[Path(p) for p in (repository_path,catalog_path,epoch_path)]
    if any(not p.is_absolute() for p in paths) or len(set(paths))!=3:raise ValueError('three distinct explicit absolute paths required')
    if not initialize and any(not p.is_file() for p in paths):raise Unavailable('persisted repository checkpoint missing; explicit initialization required')
    repository=ProjectionRepository(paths[0]);catalog=SourceCatalog(paths[1]);epochs=EpochStore(paths[2])
    canonical=CanonicalDayRepository(repository,catalog,epochs);display=DisplayDayRepository(repository,catalog,canonical)
    preparation=RepositoryPreparation(repository,catalog,epochs,canonical,display)
    if initialize:canonical.initialize();display.initialize();preparation.initialize()
    return preparation
