"""Explicit local display composition; no discovery, clock or runtime activation.

Uses the existing display bucket/coverage semantics. Only observed cell inputs
are supported by this adapter; other source kinds require an explicit adapter.
"""
import json
import math
import statistics
import uuid
from datetime import datetime, timezone
from canonical_day_repository import bounds
from cell_window_adapter import CellWindowAdapter
from display_history_projection import (_channels, _timestamp, _channel_key,
    _bucket_start, _new_bucket, _add, _public, RESOLUTIONS,
    DISPLAY_PROJECTION_SCHEMA_VERSION, AGGREGATION_ALGORITHM_VERSION)
from projection_repository import Conflict, Unavailable
from raw_registry import encoded, hashed
from research_budget import check_budget

SCHEMA='''
CREATE TABLE IF NOT EXISTS display_job(id TEXT PRIMARY KEY,day TEXT,base INTEGER,
 snapshot TEXT,after_key TEXT,input_hash TEXT,orders INTEGER,status TEXT,
 canonical TEXT,prepared TEXT,pending TEXT,version INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS display_job_day ON display_job(day,id);
CREATE TABLE IF NOT EXISTS display_current(day TEXT PRIMARY KEY,job TEXT);
CREATE TABLE IF NOT EXISTS display_bucket(job TEXT,key TEXT,payload TEXT,PRIMARY KEY(job,key));
CREATE TABLE IF NOT EXISTS display_replacement(job TEXT PRIMARY KEY,work TEXT,metadata TEXT);
CREATE TABLE IF NOT EXISTS display_cadence(job TEXT,key TEXT,payload TEXT,PRIMARY KEY(job,key));
'''

class DisplayDayRepository:
    def __init__(self,repository,catalog,canonical):
        self.repository=repository;self.catalog=catalog;self.canonical=canonical
        self.reader=CellWindowAdapter(catalog)

    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')

    @staticmethod
    def head(day):return 'display:'+day
    @staticmethod
    def product(day):return 'display:'+day

    def state(self,day):
        with self.repository._connection() as db:
            row=db.execute('SELECT j.* FROM display_current c JOIN display_job j ON j.id=c.job WHERE c.day=?',(day,)).fetchone()
            return None if row is None else dict(row)

    def begin(self,day,streams,*,correction=False):
        """Explicit bounded canonical dependency set, already at selected prefix."""
        bounds(day)
        if not 1<=len(streams)<=64 or len(set(streams))!=len(streams):raise ValueError('1..64 distinct canonical streams required')
        dependencies={}
        for stream in streams:
            state=self.canonical.published_state(stream,day)
            if state is None:raise Unavailable('awaiting canonical publication')
            valid=self.canonical.publication_validity(stream,day,state['bundle'],state['metadata'])
            if valid['stale_dependencies']:raise Conflict('canonical dependency stale')
            dependencies[stream]={'bundle':state['bundle'],'input_hash':state['metadata']['input_hash'],
                                  'status':state['metadata']['status'],'epochs':state['metadata']['epoch_vector'],'processed':state['metadata']['processed']}
        if len({encoded(x['epochs']) for x in dependencies.values()})!=1:raise Conflict('canonical epoch vectors differ')
        if len({x['input_hash'] for x in dependencies.values()})!=1:raise Conflict('canonical streams have different confirmed prefixes')
        current=self.state(day)
        if current and not correction:
            if current['status'] not in ('caught_up','published_closed'):return current
            if json.loads(current['canonical'])==dependencies:return current
            raise Conflict('canonical revision changed: explicit correction or append refresh required')
        snapshot=self.reader.open(*bounds(day));identifier=uuid.uuid4().hex
        base=self.repository.head(self.head(day))
        with self.repository._write() as db:
            db.execute('INSERT INTO display_job(id,day,base,snapshot,input_hash,orders,status,canonical) VALUES(?,?,?,?,?,?,?,?)',
                       (identifier,day,base,snapshot,'0'*64,0,'correcting' if correction else 'open',encoded(dependencies).decode()))
            db.execute('INSERT INTO display_current VALUES(?,?) ON CONFLICT(day) DO UPDATE SET job=excluded.job',(day,identifier))
        return self.state(day)

    def refresh(self,day,streams):
        """Append-only continuation. Dependency/source changes are checked explicitly."""
        old=self.state(day)
        if old is None:raise Unavailable('display day absent')
        if old['status']!='caught_up' or old['prepared']:raise Conflict('only caught-up open display can refresh')
        deps={}
        for stream in streams:
            state=self.canonical.published_state(stream,day)
            if state is None:raise Unavailable('awaiting canonical')
            valid=self.canonical.publication_validity(stream,day,state['bundle'],state['metadata'])
            if valid['stale_dependencies']:raise Conflict('canonical stale')
            deps[stream]={'bundle':state['bundle'],'input_hash':state['metadata']['input_hash'],'status':state['metadata']['status'],'epochs':state['metadata']['epoch_vector'],'processed':state['metadata']['processed']}
        prior=json.loads(old['canonical'])
        if set(deps)!=set(prior) or any(deps[k]['epochs']!=prior[k]['epochs'] for k in prior):raise Conflict('display epoch correction required')
        if len({v['input_hash'] for v in deps.values()})!=1:raise Conflict('canonical prefixes differ')
        snapshot=self.reader.open(*bounds(day))
        # Reuse the actual canonical late-record detection against its prior
        # consumed physical inputs, not a guessed timestamp watermark.
        with self.catalog.connection() as db:
            members=db.execute('SELECT source,revision FROM window_source WHERE snapshot=?',(old['snapshot'],)).fetchall()
            for m in members:
                active=db.execute('SELECT revision FROM active WHERE source=?',(m['source'],)).fetchone()
                if active is None or active[0]!=m['revision']:raise Conflict('display source revision correction required')
        if old['after_key']:
            after=tuple(json.loads(old['after_key']));start,end=bounds(day)
            with self.catalog.connection() as db:
                members=db.execute('SELECT source,revision FROM window_source WHERE snapshot=? LIMIT 257',(snapshot,)).fetchall()
                if len(members)>256:raise Conflict('display source budget')
                for m in members:
                    prior=db.execute('SELECT prefix_end FROM window_source WHERE snapshot=? AND source=? AND revision=?',(old['snapshot'],m['source'],m['revision'])).fetchone()
                    offset=prior[0] if prior else 0
                    rows,_=self.reader.resolver._budgeted(db,'SELECT offset FROM entry WHERE source=? AND revision=? AND offset>=? AND stamp>=? AND stamp<? AND (day,stamp,source,revision,offset)<=(?,?,?,?,?) LIMIT 1',(m['source'],m['revision'],offset,start,end,*after),50000)
                    if rows:raise Conflict('late display record requires correction')
        with self.repository._write() as db:
            if db.execute("UPDATE display_job SET snapshot=?,canonical=?,status='open',version=version+1 WHERE id=? AND version=?",(snapshot,encoded(deps).decode(),old['id'],old['version'])).rowcount!=1:raise Conflict('display cursor changed')
        self.reader.close(old['snapshot'])
        return self.state(day)

    def _guard(self,row,db):
        for stream,dep in json.loads(row['canonical']).items():
            current=db.execute('SELECT bundle_id FROM head WHERE name=?',(self.canonical._head(stream,row['day']),)).fetchone()
            if current is None or current[0]!=dep['bundle']:raise Conflict('canonical publication changed')

    def step(self,day,*,fault=lambda _:None,max_channels=1):
        if not 1 <= max_channels <= 8:
            raise ValueError("display channel quantum must be 1..8")
        check_budget();row=self.state(day)
        if row is None:raise Unavailable('display day absent')
        if row['status']=='publish_correction':return self.publish_correction_step(day,fault=fault)
        if row['status'] in ('caught_up','published_closed'):return {'status':row['status'],'raw_bytes':0,'decodes':0}
        if row['prepared'] is not None:return self._commit(row,fault,replay=True)
        after=None if row['after_key'] is None else tuple(json.loads(row['after_key']))
        pending=None if row['pending'] is None else json.loads(row['pending'])
        page=(self.reader.page(row['snapshot'],after=after,limit=1) if pending is None else
              dict(pending['page'],raw_bytes=0,decodes=0))
        channel_index=0 if pending is None else pending['channel']
        next_pending=None;record_finished=True
        if page.get('budget_blocked'):return {'status':'budget_blocked','raw_bytes':0,'decodes':0}
        updates={};cadences={};order=row['orders'];digest=row['input_hash'];nextkey=after
        with self.repository._connection() as db:
            for record in page['records']:
                order+=1
                next_digest=hashed(encoded([digest,record['source_id'],record['revision'],record['byte_offset'],record['record_hash']]))
                raw={'schema_version':1,'timestamp':record['timestamp'],'module':record['module'],
                     'module_serial':record['module_serial'],'position_history_id':record['position_history_id'],**record['measurements']}
                timestamp=_timestamp(raw,'cell');epoch=record['timestamp']
                channels=_channels(raw,'cell')
                if len(channels)>64:raise ValueError('display channel budget exceeded')
                for channel,metric,value in channels[channel_index:channel_index+max_channels]:
                    check_budget();ckey=_channel_key(channel,metric,'source',0)
                    saved=db.execute('SELECT payload FROM display_cadence WHERE job=? AND key=?',(row['id'],ckey)).fetchone()
                    ctx=json.loads(saved[0]) if saved else {'last':epoch,'gaps':[]}
                    if epoch>ctx['last']:ctx={'last':epoch,'gaps':(ctx['gaps']+[epoch-ctx['last']])[-256:]}
                    cadences[ckey]=ctx;cadence=statistics.median(ctx['gaps']) if ctx['gaps'] else None
                    for resolution,seconds in RESOLUTIONS.items():
                        key='B'+hashed(encoded(_channel_key(channel,metric,resolution,_bucket_start(epoch,seconds))))
                        saved=db.execute('SELECT payload FROM display_bucket WHERE job=? AND key=?',(row['id'],key)).fetchone()
                        if saved:
                            bucket=json.loads(saved[0]);_add(bucket,timestamp,value,order)
                        else:bucket=_new_bucket({**channel,'metric':metric},resolution,timestamp,value,order)
                        bucket['_source_cadence']=cadence
                        if len(encoded(bucket))>60000:raise ValueError('display bucket observation budget exceeded; no truncation')
                        public=_public(bucket)
                        if len(encoded(public))>60000:raise ValueError('display bucket output budget exceeded; no truncation')
                        updates[key]={'internal':bucket,'public':public}
                record_finished=channel_index+max_channels>=len(channels)
                if record_finished:
                    nextkey=[math.floor(epoch/86400),epoch,record['source_id'],record['revision'],record['byte_offset']]
                    digest=next_digest
                else:
                    order=row['orders']
                    next_pending={'channel':channel_index+max_channels,'page':{k:page[k] for k in ('records','complete','coverage')}}
        deps=json.loads(row['canonical']);finished=page['complete'] and record_finished
        if finished and any(v['input_hash']!=digest for v in deps.values()):raise Conflict('display/canonical consumed prefix mismatch')
        if finished and sum(v['processed'] for v in deps.values())!=order:raise Unavailable('identity dispatch incomplete: explicit identity origin/checkpoint required')
        status=('published_closed' if all(v['status']=='published_closed' for v in deps.values()) else 'caught_up') if finished else row['status']
        prepared={'pending':next_pending,'updates':updates,'cadences':cadences,'after':nextkey,'orders':order,'input_hash':digest,
                  'status':status,'raw_bytes':page['raw_bytes'],'decodes':page['decodes'],'coverage':page['coverage']}
        if len(encoded(prepared))>1024*1024:raise ValueError('display checkpoint budget exceeded; no truncation')
        with self.repository._write() as db:
            if db.execute('UPDATE display_job SET prepared=?,version=version+1 WHERE id=? AND version=?',(encoded(prepared).decode(),row['id'],row['version'])).rowcount!=1:raise Conflict('display changed')
            fault('after_display_prepared')
        fault('after_display_prepared_commit')
        return self._commit(self.state(day),fault)

    def _commit(self,row,fault,replay=False):
        check_budget()
        p=json.loads(row['prepared']);day=row['day'];correcting=row['status']=='correcting'
        # Corrections stay private until all input has been consumed. Pages are
        # durable bounded updates to this correction's own bucket rows.
        if correcting:
            with self.repository._write() as db:
                self._save_page(db,row,p)
                if p['status']!='correcting':
                    db.execute("UPDATE display_job SET status='publish_correction' WHERE id=?",(row['id'],))
                    db.execute('INSERT INTO display_replacement VALUES(?,NULL,?)',(row['id'],encoded(self._metadata(row,p)).decode()))
            return {'status':'publish_correction' if p['status']!='correcting' else 'correcting','raw_bytes':0 if replay else p['raw_bytes'],'decodes':0 if replay else p['decodes']}
        if row['status']=='publish_correction':raise Conflict('use publish_correction_step')
        work=self.repository.begin(self.head(day),expected_bundle=row['base'])
        metadata=self._metadata(row,p)
        self.repository.stage(work,self.product(day),day,{k:v['public'] for k,v in p['updates'].items()},metadata=metadata)
        fault('before_display_publish')
        check_budget()
        # Head CAS and all display bucket/cadence/cursor state share publication.
        with self.canonical.epochs.publication_guard(self._dependencies(row)), self.catalog.write() as source_db:
            self.reader.resolver._snapshot(source_db,row['snapshot'])
            with self.repository._write() as db:
                self._guard(row,db)
                current=db.execute('SELECT version FROM display_job WHERE id=?',(row['id'],)).fetchone()[0]
                if current!=row['version']:raise Conflict('display checkpoint changed')
                bundle=self.repository._publish(db,work,requires=tuple(v['bundle'] for v in json.loads(row['canonical']).values()),fault=fault)
                self._save_page(db,row,p)
                db.execute('UPDATE display_job SET base=? WHERE id=?',(bundle,row['id']))
                db.execute('INSERT INTO pin VALUES(?,?,?)',(uuid.uuid4().hex,bundle,'display revision'))
                fault('after_display_cursor')
        fault('after_display_commit')
        return {'status':p['status'],'bundle':bundle,'raw_bytes':0 if replay else p['raw_bytes'],'decodes':0 if replay else p['decodes']}

    def _metadata(self,row,p):
        return {'day':row['day'],'status':p['status'],'coverage':p['coverage'],'input_hash':p['input_hash'],
                'canonical_dependencies':json.loads(row['canonical']),'processed_records':p['orders'],
                'processing_record':p['pending'] is not None,'coverage_basis':'frozen input snapshot; output may be in progress','schema_version':DISPLAY_PROJECTION_SCHEMA_VERSION,
                'algorithm_version':AGGREGATION_ALGORITHM_VERSION,'authoritative':False,'source_scope':'confirmed indexed cell inputs only'}

    @staticmethod
    def _save_page(db,row,p):
        db.executemany('INSERT INTO display_bucket VALUES(?,?,?) ON CONFLICT(job,key) DO UPDATE SET payload=excluded.payload',[(row['id'],k,encoded(v['internal']).decode()) for k,v in p['updates'].items()])
        db.executemany('INSERT INTO display_cadence VALUES(?,?,?) ON CONFLICT(job,key) DO UPDATE SET payload=excluded.payload',[(row['id'],k,encoded(v).decode()) for k,v in p['cadences'].items()])
        if db.execute('UPDATE display_job SET after_key=?,input_hash=?,orders=?,status=?,pending=?,prepared=NULL,version=version+1 WHERE id=? AND version=?',
                   (None if p['after'] is None else encoded(p['after']).decode(),p['input_hash'],p['orders'],p['status'],None if p['pending'] is None else encoded(p['pending']).decode(),row['id'],row['version'])).rowcount!=1:raise Conflict('display private checkpoint changed')

    def _dependencies(self,row):
        # Every selected canonical stream was required to have the same captured
        # epoch vector. One representative's two durable dependencies protect it.
        first=next(iter(json.loads(row['canonical']).values()))['bundle']
        with self.repository._connection() as db:
            deps=[r[0] for r in db.execute('SELECT dependency FROM canonical_publication_dependency WHERE bundle=? LIMIT 3',(first,))]
        if len(deps)!=2:raise Conflict('canonical epoch proof unavailable')
        return deps

    def publish_correction_step(self,day,*,fault=lambda _:None):
        check_budget();row=self.state(day)
        if row['status']!='publish_correction':raise Conflict('correction is not ready')
        with self.repository._connection() as db:
            replacement=dict(db.execute('SELECT * FROM display_replacement WHERE job=?',(row['id'],)).fetchone())
        work=replacement['work'];product=self.product(day)
        if work is None:
            work=self.repository.begin(self.head(day),expected_bundle=row['base'])
            self.repository.begin_replacement(work,product,day,metadata=json.loads(replacement['metadata']))
            with self.repository._write() as db:
                if db.execute('UPDATE display_replacement SET work=? WHERE job=? AND work IS NULL',(work,row['id'])).rowcount!=1:raise Conflict('correction work changed')
        progress=self.repository.replacement_progress(work,product,day)
        if not progress.sealed:
            with self.repository._connection() as db:
                values=db.execute('SELECT key,payload FROM display_bucket WHERE job=? AND key>? ORDER BY key LIMIT 4',(row['id'],progress.last_key)).fetchall()
            if values:
                self.repository.stage_replacement(work,product,day,{r['key']:_public(json.loads(r['payload'])) for r in values},expected=progress,fault=fault)
                return {'status':'publish_correction','raw_bytes':0,'decodes':0,'buckets':len(values)}
            self.repository.seal_replacement(work,product,day,expected=progress)
        with self.canonical.epochs.publication_guard(self._dependencies(row)), self.catalog.write() as source_db:
            self.reader.resolver._snapshot(source_db,row['snapshot'])
            with self.repository._write() as db:
                self._guard(row,db)
                bundle=self.repository._publish(db,work,requires=tuple(v['bundle'] for v in json.loads(row['canonical']).values()),fault=fault)
                metadata=json.loads(replacement['metadata'])
                db.execute('UPDATE display_job SET status=?,base=?,version=version+1 WHERE id=?',(metadata['status'],bundle,row['id']))
                db.execute('INSERT INTO pin VALUES(?,?,?)',(uuid.uuid4().hex,bundle,'display correction'))
                fault('after_display_cursor')
        fault('after_display_commit')
        return {'status':metadata['status'],'bundle':bundle,'raw_bytes':0,'decodes':0}

    def read_page(self,day,*,bundle=None,after_key='',limit=64):
        bundle=self.repository.head(self.head(day)) if bundle is None else bundle
        if bundle is None:raise Unavailable('display absent')
        page=self.repository.read(bundle,self.product(day),day,after_key=after_key,limit=limit)
        stale=False;newer=False
        for stream,dep in page['metadata']['canonical_dependencies'].items():
            state=self.canonical.published_state(stream,day,dep['bundle'])
            validity=self.canonical.publication_validity(stream,day,dep['bundle'],state['metadata'])
            # A newer canonical head alone does not invalidate this pinned
            # as-of dependency. Actual epoch/correction changes still do.
            stale=stale or validity['stale_dependencies']
            newer=newer or validity['historical_revision']
        page['validity']={'historical_revision':bundle!=self.repository.head(self.head(day)),
                          'stale_dependencies':stale,'newer_canonical_available':newer,'coverage':'as_of_confirmed_indexed_inputs'}
        return page
