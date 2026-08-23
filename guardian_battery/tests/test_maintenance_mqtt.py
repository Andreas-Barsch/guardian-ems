import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from maintenance import MaintenanceEventLog, new_maintenance_event
from maintenance_api import MaintenanceApi
from maintenance_mqtt import (
    DISCOVERY_TOPIC,
    LIVE_EVENT_TOLERANCE_SECONDS,
    MaintenanceMqttPublisher,
    is_live_create,
)
from maintenance_service import MaintenanceRepository, MaintenanceService


CREATED = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
HEADERS = {"Content-Type": "application/json"}


class Client:
    def __init__(self, fail_event=False):
        self.calls = []
        self.fail_event = fail_event

    def publish(self, topic, payload, retain=False):
        if self.fail_event and topic.endswith("/event/maintenance"):
            raise ConnectionError("broker unavailable")
        self.calls.append((topic, json.loads(payload), retain))


class FailedResultClient(Client):
    def publish(self, topic, payload, retain=False):
        if topic.endswith("/event/maintenance"):
            return type("PublishResult", (), {"rc": 4})()
        return super().publish(topic, payload, retain)


def event(*, occurred_at=CREATED, source=None, **fields):
    return new_maintenance_event(
        occurred_at=occurred_at, category="inspection", title="Kontakte geprüft",
        affected_system="Pylontech Stack", module_number=3, cell_number=7,
        source=source or {"kind": "manual"}, now=CREATED, **fields,
    )


def api_env(tmp_path, client=None):
    service = MaintenanceService(
        MaintenanceRepository(MaintenanceEventLog(tmp_path / "maintenance.jsonl")),
        clock=lambda: CREATED,
    )
    mqtt_client = client or Client()
    publisher = MaintenanceMqttPublisher(mqtt_client, "guardian")
    return MaintenanceApi(service, publisher), service, mqtt_client


def request(api, method, target, payload):
    return api.handle(method, target, HEADERS, json.dumps(payload).encode())


def create_payload(occurred_at="2026-08-20T09:58:00Z"):
    return {"occurred_at": occurred_at, "category": "inspection",
            "title": "Kontakte geprüft", "affected_system": "Pylontech Stack",
            "module_number": 3, "cell_number": 7}


def test_discovery_defines_real_event_entity_without_emitting_event():
    client = Client()
    publisher = MaintenanceMqttPublisher(client, "guardian/")
    device = {"identifiers": ["guardian_battery"], "name": "Guardian Battery"}

    publisher.discovery(device)

    assert len(client.calls) == 1
    topic, payload, retained = client.calls[0]
    assert topic == DISCOVERY_TOPIC
    assert retained is True
    assert payload["state_topic"] == "guardian/battery/event/maintenance"
    assert payload["event_types"] == ["maintenance"]
    assert payload["unique_id"] == "guardian_battery_maintenance_event"
    assert payload["device"] == device


def test_live_rule_is_conservative_and_central():
    assert LIVE_EVENT_TOLERANCE_SECONDS == 300
    assert is_live_create(event()) is True
    assert is_live_create(event(occurred_at=CREATED - timedelta(minutes=5))) is True
    assert is_live_create(event(occurred_at=CREATED - timedelta(seconds=301))) is False
    assert is_live_create(event(occurred_at=CREATED + timedelta(seconds=1))) is False
    assert is_live_create(event(source={"kind": "legacy_import"})) is False
    assert is_live_create(replace(event(), revision=2, updated_at=CREATED.isoformat())) is False


def test_live_api_create_publishes_exactly_once_with_compact_payload(tmp_path):
    api, _, client = api_env(tmp_path)
    response = request(api, "POST", "/api/maintenance/events", create_payload())

    assert response.status == 201
    assert len(client.calls) == 1
    topic, payload, retained = client.calls[0]
    created = response.body["event"]
    assert topic == "guardian/battery/event/maintenance"
    assert retained is False
    assert payload == {
        "event_type": "maintenance",
        "maintenance_event_id": created["maintenance_event_id"],
        "category": "inspection", "title": "Kontakte geprüft",
        "occurred_at": "2026-08-20T09:58:00+00:00",
        "created_at": "2026-08-20T10:00:00+00:00",
        "affected_system": "Pylontech Stack", "revision": 1,
        "deep_link": f"maintenance?event_id={created['maintenance_event_id']}",
        "guardian_version": "0.7.4", "module_number": 3, "cell_number": 7,
    }


def test_optional_module_and_cell_are_omitted(tmp_path):
    api, _, client = api_env(tmp_path)
    payload = create_payload()
    payload.pop("module_number"); payload.pop("cell_number")
    assert request(api, "POST", "/api/maintenance/events", payload).status == 201
    mqtt_payload = client.calls[0][1]
    assert "module_number" not in mqtt_payload and "cell_number" not in mqtt_payload


def test_backfill_update_archive_restore_never_publish(tmp_path):
    api, _, client = api_env(tmp_path)
    backfill = request(api, "POST", "/api/maintenance/events",
                       create_payload("2024-04-05T09:00:00Z"))
    assert backfill.status == 201 and client.calls == []

    live = request(api, "POST", "/api/maintenance/events", create_payload())
    event_id = live.body["event"]["maintenance_event_id"]
    assert len(client.calls) == 1
    updated = request(api, "PATCH", f"/api/maintenance/events/{event_id}",
                      {"expected_revision": 1, "changes": {"title": "Bearbeitet"}})
    archived = request(api, "POST", f"/api/maintenance/events/{event_id}/archive",
                       {"expected_revision": updated.body["event"]["revision"]})
    restored = request(api, "POST", f"/api/maintenance/events/{event_id}/restore",
                       {"expected_revision": archived.body["event"]["revision"]})
    assert updated.status == archived.status == restored.status == 200
    assert len(client.calls) == 1


def test_active_inactive_toggle_never_publishes(tmp_path):
    api, _, client = api_env(tmp_path)
    live = request(api, "POST", "/api/maintenance/events", create_payload())
    event_id = live.body["event"]["maintenance_event_id"]
    inactive = request(api, "POST", f"/api/maintenance/events/{event_id}/deactivate",
                       {"expected_revision": 1})
    active = request(api, "POST", f"/api/maintenance/events/{event_id}/activate",
                     {"expected_revision": 2})
    assert inactive.status == active.status == 200
    assert len(client.calls) == 1


def test_reload_restart_and_import_do_not_publish(tmp_path):
    api, service, client = api_env(tmp_path)
    live = request(api, "POST", "/api/maintenance/events", create_payload())
    assert live.status == 201 and len(client.calls) == 1

    reloaded = MaintenanceService(MaintenanceRepository(service.repository.log))
    assert len(reloaded.list()) == 1
    MaintenanceMqttPublisher(client, "guardian")
    assert len(client.calls) == 1

    imported = event(source={"kind": "legacy_import"})
    service.repository.import_revision(imported, expected_revision=0)
    assert len(client.calls) == 1


def test_mqtt_failure_does_not_lose_event_or_fail_http_create(tmp_path):
    client = Client(fail_event=True)
    api, service, _ = api_env(tmp_path, client)

    response = request(api, "POST", "/api/maintenance/events", create_payload())

    assert response.status == 201
    event_id = response.body["event"]["maintenance_event_id"]
    assert service.get(event_id).maintenance_event_id == event_id
    assert client.calls == []


def test_mqtt_error_result_does_not_fail_persisted_create(tmp_path):
    client = FailedResultClient()
    api, service, _ = api_env(tmp_path, client)
    response = request(api, "POST", "/api/maintenance/events", create_payload())
    assert response.status == 201
    assert len(service.list()) == 1
