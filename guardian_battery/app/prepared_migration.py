"""Explicit offline preparation of a reviewed finite legacy inventory.

Not called by startup, a reader or a transport. Original writer files retain
bytes/names; each step adopts/consumes a bounded page and durably records its
phase. Runtime activation and the UTC writer contract remain separate gates.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from projection_repository import ProjectionRepository,Conflict,Unavailable
from raw_registry import RawRegistry,file_signature
from source_catalog import SourceCatalog
from application_writer import ApplicationWriter
from application_migration import LegacyWriterAdoption
from metadata_revision import MetadataRevision
from metadata_log_consumer import MetadataLogConsumer
from application_metadata import MetadataTail
from history_epoch_adapter import EpochStore,EpochLogConsumer
from canonical_day_repository import CanonicalDayRepository
from display_day_repository import DisplayDayRepository
from history_repository_preparation import RepositoryPreparation
from diagnostic_repository import DiagnosticRepository
from publication_window_index import PublicationWindowIndex
from application_tail import ApplicationTail
from raw_registry_catalog import RegistryCatalogConsumer
from application_catalog import CatalogTail
from store_migration_manifest import MigrationManifest
from research_budget import check_budget
from daily_diagnostics import _atomic_json


class PreparedMigration:
    def __init__(self,root,plan):
        self.root=Path(root).resolve();self.plan=plan
        if plan.get('contract')!='guardian-prepared-migration-1' or not plan.get('provenance') or not plan.get('rollback'):
            raise ValueError('reviewed migration inventory, provenance and rollback required')
        if not 1<=len(plan['files'])<=4096 or len(plan['streams'])>64:
            raise ValueError('explicit migration inventory/stream budget')
        raw=json.dumps(plan,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(raw)>1024*1024:raise ValueError('migration plan exceeds one MiB metadata budget')
        self.plan=json.loads(raw)
        self.digest=hashlib.sha256(raw).hexdigest();self.layout=self.plan['layout']
        self.state_directory=self.path(plan.get('state_directory','.'))
        self.control=self.state_directory/'prepared-migration.json';self.output=self.state_directory/'prepared-layout.json'
        self.paths=self.layout['paths']
        selected=[(item['writer'],str(self.path(item['path']))) for item in plan['files']]
        if len({path for writer,path in selected})!=len(selected):raise ValueError('duplicate legacy source binding')
        for writer,path in selected:
            if writer not in self.layout['writers']:raise ValueError('unknown inventory writer')
            if not Path(path).is_relative_to(self.path(self.layout['writers'][writer]['root'])):
                raise ValueError('inventory source outside declared writer root')

    def path(self,value):
        result=(self.root/value).resolve()
        if not result.is_relative_to(self.root):raise ValueError('migration path outside explicit root')
        return result

    def state(self):
        value=json.loads(self.control.read_text())
        if value.get('plan_sha256')!=self.digest:raise Conflict('frozen migration plan changed')
        return value

    def save(self,state):_atomic_json(self.control,state)

    def begin(self):
        if self.control.exists():return self.state()
        if not self.root.is_dir():raise ValueError('existing offline candidate root required')
        targets=[self.path(self.paths[k]) for k in ('repository','cell_catalog','epochs')]
        targets += [self.path(spec['registry']) for spec in self.layout['writers'].values()]
        targets += [self.path(spec['path']) for spec in self.layout['catalogs'].values()]
        if len(set(targets))!=len(targets):raise ValueError('derived stores must be distinct')
        if self.output.exists() or any(path.exists() for path in targets):
            raise Conflict('existing derived work preserved; choose an empty prepared-store scope')
        state={'contract':'guardian-prepared-migration-state-1','plan_sha256':self.digest,
            'phase':'freeze','ordinal':0,'files':[],'turn':0,'runtime_activated':False}
        self.state_directory.mkdir(parents=True,exist_ok=True)
        self.save(state);return state

    def objects(self,initialize=False):
        repo=ProjectionRepository(self.path(self.paths['repository']))
        cell=SourceCatalog(self.path(self.paths['cell_catalog']));epochs=EpochStore(self.path(self.paths['epochs']))
        writers={}
        for name,spec in self.layout['writers'].items():
            root=self.path(spec['registry']);directory=self.path(spec['root'])
            if initialize:directory.mkdir(parents=True,exist_ok=True)
            registry=RawRegistry(root) if root.exists() else RawRegistry.create(root) if initialize else None
            if registry is None:raise Unavailable('migration registry missing')
            writers[name]=ApplicationWriter(registry,directory,stream=spec['stream'],writer_epoch=spec['epoch'])
        canonical=CanonicalDayRepository(repo,cell,epochs);display=DisplayDayRepository(repo,cell,canonical)
        preparation=RepositoryPreparation(repo,cell,epochs,canonical,display)
        diagnostics=DiagnosticRepository(repo,cell,epochs);index=PublicationWindowIndex(repo)
        tail=ApplicationTail(preparation,diagnostics,index,name=self.layout['diagnostics'])
        metadata=self.layout['metadata'];revision=MetadataRevision(repo,'initial-prepared-migration')
        consumer=MetadataLogConsumer(writers['metadata'].registry,revision)
        epoch=EpochLogConsumer(writers['metadata'].registry,epochs)
        metadata_tail=MetadataTail(repo,writers['metadata'].registry,head=metadata['head'],mapping=metadata['mapping'],origin_proof=metadata['origin'])
        catalogs={'cell':cell};tails={}
        for name,spec in self.layout['catalogs'].items():
            catalog=SourceCatalog(self.path(spec['path']));catalogs[name]=catalog
            tails[name]=CatalogTail(RegistryCatalogConsumer(writers[spec['writer']].registry,catalog,
                record_format=spec['format'],source_formats=spec.get('source_formats')),name=spec['tail'])
        manifest=MigrationManifest(repo,self.layout['manifest'])
        if initialize:
            canonical.initialize();display.initialize();preparation.initialize();diagnostics.initialize();index.initialize();tail.initialize()
            revision.initialize();revision.begin(provenance={'plan_sha256':self.digest,'origin':self.plan['provenance']})
            consumer.initialize(metadata['mapping'],origin_proof=metadata['origin'])
            epoch.initialize({source:kind if kind in ('position','config') else None for source,kind in metadata['mapping'].items()})
            metadata_tail.initialize();manifest.initialize()
            for item in tails.values():item.initialize()
            for stream in self.plan['streams']:
                canonical.define_stream(**stream);preparation.enroll(stream['stream'])
            with repo._connection() as db:
                exists=db.execute('SELECT 1 FROM diagnostic_composition WHERE name=?',(self.layout['diagnostics'],)).fetchone()
            if not exists:
                from config_ui import DEFAULTS
                options={**DEFAULTS,**json.loads(self.path(self.paths['options']).read_text())}
                diagnostics.create(self.layout['diagnostics'],options,ingestion='history')
            preparation.watch_registry(writers['cell'].registry.root)
        return SimpleNamespace(repo=repo,cell=cell,epochs=epochs,writers=writers,preparation=preparation,
            diagnostics=diagnostics,tail=tail,revision=revision,consumer=consumer,epoch=epoch,
            catalogs=catalogs,tails=tails,manifest=manifest)

    def _binding(self):
        from prepared_candidate import open_candidate,DeferredPublisher
        def forbidden(*args,**kwargs):raise AssertionError('offline preparation cannot open a transport')
        runtime=self.layout['runtime']
        console=SimpleNamespace(port=runtime['console_port'],close=lambda:None,command=forbidden)
        # No underlying serial, MQTT, HTTP, battery or supervisor operation.
        app=open_candidate(root=self.root,layout=self.layout,console=console,publisher=DeferredPublisher(forbidden),
            supervisor_api=forbidden,cursor_secret=self.path(runtime['cursor_secret_file']).read_bytes(),
            wall_clock=lambda:self.plan['as_of_timestamp'],monotonic=lambda:0,parse_bat=forbidden,
            bus_port_resolver=lambda:runtime.get('bus_port'),bus_serial_opener=forbidden,inspect_binding=True)
        try:return app.binding
        finally:app.close()

    def step(self):
        check_budget();state=self.state();phase=state['phase'];ordinal=state['ordinal']
        if phase=='complete':return state
        if phase in ('freeze','verify'):
            item=self.plan['files'][ordinal];path=self.path(item['path'])
            observed={'signature':file_signature(path),'change_time_ns':path.stat().st_ctime_ns}
            if phase=='freeze':state['files'].append(observed)
            elif observed!=state['files'][ordinal]:raise Conflict('legacy source changed during offline preparation')
            state['ordinal']+=1
            if state['ordinal']==len(self.plan['files']):state.update(phase='initialize' if phase=='freeze' else 'bind',ordinal=0)
            self.save(state);return state
        if phase=='initialize':
            for key in ('repository','cell_catalog','epochs'):self.path(self.paths[key]).parent.mkdir(parents=True,exist_ok=True)
            for spec in self.layout['catalogs'].values():self.path(spec['path']).parent.mkdir(parents=True,exist_ok=True)
            for spec in self.layout['writers'].values():self.path(spec['registry']).parent.mkdir(parents=True,exist_ok=True)
            self.objects(initialize=True);state.update(phase='adopt',ordinal=0);self.save(state);return state
        o=self.objects()
        if phase=='adopt':
            item=self.plan['files'][ordinal];path=self.path(item['path'])
            if {'signature':file_signature(path),'change_time_ns':path.stat().st_ctime_ns}!=state['files'][ordinal]:
                raise Conflict('frozen legacy source changed before adoption')
            adoption=LegacyWriterAdoption(o.repo,'prepared-file-'+str(ordinal),[(o.writers[item['writer']],path)])
            adoption.initialize();adoption.begin(provenance={'plan_sha256':self.digest,'ordinal':ordinal},rollback=self.plan['rollback'])
            if adoption.step()['status']=='complete':state['ordinal']+=1
            if state['ordinal']==len(self.plan['files']):state.update(phase='metadata',ordinal=0)
        elif phase=='metadata':
            if o.consumer.cursor()[0]<=o.consumer.registry._head()['seq']:o.consumer.step(expected_cursor=o.consumer.cursor(),max_records=8)
            elif o.revision.state()['status']!='ready':
                result=o.revision.position_step(expected_version=o.revision.state()['version'],limit=8)
                if result['complete']:o.revision.publish(expected_version=o.revision.state()['version'],head=self.layout['metadata']['head'],expected_head=None,scope_proof={'plan_sha256':self.digest})
            else:state['phase']='epochs'
        elif phase=='epochs':
            if o.epoch.cursor()[0]<=o.epoch.registry._head()['seq']:o.epoch.step(expected_cursor=o.epoch.cursor(),max_records=8)
            else:state.update(phase='catalogs',ordinal=0)
        elif phase=='catalogs':
            names=list(o.tails)
            if ordinal<len(names):
                item=o.tails[names[ordinal]]
                if item.step().get('status')=='caught_up' and not item.catalog.status()['pending']:state['ordinal']+=1
            else:
                o.preparation.confirm_frontier(self.plan['frontier_day'],proof={'plan_sha256':self.digest,'origin':self.plan['provenance']})
                state.update(phase='core',ordinal=0)
        elif phase=='core':
            (o.preparation.step if state['turn']%2==0 else o.tail.step)();state['turn']+=1
            if o.preparation.status()['state']=='blocked':raise Unavailable(str(o.preparation.status()['blocked']))
            with o.repo._connection() as db:latest=db.execute('SELECT COALESCE(MAX(id),0) FROM preparation_job').fetchone()[0]
            raw=RegistryCatalogConsumer(o.writers['cell'].registry,o.cell);tail=o.tail.state()
            if (raw.cursor()[0]>raw.registry._head()['seq'] and not o.cell.status()['pending']
                    and o.preparation.status()['state']=='caught_up' and tail['job'] is None and tail['after_job']>=latest):
                state['phase']='handover'
        elif phase=='handover':
            o.diagnostics.begin_live_handover(self.layout['diagnostics'])
            if o.diagnostics.state(self.layout['diagnostics'])['work']:o.diagnostics.finish(self.layout['diagnostics'])
            else:state.update(phase='verify',ordinal=0)
        elif phase=='bind':
            binding=self._binding()
            units=[(catalog,{'origin':self.plan['provenance'],'catalog_state':catalog.status()}) for catalog in o.catalogs.values()]
            units.extend([(o.consumer,{'origin':self.plan['provenance'],'registry':o.consumer.identity,'last_sequence':o.consumer.registry._head()['seq'],
                'revision':o.revision.revision,'head':self.layout['metadata']['head'],'expected_head':None}),
                (o.epoch,{'origin':self.plan['provenance'],'registry':o.epoch.identity,'last_sequence':o.epoch.registry._head()['seq']})])
            o.manifest.begin(units,provenance={'plan_sha256':self.digest,'application_binding':binding,'files':len(state['files'])},rollback=self.plan['rollback'])
            state.update(phase='manifest',ordinal=0)
        elif phase=='manifest':
            if o.manifest.state()['status']=='complete':
                _atomic_json(self.output,self.layout);state.update(phase='complete',runtime_activated=False);self.save(state);return state
            with o.repo._connection() as db:
                ordinal=db.execute("SELECT ordinal FROM lifecycle_migration_unit WHERE migration=? AND status='pending' ORDER BY ordinal LIMIT 1",(o.manifest.identifier,)).fetchone()[0]
            adapters=[*o.catalogs.values(),o.consumer,o.epoch]
            result=o.manifest.step(adapters[ordinal],expected_version=o.manifest.state()['version'])
            if result['complete']:
                _atomic_json(self.output,self.layout);state.update(phase='complete',runtime_activated=False)
            elif result.get('evidence',{}).get('migration_scope_complete'):state['ordinal']+=1
            else:
                with o.repo._connection() as db:
                    done=db.execute('SELECT status FROM lifecycle_migration_unit WHERE migration=? AND ordinal=?',(o.manifest.identifier,ordinal)).fetchone()[0]=='complete'
                if done:state['ordinal']+=1
        else:raise Conflict('unknown offline migration phase')
        self.save(state);return state


def main(argv=None):
    import argparse
    from version import require_source_commit
    parser=argparse.ArgumentParser(description='Explicit offline prepared-candidate migration; no activation')
    parser.add_argument('--root',required=True);parser.add_argument('--plan',required=True)
    parser.add_argument('--begin',action='store_true');parser.add_argument('--steps',type=int,default=1)
    args=parser.parse_args(argv)
    if not 1<=args.steps<=64:parser.error('--steps must be 1..64')
    require_source_commit()
    with Path(args.plan).open('rb') as handle:raw=handle.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('migration plan exceeds budget')
    migration=PreparedMigration(args.root,json.loads(raw))
    if args.begin:migration.begin()
    for _ in range(args.steps):
        state=migration.step()
        if state['phase']=='complete':break
    print(json.dumps({'phase':state['phase'],'ordinal':state['ordinal'],'plan_sha256':migration.digest,'runtime_activated':False},sort_keys=True))


if __name__=='__main__':main()
