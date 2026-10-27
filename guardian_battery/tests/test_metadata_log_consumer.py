class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import uuid
import pytest
from raw_registry import RawRegistry,hashed,encoded
from metadata_revision import MetadataRevision,RevisionMetadataReader
from metadata_log_consumer import MetadataLogConsumer
from projection_repository import ProjectionRepository,Conflict
from test_metadata_revision import publish
from test_history_epoch_adapter import position
from research_identity import ResearchIdentityResolver


def setup(tmp_path):
    registry=RawRegistry.create(tmp_path/'registry')
    receipt=registry.register('positions','r1',tmp_path/'positions.jsonl',writer_epoch='synthetic',namespace='legacy-local',operation='register')
    repo=ProjectionRepository(tmp_path/'repo');revision=MetadataRevision(repo,'initial');revision.initialize();revision.begin(provenance={'source':'actual synthetic RawRegistry'})
    consumer=MetadataLogConsumer(registry,revision);consumer.initialize({'positions':'position'},origin_proof={'confirmed':'synthetic empty origin'})
    return registry,receipt,repo,revision,consumer


def append(registry,receipt,records):
    return registry.append('positions',b''.join(encoded(v.to_dict())+b'\n' for v in records),expected_receipt=hashed(encoded(receipt)),operation=str(uuid.uuid4()))


def drain(consumer):
    results=[]
    for _ in range(1000):
        result=consumer.step(expected_cursor=consumer.cursor(),max_records=1);results.append(result)
        assert result['decodes']<=1 and result['raw_bytes']<=65536
        if result['caught_up']:return results
    pytest.fail('receipt consumer unfinished')


def test_actual_receipts_resume_delta_without_old_raw_replay(tmp_path):
    registry,receipt,repo,revision,consumer=setup(tmp_path)
    records=[position(100,'A'),position(300,'B')];receipt=append(registry,receipt,records)
    drain(consumer);publish(revision)
    old_cursor=consumer.cursor()
    late=position(200,'C');receipt=append(registry,receipt,[late])
    nxt=MetadataRevision(repo,'next');nxt.begin(parent='initial',provenance={'source':'one new confirmed receipt'})
    resumed=MetadataLogConsumer(registry,nxt);resumed.initialize({'positions':'position'},origin_proof={'confirmed':'synthetic empty origin'})
    assert resumed.cursor()==old_cursor
    results=drain(resumed)
    assert sum(r['decodes'] for r in results)==1
    assert sum(r['raw_bytes'] for r in results)==receipt['end']-receipt['start']
    publish(nxt,'initial')
    assert RevisionMetadataReader(repo,'next').epochs()==ResearchIdentityResolver(records+[late]).epochs()
    assert RevisionMetadataReader(repo,'initial').epochs()==ResearchIdentityResolver(records).epochs()
    assert resumed.step(expected_cursor=resumed.cursor())['raw_bytes']==0


@pytest.mark.parametrize('boundary',['before_metadata_receipt_commit','after_metadata_receipt_commit'])
def test_receipt_and_tree_commit_together_after_restart(tmp_path,boundary):
    registry,receipt,repo,revision,consumer=setup(tmp_path)
    values=[position(100,'A'),position(200,'B')];append(registry,receipt,values)
    consumer.step(expected_cursor=consumer.cursor())  # confirmed registration
    old=consumer.cursor()
    def fault(stage):
        if stage==boundary:raise InjectedCrash('power loss')
    with pytest.raises(InjectedCrash):consumer.step(expected_cursor=old,max_records=1,fault=fault)
    resumed=MetadataLogConsumer(registry,MetadataRevision(ProjectionRepository(repo.path),'initial'))
    assert (resumed.cursor()==old)==boundary.startswith('before')
    drain(resumed);publish(resumed.revision)
    assert RevisionMetadataReader(repo,'initial').epochs()==ResearchIdentityResolver(values).epochs()


def test_input_rewrite_and_unknown_mapping_do_not_publish(tmp_path):
    registry,receipt,repo,revision,consumer=setup(tmp_path)
    receipt=append(registry,receipt,[position(100,'A')]);consumer.step(expected_cursor=consumer.cursor())
    path=tmp_path/'positions.jsonl';before=path.read_bytes();path.write_bytes(before.replace(b'"A"',b'"Z"'))
    from raw_registry import RegistryCorrupt
    with pytest.raises(RegistryCorrupt):consumer.step(expected_cursor=consumer.cursor())
    assert revision.state()['status']=='preparing'


def test_actual_maintenance_receipts_and_position_enrichment(tmp_path):
    from dataclasses import asdict
    from test_timeline import build,add
    from maintenance import MaintenanceEventLog
    from timeline import project_maintenance
    registry,receipt,repo,revision,consumer=setup(tmp_path)
    # New explicit scope is prepared before consuming, never broaden an old map.
    with repo._write() as db:db.execute('DELETE FROM metadata_registry_cursor')
    consumer.initialize({'positions':'position','maintenance':'maintenance'},origin_proof={'confirmed':'synthetic empty origin'})
    receipt=append(registry,receipt,[position(100,'A')])
    service,_,_=build(tmp_path)
    event=add(service,module_number=1,module_serial=None,occurred_at='1970-01-01T00:02:30+00:00')
    service.update(event.maintenance_event_id,expected_revision=1,changes={'title':'Revised'})
    records=MaintenanceEventLog(tmp_path/'maintenance.jsonl').read_all()
    m=registry.register('maintenance','r1',tmp_path/'confirmed-maintenance.jsonl',writer_epoch='synthetic',namespace='legacy-local',operation='maintenance-register')
    registry.append('maintenance',b'\n'+b''.join(encoded(asdict(r))+b'\n' for r in records),expected_receipt=hashed(encoded(m)),operation='maintenance-append')
    results=drain(consumer);publish(revision)
    assert sum(r['decodes'] for r in results)==3
    actual=RevisionMetadataReader(repo,'initial').query(timestamp_from='1970-01-01T00:00:00Z',timestamp_to='1970-01-02T00:00:00Z')
    assert actual==[project_maintenance(records[-1],'A')]
    assert records[-1].module_serial is None
