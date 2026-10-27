"""Regression checks use temporary stores and a mocked browser, never HA."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from maintenance import MaintenanceEventLog
from maintenance_api import MaintenanceApi
from maintenance_service import MaintenanceRepository, MaintenanceService, MaintenanceNotFoundError
from maintenance_diagnostics import project_maintenance_boundaries
from position_history import resolve_maintenance_event_identities
from position_history_api import PositionHistoryApi
from test_maintenance_api import create_payload, request
from test_position_history import env, state
from test_history_ui import _run_node
from maintenance_ui import render_maintenance_html

ROUTE = "/api/maintenance/events"
NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def service_at(path):
    return MaintenanceService(MaintenanceRepository(MaintenanceEventLog(path)), clock=lambda: NOW)


def save(api, key, **changes):
    return request(api, "POST", ROUTE, create_payload(**changes),
                   headers={"Content-Type": "application/json", "Idempotency-Key": key})


def test_parallel_create_retry_and_restart_preserve_one_event_and_one_publish(tmp_path):
    path = tmp_path / "events.jsonl"
    published = []
    class Publisher:
        def publish_if_live(self, event):
            published.append(event.maintenance_event_id)
    key = str(uuid.uuid4())
    def send(_):
        return save(MaintenanceApi(service_at(path), Publisher()), key)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(send, range(16)))
    assert [item.status for item in responses].count(201) == 1
    assert [item.status for item in responses].count(200) == 15
    assert len({item.body["event"]["maintenance_event_id"] for item in responses}) == 1
    assert len(published) == len(path.read_text().splitlines()) == 1
    restarted = MaintenanceApi(service_at(path), Publisher())
    assert save(restarted, key).status == 200
    assert len(published) == 1
    assert save(restarted, key, title="Different request").status == 409
    # An intentionally new operation may have exactly the same content.
    assert save(restarted, str(uuid.uuid4())).status == 201
    assert len(path.read_text().splitlines()) == 2


def test_hard_delete_removes_all_target_revisions_preserves_other_bytes_and_blocks_late_retry(tmp_path):
    path = tmp_path / "events.jsonl"
    service = service_at(path)
    api = MaintenanceApi(service)
    key = str(uuid.uuid4())
    event = save(api, key).body["event"]
    other = save(api, str(uuid.uuid4())).body["event"]
    event_id = event["maintenance_event_id"]
    service.update(event_id, expected_revision=1, changes={"title": "Updated"})
    service.set_active(event_id, expected_revision=2, active=False)
    kept = b"".join(line for line in path.read_bytes().splitlines(keepends=True)
                    if json.loads(line)["maintenance_event_id"] != event_id)
    stale = request(api, "DELETE", ROUTE + "/" + event_id, {"expected_revision": 1})
    assert stale.status == 409
    assert len(path.read_text().splitlines()) == 4
    response = request(api, "DELETE", ROUTE + "/" + event_id, {"expected_revision": 3})
    assert response.status == 200 and response.body == {"deleted": True}
    assert path.read_bytes() == kept
    assert event_id.encode() not in path.read_bytes()
    assert service.get(other["maintenance_event_id"]).revision == 1
    assert request(api, "GET", ROUTE + "/" + event_id).status == 404
    assert request(api, "GET", ROUTE + "/" + event_id + "/history").status == 404
    assert request(api, "PATCH", ROUTE + "/" + event_id,
                   {"expected_revision": 3, "changes": {"title": "late"}}).status == 404
    assert save(MaintenanceApi(service_at(path)), key).status == 404
    # No event payload/ID is retained in the retry receipt.
    receipt = path.with_suffix(".jsonl.retired-requests").read_text()
    assert len(receipt.strip()) == 64
    assert event_id not in receipt and "Updated" not in receipt
    assert save(api, str(uuid.uuid4())).status == 201


def test_delete_failure_leaves_original_complete_and_retryable(tmp_path, monkeypatch):
    import maintenance
    path = tmp_path / "events.jsonl"
    api = MaintenanceApi(service_at(path))
    key = str(uuid.uuid4())
    event = save(api, key).body["event"]
    before = path.read_bytes()
    original_replace = maintenance.os.replace
    def fail(*args):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(maintenance.os, "replace", fail)
    response = request(api, "DELETE", ROUTE + "/" + event["maintenance_event_id"], {"expected_revision": 1})
    assert response.status == 500
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".maintenance-delete-*"))
    assert save(api, key).status == 200
    monkeypatch.setattr(maintenance.os, "replace", original_replace)
    assert request(api, "DELETE", ROUTE + "/" + event["maintenance_event_id"], {"expected_revision": 1}).status == 200


def test_delete_racing_update_has_one_consistent_outcome(tmp_path):
    from maintenance_service import MaintenanceConflictError
    path = tmp_path / "events.jsonl"
    service = service_at(path)
    event = save(MaintenanceApi(service), str(uuid.uuid4())).body["event"]
    event_id = event["maintenance_event_id"]
    def change(delete):
        try:
            if delete:
                service.delete(event_id, expected_revision=1)
            else:
                service.update(event_id, expected_revision=1, changes={"title": "changed"})
            return "ok"
        except (MaintenanceNotFoundError, MaintenanceConflictError):
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, [True, False]))
    assert sorted(outcomes) == ["conflict", "ok"]
    records = service.repository.log.read_all()
    assert len(records) in (0, 2)
    if records:
        assert service.get(event_id).revision == 2


def test_position_facts_survive_missing_maintenance_source(tmp_path):
    event, positions = env(tmp_path)
    snapshot = positions.record(effective_at="2026-08-19T10:00:00Z",
                                maintenance_event_id=event.maintenance_event_id,
                                positions=state(**{"1": "SERIAL-A"}), expected_latest_snapshot_id=None)
    before = positions.log.path.read_bytes()
    api = PositionHistoryApi(positions)
    assert api.handle("GET", "/api/position-history").body["snapshots"][0]["maintenance_event_available"] is True
    positions.maintenance_service.delete(event.maintenance_event_id, expected_revision=1)
    assert positions.log.path.read_bytes() == before
    assert positions.current() == snapshot
    assert positions.position_at(1, "2026-08-20T10:00:00Z") == "SERIAL-A"
    response = api.handle("GET", "/api/position-history")
    assert response.status == 200
    assert response.body["snapshots"][0]["maintenance_event_available"] is False
    assert api.handle("GET", "/api/position-history/current").body["snapshot"]["maintenance_event_available"] is False


def test_latest_revision_only_drives_diagnostic_context(tmp_path):
    service = service_at(tmp_path / "events.jsonl")
    event = service.create(occurred_at=NOW.isoformat(), category="module_added", title="Installed",
                           affected_system="Stack", module_serial="SERIAL-A", source={"kind": "manual"})
    def context():
        return project_maintenance_boundaries(resolve_maintenance_event_identities(
            service.repository.list(), tmp_path / "positions.jsonl"))
    assert len(context()) == 1
    service.set_active(event.maintenance_event_id, expected_revision=1, active=False)
    assert len(service.history(event.maintenance_event_id)) == 2
    assert context() == []
    service.set_active(event.maintenance_event_id, expected_revision=2, active=True)
    assert len(context()) == 1
    service.delete(event.maintenance_event_id, expected_revision=3)
    assert context() == []


def test_browser_double_submit_retry_delete_cancel_and_new_operation():
    html = render_maintenance_html(configuration_path="/")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    setup = r"""
const assert=require('node:assert/strict');
const crypto=require('node:crypto').webcrypto;
const nodes=new Map();
const document={addEventListener(){},getElementById(id){if(!nodes.has(id))nodes.set(id,{disabled:false,textContent:'',replaceChildren(){},querySelectorAll(){return [document.getElementById('save-button')]}});return nodes.get(id)}};
const window={history:{replaceState(){}},confirm:()=>false};
"""
    checks = r"""
(async()=>{
 let payload={title:'same'},requests=[],resolve,reject,messages=[],views=[];
 formPayload=()=>({...payload});setMessage=text=>messages.push(text);show=view=>views.push(view);
 renderDetail=event=>{state.current=event};historyData=()=>{};loadList=async()=>{};
 apiFetch=(target,options)=>{requests.push({target,options});return new Promise((yes,no)=>{resolve=yes;reject=no})};
 state.requestId=newRequestId();const key=state.requestId;
 const first=saveForm({preventDefault(){}});const second=saveForm({preventDefault(){}});
 await second;assert.equal(requests.length,1);assert.equal(document.getElementById('save-button').disabled,true);
 reject(new Error('response lost'));await first;
 assert.equal(state.saving,false);assert.equal(state.requestId,key);
 const retry=saveForm({preventDefault(){}});
 assert.equal(requests[1].options.headers['Idempotency-Key'],key);
 resolve({event:{maintenance_event_id:'MEV-one',revision:1}});await retry;
 assert.equal(messages.at(-1),'Eintrag gespeichert.');assert.equal(views.at(-1),'detail-view');
 // New form gets its own key, even for identical input.
 formValue=()=>{};setSerialOptions=()=>{};updateUtcPreview=()=>{};
 openForm();assert.notEqual(state.requestId,key);
 state.current={maintenance_event_id:'MEV-one',revision:1};
 await deleteEntry();assert.equal(requests.length,2); // cancel sends nothing
 window.confirm=()=>true;
 const deletion=deleteEntry();await deleteEntry();assert.equal(requests.length,3);
 assert.equal(requests[2].options.method,'DELETE');
 resolve({deleted:true});await deletion;
 assert.equal(state.current,null);assert.equal(messages.at(-1),'Eintrag gelöscht.');
 // Deactivation remains available.
 state.current={maintenance_event_id:'MEV-two',revision:1,active:true};
 const deactivate=toggleActive();assert.ok(requests.at(-1).target.endsWith('/deactivate'));
 resolve({event:{maintenance_event_id:'MEV-two',revision:2,active:false}});await deactivate;
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    _run_node(setup + script + checks)


@pytest.mark.parametrize("key", ["", "invalid", "../events", "ä"])
def test_invalid_idempotency_key_is_rejected_without_writing(tmp_path, key):
    path = tmp_path / "events.jsonl"
    assert save(MaintenanceApi(service_at(path)), key).status == 400
    assert not path.exists()


def test_retry_returns_current_revision_without_reverting_edits(tmp_path):
    service = service_at(tmp_path / "events.jsonl")
    api = MaintenanceApi(service)
    key = str(uuid.uuid4())
    event = save(api, key).body["event"]
    service.update(event["maintenance_event_id"], expected_revision=1, changes={"title": "corrected"})
    retry = save(api, key)
    assert retry.status == 200
    assert retry.body["event"]["revision"] == 2
    assert retry.body["event"]["title"] == "corrected"
    assert len(service.repository.log.read_all()) == 2


def test_legacy_event_deletes_without_migration_or_retry_receipt(tmp_path):
    path = tmp_path / "events.jsonl"
    service = service_at(path)
    event = service.create(**create_payload(), source={"kind": "manual"})
    service.delete(event.maintenance_event_id, expected_revision=1)
    assert path.read_bytes() == b""
    assert not path.with_suffix(".jsonl.retired-requests").exists()
    assert service.repository.list(include_archived=True) == []


def test_module_source_link_is_removed_only_when_missing():
    from module_information_ui import render_module_information_html
    from test_history_ui import _javascript_function
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    source = _javascript_function(html, "maintenanceSource")
    script = "const assert=require('node:assert/strict');const esc=value=>String(value);const maintenanceHref=id=>'maintenance?event_id='+id;" + source
    script += "assert.ok(maintenanceSource({maintenance_event_id:'MEV-a',maintenance_event_available:true},'date').includes('<a '));"
    script += "const missing=maintenanceSource({maintenance_event_id:'MEV-a',maintenance_event_available:false},'date');assert.ok(!missing.includes('<a '));assert.ok(missing.includes('Eintrag gelöscht'));"
    _run_node(script)
