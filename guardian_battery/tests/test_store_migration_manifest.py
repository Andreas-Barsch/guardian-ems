class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import pytest
from projection_repository import Conflict
from store_migration_manifest import MigrationManifest
from store_retirement import StoreRetirement
from snapshot_lifecycle import SnapshotLifecycle
from metadata_revision import MetadataRevision,RevisionMetadataReader
from metadata_log_consumer import MetadataLogConsumer
from history_epoch_adapter import EpochLogConsumer
from raw_registry import RawRegistry,hashed,encoded
from test_publication_history_readers import setup
from test_history_epoch_adapter import position

@pytest.mark.parametrize('boundary',['after_adapter_commit','before_manifest_commit','after_manifest_commit'])
def test_actual_stores_resume_manifest_without_old_receipt_replay(tmp_path,boundary):
    env,preparation,index,readers=setup(tmp_path)
    repo=env['repo'];catalog=env['catalog']
    registry=RawRegistry.create(tmp_path/'metadata-registry')
    receipt=registry.register('position','r',tmp_path/'positions.jsonl',namespace='legacy-local',writer_epoch='synthetic',operation='register')
    receipt=registry.append('position',encoded(position(100,'A').to_dict())+b'\n',expected_receipt=hashed(encoded(receipt)),operation='append')
    revision=MetadataRevision(repo,'initial');revision.initialize();revision.begin(provenance={'synthetic':'explicit scope'})
    consumer=MetadataLogConsumer(registry,revision);consumer.initialize({'position':'position'},origin_proof={'synthetic':'beginning'})
    epoch=EpochLogConsumer(registry,env['epochs']);epoch.initialize({'position':'position'})
    bridge=SnapshotLifecycle(repo,catalog);bridge.initialize()
    life=StoreRetirement(repo);life.initialize()
    manifest=MigrationManifest(repo,'migration');manifest.initialize()
    scope={'origin':'synthetic confirmed log','registry':consumer.identity,'last_sequence':receipt['seq']}
    units=[(consumer,dict(scope,revision='initial',head='current',expected_head=None)),(epoch,scope),(life,{'origin':'derived stores schema'}),(bridge,{'origin':'actual jobs'})]
    manifest.begin(units,provenance={'synthetic':'test'},rollback={'preserved':'original source'})
    crashed=False
    for _ in range(1000):
        state=manifest.state()
        with repo._connection() as db:
            row=db.execute("SELECT ordinal FROM lifecycle_migration_unit WHERE migration='migration' AND status='pending' ORDER BY ordinal LIMIT 1").fetchone()
        if row is None:break
        def fault(point):
            nonlocal crashed
            if point==boundary and not crashed:crashed=True;raise InjectedCrash('power loss')
        try:manifest.step(units[row[0]][0],expected_version=state['version'],fault=fault)
        except InjectedCrash:manifest=MigrationManifest(repo,'migration')
    else:pytest.fail('manifest incomplete')
    assert manifest.state()['status']=='complete'
    assert RevisionMetadataReader(repo,'initial').topology_at(position(100,'A').effective_at)['positions'][0]['physical_serial']=='A'
    registry.reset_metrics()
    result=manifest.step(consumer,expected_version=manifest.state()['version'])
    assert result['work']==0
    assert crashed
    assert manifest.archive_page()['units']
    nonce=manifest.state()['nonce'];manifest.prune(nonce=nonce,archive_sha256='a'*64,retry_closed=True)
    manifest.begin(units,provenance={'synthetic':'test'},rollback={'preserved':'original source'})
    with pytest.raises(Conflict):manifest.prune(nonce=nonce,archive_sha256='a'*64,retry_closed=True)


def test_unknown_schema_and_changed_manifest_are_not_adopted(tmp_path):
    from projection_repository import ProjectionRepository
    repo=ProjectionRepository(tmp_path/'repo');life=StoreRetirement(repo);life.initialize()
    manifest=MigrationManifest(repo,'m');manifest.initialize()
    units=[(life,{'origin':'synthetic'})]
    manifest.begin(units,provenance={'source':'synthetic'},rollback={'preserved':True})
    with pytest.raises(Conflict):manifest.begin(units,provenance={'source':'different'},rollback={'preserved':True})
    with repo._write() as db:db.execute('CREATE TABLE unexpected(x TEXT)')
    with pytest.raises(Conflict,match='schema changed'):manifest.step(life,expected_version=0)
    assert manifest.state()['version']==0


def single_log(tmp_path,kind,count=1,future=0):
    from projection_repository import ProjectionRepository
    from history_epoch_adapter import EpochStore
    repo=ProjectionRepository(tmp_path/'repo')
    registry=RawRegistry.create(tmp_path/'registry')
    receipt=registry.register('position','r',tmp_path/'positions.jsonl',namespace='legacy-local',writer_epoch='test',operation='register')
    values=[position(100+i,'A' if i%2 else 'B') for i in range(count)]
    receipt=registry.append('position',b''.join(encoded(v.to_dict())+b'\n' for v in values),expected_receipt=hashed(encoded(receipt)),operation='append')
    if kind=='metadata-log':
        rev=MetadataRevision(repo,'initial');rev.initialize();rev.begin(provenance={'test':'frontier'})
        adapter=MetadataLogConsumer(registry,rev);adapter.initialize({'position':'position'},origin_proof={'test':'origin'})
    else:
        adapter=EpochLogConsumer(registry,EpochStore(tmp_path/'epochs'));adapter.initialize({'position':'position'})
    manifest=MigrationManifest(repo,'frontier');manifest.initialize()
    scope={'origin':'synthetic complete input','registry':adapter.identity,'last_sequence':receipt['seq']+future}
    if kind=='metadata-log':scope.update(revision='initial',head='current',expected_head=None)
    manifest.begin([(adapter,scope)],provenance={'test':'explicit'},rollback={'test':'retained Raw'})
    return repo,registry,adapter,manifest,receipt,values


def finish_log(manifest,adapter):
    results=[]
    for _ in range(100):
        result=manifest.step(adapter,expected_version=manifest.state()['version'])
        results.append(result)
        if result['complete']:return results
    pytest.fail('manifest did not complete normally')


@pytest.mark.parametrize('kind',['metadata-log','epoch-log'])
@pytest.mark.parametrize('count',[1,17])
def test_exact_current_journal_end_completes_without_fault_or_retry(tmp_path,kind,count):
    repo,registry,adapter,manifest,receipt,values=single_log(tmp_path,kind,count)
    first=manifest.step(adapter,expected_version=0)
    assert adapter.cursor()[:2]==(receipt['seq'],0)
    second=manifest.step(adapter,expected_version=1)
    assert second['version']==2
    if count>8:
        assert adapter.cursor()[0]==receipt['seq'] and adapter.cursor()[1]>0
        assert second['evidence']['decodes']==8 and not second['evidence']['caught_up']
    else:
        assert adapter.cursor()[:2]==(receipt['seq']+1,0)
        assert second['evidence']['caught_up']
    results=[first,second]+finish_log(manifest,adapter)
    assert sum(r.get('evidence',{}).get('decodes',0) for r in results)==count
    assert manifest.state()['status']=='complete'
    assert adapter.cursor()[:2]==(receipt['seq']+1,0)
    if kind=='metadata-log':
        from research_identity import ResearchIdentityResolver
        assert RevisionMetadataReader(repo,'initial').epochs()==ResearchIdentityResolver(values).epochs()
    else:
        with adapter.store.connection() as db:assert db.execute('SELECT count(*) FROM epoch').fetchone()[0]==count


@pytest.mark.parametrize('kind',['metadata-log','epoch-log'])
def test_missing_declared_future_receipt_stays_unavailable_after_restart(tmp_path,kind):
    repo,registry,adapter,manifest,receipt,values=single_log(tmp_path,kind,future=1)
    manifest.step(adapter,expected_version=0)
    with pytest.raises(Conflict,match='frontier unavailable'):manifest.step(adapter,expected_version=1)
    assert adapter.cursor()[:2]==(receipt['seq']+1,0)
    assert manifest.state()['version']==1 and manifest.state()['status']=='preparing'
    reopened=MigrationManifest(repo,'frontier')
    with pytest.raises(Conflict,match='frontier unavailable'):reopened.step(adapter,expected_version=1)
    assert reopened.state()['status']=='preparing'
    if kind=='metadata-log':assert adapter.revision.state()['status']=='preparing'


@pytest.mark.parametrize('kind',['metadata-log','epoch-log'])
@pytest.mark.parametrize('count',[1,17])
@pytest.mark.parametrize('boundary',['after_adapter_commit','before_manifest_commit'])
def test_restart_after_committed_log_page_before_ack_without_replay(tmp_path,kind,count,boundary):
    repo,registry,adapter,manifest,receipt,values=single_log(tmp_path,kind,count)
    manifest.step(adapter,expected_version=0)
    def fault(point):
        if point==boundary:raise InjectedCrash('exact injected interruption')
    with pytest.raises(InjectedCrash):manifest.step(adapter,expected_version=1,fault=fault)
    durable=adapter.cursor()
    assert durable[0]==receipt['seq']+(count<=8)
    assert (durable[1]>0)==(count>8)
    assert manifest.state()['version']==1
    # Recreate both adapter and manifest against their committed stores.
    if kind=='metadata-log':adapter=MetadataLogConsumer(registry,MetadataRevision(repo,'initial'))
    else:adapter=EpochLogConsumer(registry,adapter.store)
    assert adapter.cursor()==durable
    results=finish_log(MigrationManifest(repo,'frontier'),adapter)
    assert sum(r.get('evidence',{}).get('decodes',0) for r in results)==max(0,count-8)
    assert adapter.cursor()[:2]==(receipt['seq']+1,0)


@pytest.mark.parametrize('kind',['metadata-log','epoch-log'])
def test_later_receipts_do_not_expand_frozen_manifest(tmp_path,kind):
    repo,registry,adapter,manifest,receipt,values=single_log(tmp_path,kind)
    registry.append('position',encoded(position(999,'C').to_dict())+b'\n',expected_receipt=hashed(encoded(receipt)),operation='later')
    finish_log(manifest,adapter)
    assert adapter.cursor()[:2]==(receipt['seq']+1,0)
    assert manifest.state()['status']=='complete'



@pytest.mark.parametrize('mutation',['pending','indexed'])
def test_catalog_state_manifest_rejects_changes_outside_frozen_inventory(tmp_path,mutation):
    from source_catalog import SourceCatalog
    from projection_repository import ProjectionRepository,Conflict
    from store_migration_manifest import MigrationManifest
    from test_source_catalog import registration,index_all
    catalog=SourceCatalog(tmp_path/'catalog');repo=ProjectionRepository(tmp_path/'repo')
    manifest=MigrationManifest(repo,'frozen-all-sources');manifest.initialize()
    manifest.begin([(catalog,{'origin':'synthetic finite inventory','catalog_state':catalog.status()})],
        provenance={'test':True},rollback={'originals':True})
    path=tmp_path/'new.jsonl';path.write_text('{"timestamp":100,"module":1}\n')
    catalog.register(**registration(path,source='new'))
    if mutation=='indexed':index_all(catalog,'new')
    with pytest.raises(Conflict):manifest.step(catalog,expected_version=0)
    assert manifest.state()['status']=='preparing'
