from datetime import datetime, timezone

import pytest

from maintenance import MaintenanceEventLog
from maintenance_service import MaintenanceRepository, MaintenanceService
from position_history import (PositionHistoryConflictError, PositionHistoryLog,
                              PositionHistoryService, PositionHistoryValidationError)
from position_history_api import PositionHistoryApi


def env(tmp_path):
    maintenance = MaintenanceService(MaintenanceRepository(MaintenanceEventLog(tmp_path / "events.jsonl")),
                                     clock=lambda: datetime(2026, 8, 19, 10, tzinfo=timezone.utc))
    event = maintenance.create(occurred_at="2026-08-19T10:00:00Z", category="module_replacement",
                               title="Stack geändert", affected_system="Stack", source={"kind": "manual"})
    service = PositionHistoryService(PositionHistoryLog(tmp_path / "positions.jsonl"), maintenance,
                                     clock=lambda: datetime(2026, 8, 19, 10, tzinfo=timezone.utc))
    return event, service


def state(**changes):
    value = {str(index): None for index in range(1, 7)}
    value.update(changes)
    return value


def test_append_only_time_queries_and_physical_identity(tmp_path):
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z", maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"5": "X", "6": "Y"}), expected_latest_snapshot_id=None)
    second = service.record(effective_at="2026-08-20T10:00:00Z", maintenance_event_id=event.maintenance_event_id,
                            positions=state(**{"5": "Y"}), expected_latest_snapshot_id=first.position_history_id)
    assert service.position_at(5, "2026-08-19T12:00:00Z") == "X"
    assert service.serial_at("Y", "2026-08-19T12:00:00Z") == 6
    assert service.position_at(5, "2026-08-20T12:00:00Z") == "Y"
    assert service.serial_at("X", "2026-08-20T12:00:00Z") is None
    assert service.known_serials(5) == ["X", "Y"]
    assert len((tmp_path / "positions.jsonl").read_text().splitlines()) == 2
    assert service.current() == second


def test_full_snapshot_validation_concurrency_and_divergence(tmp_path):
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z", maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"1": "A"}), expected_latest_snapshot_id=None)
    with pytest.raises(PositionHistoryConflictError):
        service.record(effective_at="2026-08-20T10:00:00Z", maintenance_event_id=event.maintenance_event_id,
                       positions=state(**{"1": "B"}), expected_latest_snapshot_id=None)
    assert service.divergence({1: "B"})[0] == {"module_number": 1, "documented_serial": "A", "observed_serial": "B"}
    assert first.maintenance_event_id == event.maintenance_event_id


def test_api_exposes_history_resolution_and_requires_maintenance(tmp_path):
    event, service = env(tmp_path)
    api = PositionHistoryApi(service)
    payload = {"effective_at": "2026-08-19T10:00:00Z", "maintenance_event_id": event.maintenance_event_id,
               "positions": state(**{"5": "SER-5"}), "expected_latest_snapshot_id": None}
    import json
    response = api.handle("POST", "/api/position-history", {"Content-Type": "application/json"}, json.dumps(payload).encode())
    assert response.status == 201
    result = api.handle("GET", "/api/position-history/resolve?at=2026-08-19T11:00:00Z&module_number=5")
    assert result.body["serial"] == "SER-5"
    known = api.handle("GET", "/api/position-history/known-serials?module_number=5&at=2026-08-19T11:00:00Z")
    assert known.body["effective_serial"] == "SER-5"
    assert known.body["known_serials"] == ["SER-5"]


def test_initial_snapshot_cannot_invent_retrospective_identity(tmp_path):
    event, service = env(tmp_path)
    with pytest.raises(PositionHistoryValidationError, match="capture time"):
        service.record(effective_at="2020-01-01T00:00:00Z", maintenance_event_id=event.maintenance_event_id,
                       positions=state(**{"5": "UNKNOWN-BACKFILL"}), expected_latest_snapshot_id=None)
    assert service.list() == []


def test_serial_options_are_unique_and_temporally_classified(tmp_path):
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z", maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"5": "SN-A"}), expected_latest_snapshot_id=None)
    second = service.record(effective_at="2026-08-19T12:00:00Z", maintenance_event_id=event.maintenance_event_id,
                            positions=state(**{"5": "SN-X"}), expected_latest_snapshot_id=first.position_history_id)
    service.record(effective_at="2026-08-20T10:00:00Z", maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"5": "SN-Y"}), expected_latest_snapshot_id=second.position_history_id)
    assert service.serial_options(5, "2026-08-19T18:00:00Z") == [
        {"serial": "SN-X", "relationship": "effective"},
        {"serial": "SN-A", "relationship": "earlier"},
        {"serial": "SN-Y", "relationship": "later"},
    ]
