class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import pytest
from dataclasses import asdict
from projection_repository import ProjectionRepository,Conflict
from metadata_revision import MetadataRevision,RevisionMetadataReader
from test_prepared_read_metadata import resolver
from test_timeline import build,add
from maintenance import MaintenanceEventLog


def begin(tmp_path):
    repo=ProjectionRepository(tmp_path/'meta')
    rev=MetadataRevision(repo,'first');rev.initialize();rev.begin(provenance={'scope':'synthetic original inputs'})
    return repo,rev


def publish(rev,head=None):
    for _ in range(1000):
        if rev.position_step(expected_version=rev.state()['version'],limit=2)['complete']:break
    else:pytest.fail('position correction unfinished')
    rev.publish(expected_version=rev.state()['version'],head='current',expected_head=head,scope_proof={'synthetic':'complete supplied scope'})


def test_identity_and_incremental_revision_share_old_pages(tmp_path):
    repo,rev=begin(tmp_path);original=resolver()
    rev.apply_page('position',[s.to_dict() for s in original.snapshots[:2]],source='positions',sequence=1,expected_version=0,provenance={'confirmed':'synthetic'})
    publish(rev);old=RevisionMetadataReader(repo,'first')
    baseline=old.epochs()
    next_rev=MetadataRevision(repo,'second');next_rev.begin(parent='first',provenance={'scope':'confirmed append only'})
    with repo._connection() as db:
        assert [tuple(r) for r in db.execute('SELECT name,node FROM metadata_root WHERE revision=? ORDER BY name',('first',))]==[tuple(r) for r in db.execute('SELECT name,node FROM metadata_root WHERE revision=? ORDER BY name',('second',))]
    measured=next_rev.apply_page('position',[original.snapshots[2].to_dict()],source='positions',sequence=2,expected_version=0,provenance={'confirmed':'synthetic append'})
    assert measured['records']==1 and measured['node_writes']<100
    publish(next_rev,'first');current=RevisionMetadataReader(repo,'second')
    assert current.epochs()==original.epochs()
    assert old.epochs()==baseline
    for snapshot in original.snapshots:assert current.topology_at(snapshot.effective_at)==original.topology_at(snapshot.effective_at)


def test_raw_maintenance_revisions_match_original_timeline(tmp_path):
    service,path,timeline=build(tmp_path)
    created=add(service,module_serial='A');service.update(created.maintenance_event_id,expected_revision=1,changes={'title':'Changed'})
    records=MaintenanceEventLog(tmp_path/'maintenance.jsonl').read_all()
    repo,rev=begin(tmp_path)
    rev.apply_page('maintenance',[asdict(r) for r in records],source='maintenance',sequence=1,expected_version=0,provenance={'confirmed':'synthetic log'})
    publish(rev);reader=RevisionMetadataReader(repo,'first')
    args={'timestamp_from':'2000-01-01T00:00:00Z','timestamp_to':'2030-01-01T00:00:00Z','include_archived':True}
    assert reader.query(**args)==timeline.query(**args)
    service.archive(created.maintenance_event_id,expected_revision=2)
    update=MaintenanceEventLog(tmp_path/'maintenance.jsonl').read_all()[-1]
    nxt=MetadataRevision(repo,'second');nxt.begin(parent='first',provenance={'scope':'one appended maintenance revision'})
    result=nxt.apply_page('maintenance',[asdict(update)],source='maintenance',sequence=2,expected_version=0,provenance={'confirmed':'synthetic append'})
    assert result['records']==1
    publish(nxt,'first');latest=RevisionMetadataReader(repo,'second')
    assert latest.query(**args)==timeline.query(**args)
    assert latest.query(**dict(args,include_archived=False))==[]
    assert reader.query(**args)[0].metadata['revision']==2


@pytest.mark.parametrize('stage',['before_metadata_page_commit','after_metadata_page_commit','before_metadata_publish_commit','after_metadata_publish_commit'])
def test_metadata_crash_restart(stage,tmp_path):
    repo,rev=begin(tmp_path)
    def fault(point):
        if point==stage:raise InjectedCrash('power loss')
    data=[resolver().snapshots[0].to_dict()]
    if 'page' in stage:
        with pytest.raises(InjectedCrash):rev.apply_page('position',data,source='positions',sequence=1,expected_version=0,provenance={'confirmed':'synthetic'},fault=fault)
        rev=MetadataRevision(ProjectionRepository(repo.path),'first')
        if rev.state()['version']==0:rev.apply_page('position',data,source='positions',sequence=1,expected_version=0,provenance={'confirmed':'synthetic'})
        publish(rev)
    else:
        rev.apply_page('position',data,source='positions',sequence=1,expected_version=0,provenance={'confirmed':'synthetic'})
        for _ in range(1000):
            if rev.position_step(expected_version=rev.state()['version'])['complete']:break
        with pytest.raises(InjectedCrash):rev.publish(expected_version=rev.state()['version'],head='current',expected_head=None,scope_proof={'confirmed':'synthetic'},fault=fault)
        rev=MetadataRevision(ProjectionRepository(repo.path),'first')
        if rev.state()['status']!='ready':publish(rev)
    assert RevisionMetadataReader(repo,'first').topology_at(data[0]['effective_at'])['positions'][0]['physical_serial']=='A'


def test_maintenance_gap_and_unconfirmed_parent_fail(tmp_path):
    from maintenance_service import MaintenanceHistoryError
    service,_,_=build(tmp_path);item=add(service)
    repo,rev=begin(tmp_path)
    malformed=asdict(item);malformed['revision']=2
    with pytest.raises(MaintenanceHistoryError):rev.apply_page('maintenance',[malformed],source='m',sequence=1,expected_version=0,provenance={'scope':'synthetic gap'})
    assert rev.state()['version']==0
    with pytest.raises(Conflict):MetadataRevision(repo,'next').begin(parent='first',provenance={'scope':'not ready'})


def test_historical_metadata_reader_lease_protects_shared_pages(tmp_path):
    from store_retirement import StoreRetirement
    from test_store_retirement import drain_index,drain_fences,drain_validation,POLICY
    repo,rev=begin(tmp_path)
    original=resolver()
    rev.apply_page('position',[s.to_dict() for s in original.snapshots[:2]],source='p',sequence=1,expected_version=0,provenance={'confirmed':'synthetic'})
    publish(rev)
    nxt=MetadataRevision(repo,'second');nxt.begin(parent='first',provenance={'confirmed':'append'})
    nxt.apply_page('position',[original.snapshots[2].to_dict()],source='p',sequence=2,expected_version=0,provenance={'confirmed':'append'})
    publish(nxt,'first')
    life=StoreRetirement(repo);life.initialize();drain_index(life);life.begin('gc',policy=POLICY)
    life.select('gc','metadata_revision',['first'])
    with repo._connection() as db:
        for row in db.execute('SELECT name FROM metadata_root WHERE revision=?',('first',)):life.select('gc','metadata_root',['first',row[0]])
    life.select('gc','metadata_cursor',['first','p'])
    old=RevisionMetadataReader(repo,'first')
    with old._read() as trees:
        drain_fences(life)
        with pytest.raises(Conflict,match='external owner'):drain_validation(life)
        assert trees['topology'].floor('~')[1]['position_history_id']==original.snapshots[1].position_history_id
    drain_validation(life)
    for _ in range(100):
        if life.delete_step('gc',limit=2)['complete']:break
    else:pytest.fail('metadata roots retirement unfinished')
    assert RevisionMetadataReader(repo,'second').epochs()==original.epochs()


def test_backdated_position_reprojects_only_affected_suffix(tmp_path):
    from research_identity import ResearchIdentityResolver
    from test_history_epoch_adapter import position
    repo,rev=begin(tmp_path)
    originals=[position(100,'A'),position(200,'B'),position(400,'A')]
    rev.apply_page('position',[v.to_dict() for v in originals],source='p',sequence=1,expected_version=0,provenance={'confirmed':'synthetic'})
    publish(rev)
    late=position(300,'C')
    nxt=MetadataRevision(repo,'second');nxt.begin(parent='first',provenance={'confirmed':'late append'})
    nxt.apply_page('position',[late.to_dict()],source='p',sequence=2,expected_version=0,provenance={'confirmed':'synthetic late'})
    touched=[]
    for _ in range(1000):
        result=nxt.position_step(expected_version=nxt.state()['version'],limit=1);touched.append(result)
        assert result['rows']<=1
        if result['complete']:break
    else:pytest.fail('correction unfinished')
    publish(nxt,'first')
    expected=ResearchIdentityResolver(originals+[late]);reader=RevisionMetadataReader(repo,'second')
    assert reader.epochs()==expected.epochs()
    assert RevisionMetadataReader(repo,'first').epochs()==ResearchIdentityResolver(originals).epochs()
    for v in originals+[late]:assert reader.topology_at(v.effective_at)==expected.topology_at(v.effective_at)


@pytest.mark.parametrize('boundary',['before_position_correction_commit','after_position_correction_commit'])
def test_same_time_late_positions_resume_each_correction_phase(tmp_path,boundary):
    from dataclasses import replace
    from test_history_epoch_adapter import position
    from research_identity import ResearchIdentityResolver
    repo,rev=begin(tmp_path)
    originals=[position(100,'A'),position(300,'B')]
    rev.apply_page('position',[r.to_dict() for r in originals],source='p',sequence=1,expected_version=0,provenance={'synthetic':'initial'})
    publish(rev)
    late=position(100,'C')
    late=replace(late,created_at='2026-10-20T12:00:00+00:00')
    nxt=MetadataRevision(repo,'next');nxt.begin(parent='first',provenance={'synthetic':'same effective time'})
    assert nxt.state()['parent_proof']
    nxt.apply_page('position',[late.to_dict()],source='p',sequence=2,expected_version=0,provenance={'synthetic':'late'})
    crashed=set()
    for _ in range(100):
        state=nxt.state();phase=state['position_phase']
        def fault(stage):
            if stage==boundary and phase not in crashed:crashed.add(phase);raise InjectedCrash('power loss')
        try:result=nxt.position_step(expected_version=state['version'],limit=1,fault=fault)
        except InjectedCrash:nxt=MetadataRevision(ProjectionRepository(repo.path),'next');continue
        if result['complete']:break
    else:pytest.fail('position correction incomplete')
    publish(nxt,'first')
    expected=ResearchIdentityResolver(originals+[late]);reader=RevisionMetadataReader(repo,'next')
    assert reader.epochs()==expected.epochs()
    for snapshot in originals:assert reader.topology_at(snapshot.effective_at)==expected.topology_at(snapshot.effective_at)
    assert {'trim_epochs','trim_topology','project'}<=crashed
