import uuid
import pytest
from application_writer import ApplicationWriter
from application_metadata import MetadataTail,HeadMetadataReader,ConfirmedMaintenanceReader
from metadata_revision import MetadataRevision
from metadata_log_consumer import MetadataLogConsumer
from projection_repository import ProjectionRepository,Unavailable
from raw_registry import RawRegistry
from test_maintenance_service import service as ordinary_service,create
from test_metadata_revision import publish
from test_metadata_log_consumer import drain
from test_application_metadata import finish
from maintenance_service import MaintenanceConflictError,MaintenanceRequestConflictError


def prepared(tmp_path):
    service,repository,log=ordinary_service(tmp_path)
    registry=RawRegistry.create(tmp_path/'registry')
    writer=ApplicationWriter(registry,tmp_path,stream='metadata',writer_epoch='synthetic')
    log.append_backend=writer
    repo=ProjectionRepository(tmp_path/'repo')
    revision=MetadataRevision(repo,'initial');revision.initialize()
    revision.begin(provenance={'source':'explicit empty synthetic maintenance source'})
    mapping={writer.source(log.path):'maintenance',writer.source(log.path.with_suffix(log.path.suffix+'.retired-requests')):'retired'};origin={'confirmed':'synthetic empty origin'}
    consumer=MetadataLogConsumer(registry,revision);consumer.initialize(mapping,origin_proof=origin)
    drain(consumer);publish(revision)
    tail=MetadataTail(repo,registry,head='current',mapping=mapping,origin_proof=origin);tail.initialize()
    repository.read_backend=ConfirmedMaintenanceReader(tail,HeadMetadataReader(repo,'current'),writer,log.path)
    from prepared_maintenance_mutation import PreparedMaintenanceMutation
    log.mutation_backend=PreparedMaintenanceMutation(log,repository.read_backend)
    return service,log,tail


def test_existing_service_revision_archive_and_list_without_raw_replay(tmp_path,monkeypatch):
    service,log,tail=prepared(tmp_path)
    monkeypatch.setattr(log,'read_all',lambda:pytest.fail('maintenance service scanned old Raw'))
    first=create(service)
    # Existing create-then-reference semantics remain available before the
    # background metadata catches up, through this exact confirmed tip only.
    assert service.get(first.maintenance_event_id)==first
    finish(tail)
    assert service.get(first.maintenance_event_id)==first
    with pytest.raises(MaintenanceConflictError):
        service.update(first.maintenance_event_id,expected_revision=0,changes={'title':'changed'})
    second=service.update(first.maintenance_event_id,expected_revision=1,changes={'title':'changed'})
    finish(tail)
    assert service.history(first.maintenance_event_id)==(first,second)
    assert service.list()==[second]
    archived=service.archive(first.maintenance_event_id,expected_revision=2)
    finish(tail)
    assert service.list()==[]
    assert service.list(include_archived=True)==[archived]


def test_existing_create_once_uses_indexed_original_request(tmp_path,monkeypatch):
    service,log,tail=prepared(tmp_path)
    monkeypatch.setattr(log,'read_all',lambda:pytest.fail('request lookup scanned old Raw'))
    request=str(uuid.uuid4())
    fields=dict(occurred_at='2026-08-20T08:00:00Z',category='maintenance',title='test',affected_system='Stack')
    event,created=service.create_once(request,**fields)
    assert created
    finish(tail)
    assert service.create_once(request,**fields)==(event,False)
    with pytest.raises(MaintenanceRequestConflictError):
        service.create_once(request,**dict(fields,title='different'))


@pytest.mark.parametrize('old_count',[1,300])
def test_http_selected_window_skips_unrelated_old_events(tmp_path,monkeypatch,old_count):
    from maintenance_api import MaintenanceApi
    from maintenance import new_maintenance_event
    service,log,tail=prepared(tmp_path)
    # Explicit fixture writes; no HTTP preparation or repair is allowed.
    for _ in range(old_count):
        log.append(new_maintenance_event(occurred_at='2020-01-01T00:00:00Z',
            category='maintenance',title='old',affected_system='Stack'))
    finish(tail)
    selected=create(service,occurred_at='2026-08-20T08:00:00Z')
    finish(tail)
    monkeypatch.setattr(log,'read_all',lambda:pytest.fail('window replayed Raw'))
    backend=service.repository.read_backend
    raw_range=backend.writer.registry._raw_range
    monkeypatch.setattr(backend.writer.registry,'_raw_range',lambda *args:pytest.fail('published window read Raw'))
    # Count actual original-event decodes, not just the response length.
    from maintenance import MaintenanceEvent
    original=MaintenanceEvent.from_dict
    decoded=[]
    def decode(value):
        decoded.append(value['maintenance_event_id'])
        return original(value)
    monkeypatch.setattr(MaintenanceEvent,'from_dict',decode)
    response=MaintenanceApi(service).handle('GET',
        '/api/maintenance/events?occurred_from=2026-08-20T08:00:00Z&occurred_to=2026-08-20T08:00:00Z')
    assert response.status==200,response.body
    assert response.body['pagination']['total']==1
    assert decoded==[selected.maintenance_event_id]
    monkeypatch.setattr(backend.writer.registry,'_raw_range',raw_range)
    # Moving the same event outside the window removes its old ordered entry.
    service.update(selected.maintenance_event_id,expected_revision=1,
        changes={'occurred_at':'2026-08-21T08:00:00Z'})
    finish(tail)
    assert service.list(occurred_from='2026-08-20T08:00:00Z',occurred_to='2026-08-20T08:00:00Z')==[]


def test_confirmed_delete_removes_all_revisions_and_preserves_retry_digest(tmp_path):
    from maintenance_service import MaintenanceNotFoundError
    service,log,tail=prepared(tmp_path)
    request=str(uuid.uuid4())
    fields=dict(occurred_at='2026-08-20T08:00:00Z',category='maintenance',title='delete me',affected_system='Stack')
    event,_=service.create_once(request,**fields);finish(tail)
    service.update(event.maintenance_event_id,expected_revision=1,changes={'title':'second revision'});finish(tail)
    other=create(service,title='keep me');finish(tail)
    before=log.path.read_bytes()
    service.delete(event.maintenance_event_id,expected_revision=2)
    expected=b''.join(line for line in before.splitlines(keepends=True) if event.maintenance_event_id.encode() not in line)
    assert log.path.read_bytes()==expected
    assert service.list()==[other]
    with pytest.raises(MaintenanceNotFoundError):service.get(event.maintenance_event_id)
    with pytest.raises(MaintenanceNotFoundError,match='deleted'):service.create_once(request,**fields)
    import hashlib
    ledger=log.path.with_suffix(log.path.suffix+'.retired-requests')
    assert ledger.read_bytes()==hashlib.sha256(request.encode('ascii')).hexdigest().encode('ascii')+b'\n'
    assert not log.mutation_backend.pending()


@pytest.mark.parametrize('stage',['after_delete_intent','after_retired_request','after_deleted_file','after_deleted_metadata'])
def test_requested_delete_resumes_after_interruption_without_duplicate_ledger(tmp_path,stage):
    from prepared_maintenance_mutation import PreparedMaintenanceMutation
    service,log,tail=prepared(tmp_path)
    request=str(uuid.uuid4())
    fields=dict(occurred_at='2026-08-20T08:00:00Z',category='maintenance',title='delete',affected_system='Stack')
    event,_=service.create_once(request,**fields);finish(tail)
    class Interrupted(Exception):pass
    log.mutation_backend.fault=lambda value:(_ for _ in ()).throw(Interrupted()) if value==stage else None
    with pytest.raises(Interrupted):service.delete(event.maintenance_event_id,expected_revision=1)
    log.mutation_backend=PreparedMaintenanceMutation(log,service.repository.read_backend)
    for _ in range(100):
        if not log.mutation_backend.pending():break
        log.mutation_backend.step()
    else:pytest.fail('deletion recovery did not complete')
    assert service.list()==[] and log.path.read_bytes()==b''
    assert len(log.path.with_suffix(log.path.suffix+'.retired-requests').read_text().splitlines())==1



def reopen_prepared_service(root):
    from pathlib import Path
    from prepared_maintenance_mutation import PreparedMaintenanceMutation
    root=Path(root)
    service,repository,log=ordinary_service(root)
    writer=ApplicationWriter(RawRegistry(root/'registry'),root,stream='metadata',writer_epoch='synthetic')
    log.append_backend=writer
    repo=ProjectionRepository(root/'repo')
    mapping={writer.source(log.path):'maintenance',writer.source(log.path.with_suffix(log.path.suffix+'.retired-requests')):'retired'}
    tail=MetadataTail(repo,writer.registry,head='current',mapping=mapping,origin_proof={'confirmed':'synthetic empty origin'})
    repository.read_backend=ConfirmedMaintenanceReader(tail,HeadMetadataReader(repo,'current'),writer,log.path)
    log.mutation_backend=PreparedMaintenanceMutation(log,repository.read_backend)
    return service,log


@pytest.mark.parametrize('stage',['after_delete_intent','after_retired_request','after_deleted_file','after_deleted_metadata'])
def test_delete_coordinator_real_process_exit_and_second_process_recovery(tmp_path,stage):
    import subprocess,sys,os
    from pathlib import Path
    service,log,tail=prepared(tmp_path)
    event,_=service.create_once(str(uuid.uuid4()),occurred_at='2026-08-20T08:00:00Z',category='maintenance',title='delete',affected_system='Stack')
    finish(tail)
    preserved=create(service,title='preserved');finish(tail)
    expected=b''.join(line for line in log.path.read_bytes().splitlines(keepends=True) if event.maintenance_event_id.encode() not in line)
    code="""import sys,os
sys.path.insert(0,sys.argv[1])
from test_prepared_maintenance_service import reopen_prepared_service
service,log=reopen_prepared_service(sys.argv[2])
if sys.argv[3]=='recover':
 for _ in range(300):
  if not log.mutation_backend.pending():break
  log.mutation_backend.step()
 else:raise AssertionError('recovery not complete')
else:
 log.mutation_backend.fault=lambda name:os._exit(77) if name==sys.argv[3] else None
 service.delete(sys.argv[4],expected_revision=1)
"""
    args=[sys.executable,'-c',code,str(Path(__file__).parent),str(tmp_path)]
    crashed=subprocess.run([*args,stage,event.maintenance_event_id],capture_output=True,text=True,timeout=20)
    assert crashed.returncode==77,crashed.stderr
    recovered=subprocess.run([*args,'recover'],capture_output=True,text=True,timeout=20)
    assert recovered.returncode==0,recovered.stderr
    fresh,newlog=reopen_prepared_service(tmp_path)
    assert fresh.list()==[preserved] and newlog.path.read_bytes()==expected
    assert len(newlog.path.with_suffix(newlog.path.suffix+'.retired-requests').read_text().splitlines())==1
    assert not newlog.mutation_backend.pending()
