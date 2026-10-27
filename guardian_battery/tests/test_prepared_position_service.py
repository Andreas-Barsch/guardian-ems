import pytest
from application_writer import ApplicationWriter
from application_metadata import MetadataTail, HeadMetadataReader, ConfirmedPositionReader
from metadata_revision import MetadataRevision
from metadata_log_consumer import MetadataLogConsumer
from projection_repository import ProjectionRepository, Unavailable
from raw_registry import RawRegistry
from test_position_history import env, state
from test_metadata_revision import publish
from test_metadata_log_consumer import drain
from test_application_metadata import finish
from position_history import PositionHistoryValidationError, PositionHistoryConflictError


def prepared(tmp_path):
    event, service = env(tmp_path)
    registry = RawRegistry.create(tmp_path/'registry')
    writer = ApplicationWriter(registry,tmp_path,stream='metadata',writer_epoch='synthetic')
    service.log.append_backend = writer
    repo = ProjectionRepository(tmp_path/'repo')
    revision = MetadataRevision(repo,'initial'); revision.initialize()
    revision.begin(provenance={'source':'explicit empty synthetic position source'})
    mapping = {writer.source(service.log.path):'position'}
    origin = {'confirmed':'synthetic empty origin'}
    consumer = MetadataLogConsumer(registry,revision)
    consumer.initialize(mapping,origin_proof=origin)
    drain(consumer);publish(revision)
    tail = MetadataTail(repo,registry,head='current',mapping=mapping,origin_proof=origin)
    tail.initialize()
    service.read_backend = ConfirmedPositionReader(tail,HeadMetadataReader(repo,'current'),writer,service.log.path)
    return event,service,tail


def test_real_position_service_uses_metadata_for_cas_and_time_queries(tmp_path,monkeypatch):
    event,service,tail = prepared(tmp_path)
    monkeypatch.setattr(service.log,'read_all',lambda:pytest.fail('position service scanned full Raw log'))
    first = service.record(effective_at='2026-08-19T10:00:00Z',maintenance_event_id=event.maintenance_event_id,
        positions=state(**{'1':'A'}),expected_latest_snapshot_id=None)
    # A service write must not base its CAS on a stale metadata head.
    with pytest.raises(Unavailable,match='metadata pending'):
        service.record(effective_at='2026-08-20T10:00:00Z',maintenance_event_id=event.maintenance_event_id,
            positions=state(**{'1':'B'}),expected_latest_snapshot_id=None)
    finish(tail)
    assert service.current()==first
    assert service.at('2026-08-19T09:00:00Z') is None
    assert service.at('2026-08-19T10:00:00Z')==first
    with pytest.raises(PositionHistoryConflictError):
        service.record(effective_at='2026-08-20T10:00:00Z',maintenance_event_id=event.maintenance_event_id,
            positions=state(**{'1':'B'}),expected_latest_snapshot_id=None)
    with pytest.raises(PositionHistoryValidationError,match='conflicting'):
        service.record(effective_at='2026-08-19T10:00:00Z',maintenance_event_id=event.maintenance_event_id,
            positions=state(**{'1':'B'}),expected_latest_snapshot_id=first.position_history_id)
    second=service.record(effective_at='2026-08-20T10:00:00Z',maintenance_event_id=event.maintenance_event_id,
        positions=state(**{'1':'B'}),expected_latest_snapshot_id=first.position_history_id)
    finish(tail)
    assert service.list()==[first,second]
    assert service.current()==second
    assert service.position_at(1,'2026-08-19T12:00:00Z')=='A'


def test_initial_position_capture_rule_is_unchanged(tmp_path):
    event,service,tail=prepared(tmp_path)
    with pytest.raises(PositionHistoryValidationError,match='initial documentation'):
        service.record(effective_at='2026-08-18T10:00:00Z',maintenance_event_id=event.maintenance_event_id,
            positions=state(**{'1':'A'}),expected_latest_snapshot_id=None)


def test_long_position_history_uses_selected_indexes_and_preserves_retroactive_semantics(tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from uuid import UUID
    from types import SimpleNamespace
    from position_history import PositionSnapshot,PositionHistoryService
    from metadata_revision import MetadataRevision,RevisionMetadataReader
    from test_metadata_revision import begin,publish
    from metadata_tree import MetadataTree
    repo,rev=begin(tmp_path)
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    def snapshot(index,offset,serial):
        stamp=(start+timedelta(seconds=offset)).isoformat()
        return PositionSnapshot(1,'PHS-'+str(UUID(int=index)),stamp,stamp,'MEV-synthetic',state(**{'1':serial,'2':'UNCHANGED'}))
    values=[snapshot(i+1,i*10,('A','B',None)[i%3]) for i in range(300)]
    for sequence,offset in enumerate(range(0,len(values),16),1):
        rev.apply_page('position',[v.to_dict() for v in values[offset:offset+16]],source='p',sequence=sequence,
            expected_version=rev.state()['version'],provenance={'synthetic':True})
    publish(rev)
    original=PositionHistoryService(SimpleNamespace(read_all=lambda:values),None)
    reader=RevisionMetadataReader(repo,rev.revision)
    prepared=PositionHistoryService(None,None,read_backend=reader)
    assert prepared.list()==original.list()
    calls=[];page=MetadataTree.page
    def bounded(self,**kwargs):
        result=page(self,**kwargs);calls.append(len(result));return result
    monkeypatch.setattr(MetadataTree,'page',bounded)
    monkeypatch.setattr(reader,'position_snapshots',lambda:pytest.fail('compact lookup materialized all snapshots'))
    for second in (-1,0,10,15,2990,5000):
        at=(start+timedelta(seconds=second)).isoformat()
        assert prepared.serial_options(1,at)==original.serial_options(1,at)
    assert prepared.known_serials(1)==original.known_serials(1)
    assert prepared.last_documented_serials()==original.last_documented_serials()
    assert max(calls)<=2
    # A retroactive observation changes later transition starts; rebuild only
    # the explicit suffix and preserve the old immutable reader.
    nxt=MetadataRevision(repo,'retro');nxt.begin(parent=rev.revision,provenance={'synthetic':'retroactive'})
    late=snapshot(1000,2985,'C');values.append(late)
    nxt.apply_page('position',[late.to_dict()],source='p',sequence=20,expected_version=0,provenance={'synthetic':True})
    publish(nxt,rev.revision)
    prepared.read_backend=RevisionMetadataReader(repo,'retro')
    for second in (2980,2985,2990,5000):
        at=(start+timedelta(seconds=second)).isoformat()
        assert prepared.serial_options(1,at)==original.serial_options(1,at)
    assert prepared.last_documented_serials()==original.last_documented_serials()
    assert reader.position_known_serials(1)==['A','B']
