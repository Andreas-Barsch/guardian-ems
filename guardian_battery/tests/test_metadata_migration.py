class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import hashlib
import json
import pytest
from test_prepared_read_metadata import resolver
from projection_repository import ProjectionRepository, Conflict
from read_metadata_index import PreparedMetadata
from metadata_migration import MetadataMigration
from research_budget import ResearchQueryError


def setup(tmp_path):
    original=resolver(); path=tmp_path/'positions.jsonl'
    path.write_text(''.join(json.dumps(s.to_dict())+'\n' for s in reversed(original.snapshots)))
    repo=ProjectionRepository(tmp_path/'repo.sqlite'); job=MetadataMigration(repo,'revision');job.initialize()
    job.begin([{'kind':'position','path':str(path),'origin':'synthetic full source'}],provenance={'fixture':'authoritative positions'})
    return original,path,job,PreparedMetadata(repo,'revision')


def drain(job):
    for _ in range(100):
        state=job.status()
        result=job.step(expected_version=state['version'],rows=1)
        assert result['rows']<=1 and result['bytes']<=65536
        if result['complete']:return
    pytest.fail('unfinished')


def test_bounded_resumable_identity_equals_existing_resolver(tmp_path):
    original,path,job,prepared=setup(tmp_path); before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ResearchQueryError):prepared.epochs()
    job.step(expected_version=0,rows=1)
    reopened=MetadataMigration(ProjectionRepository(job.repository.path),'revision');drain(reopened)
    assert prepared.epochs()==original.epochs()
    for snapshot in original.snapshots:
        assert prepared.topology_at(snapshot.effective_at)==original.topology_at(snapshot.effective_at)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    path.unlink()  # confirmed completed state never rebuilds or rereads inputs
    assert reopened.step(expected_version=reopened.status()['version'])['rows']==0


@pytest.mark.parametrize('stage',['before_page_commit','after_page_commit'])
def test_page_crash_restart_and_cas(tmp_path,stage):
    original,path,job,prepared=setup(tmp_path)
    def fail(point):
        if point==stage:raise InjectedCrash('power loss')
    with pytest.raises(InjectedCrash):job.step(expected_version=0,rows=1,fault=fail)
    state=job.status();assert state['version']==(0 if stage.startswith('before') else 1)
    drain(job);assert prepared.epochs()==original.epochs()
    with pytest.raises(Conflict):job.step(expected_version=0)


def test_changed_source_and_contract_never_publish(tmp_path):
    _,path,job,prepared=setup(tmp_path)
    job.step(expected_version=0,rows=1);path.write_text(path.read_text()+'\n')
    with pytest.raises(Conflict,match='source changed'):job.step(expected_version=1)
    with pytest.raises(ResearchQueryError):prepared.epochs()
    with pytest.raises(ValueError):job.begin([],provenance={},semantics='other')


def test_technical_uses_existing_projection_and_explicit_empty_identity(tmp_path):
    from timeline import TechnicalEventSource
    path=tmp_path/'events.jsonl';rows=[{'type':'status_changed','timestamp':100.,'from':None,'to':'normal'}]
    path.write_text(json.dumps(rows[0])+'\n')
    repo=ProjectionRepository(tmp_path/'db');job=MetadataMigration(repo,'technical');job.initialize()
    job.begin([{'kind':'technical','path':str(path),'origin':'synthetic complete log'}],provenance={'position_scope':'confirmed empty'})
    drain(job);prepared=PreparedMetadata(repo,'technical')
    assert prepared.epochs()==[]
    assert prepared.query(timestamp_from='1970-01-01T00:00:00Z',timestamp_to='1970-01-02T00:00:00Z')==[TechnicalEventSource._project(rows[0],1)]


@pytest.mark.parametrize('stage',['before_page_commit','after_page_commit'])
def test_each_page_boundary_restart_matches_original(tmp_path,stage):
    original,path,job,prepared=setup(tmp_path)
    for _ in range(50):
        state=job.status()
        if state['phase']=='complete':break
        def fail(point):
            if point==stage:raise InjectedCrash('power loss')
        with pytest.raises(InjectedCrash):job.step(expected_version=state['version'],rows=1,fault=fail)
        job=MetadataMigration(ProjectionRepository(job.repository.path),'revision')
        if stage=='before_page_commit':job.step(expected_version=job.status()['version'],rows=1)
    assert job.status()['phase']=='complete'
    assert prepared.epochs()==original.epochs()


def test_empty_lines_and_source_page_budget(tmp_path):
    repo=ProjectionRepository(tmp_path/'db');path=tmp_path/'technical.jsonl'
    path.write_text('\n'+json.dumps({'type':'status_changed','timestamp':10,'from':None,'to':'ok'})+'\n')
    job=MetadataMigration(repo,'blank');job.initialize()
    job.begin([{'kind':'technical','path':str(path),'origin':'synthetic full log'}],provenance={'scope':'technical; positions empty'})
    drain(job)
    value=PreparedMetadata(repo,'blank')
    assert value.query(timestamp_from='1970-01-01T00:00:00Z',timestamp_to='1970-01-02T00:00:00Z')[0].projection_key=='technical:2'


def test_same_effective_time_and_absence_epochs_match(tmp_path):
    from position_history import PositionSnapshot
    from research_identity import ResearchIdentityResolver
    from uuid import UUID
    records=[]
    for i,positions in enumerate([{'1':'A'}, {'2':'A','3':'B'}, {}, {'1':'B'}, {'1':'B'}],1):
        records.append(PositionSnapshot(1,'PHS-'+str(UUID(int=i)),
            '2026-10-01T00:00:00Z' if i<3 else f'2026-10-{i:02}T00:00:00Z',
            f'2026-10-{i:02}T01:00:00Z','MEV-synthetic',{str(p):positions.get(str(p)) for p in range(1,7)}))
    path=tmp_path/'positions';path.write_text(''.join(json.dumps(s.to_dict())+'\n' for s in reversed(records)))
    repo=ProjectionRepository(tmp_path/'db');job=MetadataMigration(repo,'ties');job.initialize()
    job.begin([{'kind':'position','path':str(path),'origin':'synthetic complete'}],provenance={'scope':'synthetic'})
    drain(job);prepared=PreparedMetadata(repo,'ties');original=ResearchIdentityResolver(records)
    assert prepared.epochs()==original.epochs()
    for record in records:assert prepared.topology_at(record.effective_at)==original.topology_at(record.effective_at)
