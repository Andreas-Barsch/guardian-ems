from datetime import datetime, timezone

import pytest

from maintenance import MaintenanceEventLog
from maintenance_service import MaintenanceRepository, MaintenanceService
from position_history import (PositionHistoryConflictError, PositionHistoryLog,
                              PositionHistoryService, PositionHistoryValidationError,
                              classify_stack_change, documented_identity_at,
                              observed_stack, update_observed_stack)
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


def test_backdated_append_keeps_chronological_current_and_future_concurrency(tmp_path):
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z",
                           maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"1": "SN-A"}), expected_latest_snapshot_id=None)
    current = service.record(effective_at="2026-08-20T10:00:00Z",
                             maintenance_event_id=event.maintenance_event_id,
                             positions=state(**{"1": "SN-B"}),
                             expected_latest_snapshot_id=first.position_history_id)
    service.record(effective_at="2026-08-19T12:00:00Z",
                   maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"1": "SN-C"}),
                   expected_latest_snapshot_id=current.position_history_id)
    assert service.current() == current
    later = service.record(effective_at="2026-08-21T10:00:00Z",
                           maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"1": "SN-D"}),
                           expected_latest_snapshot_id=current.position_history_id)
    assert service.current() == later


def test_conflicting_mapping_at_identical_effective_timestamp_is_rejected(tmp_path):
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z",
                           maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"1": "SN-A"}), expected_latest_snapshot_id=None)
    with pytest.raises(PositionHistoryValidationError, match="identical effective_at"):
        service.record(effective_at="2026-08-19T10:00:00Z",
                       maintenance_event_id=event.maintenance_event_id,
                       positions=state(**{"1": "SN-B"}),
                       expected_latest_snapshot_id=first.position_history_id)


def test_unchanged_later_mapping_does_not_create_a_history_state(tmp_path):
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z",
                           maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"1": "SN-A"}), expected_latest_snapshot_id=None)
    with pytest.raises(PositionHistoryValidationError, match="positions are unchanged"):
        service.record(effective_at="2026-08-20T10:00:00Z",
                       maintenance_event_id=event.maintenance_event_id,
                       positions=state(**{"1": "SN-A"}),
                       expected_latest_snapshot_id=first.position_history_id)
    assert service.list() == [first]


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


def test_current_api_keeps_historical_position_six_outside_expected_topology(
        tmp_path, monkeypatch):
    import position_history
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z",
                           maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"1": "A", "2": "B", "3": "C", "4": "D",
                                                   "5": "E", "6": "HISTORICAL"}),
                           expected_latest_snapshot_id=None)
    service.record(effective_at="2026-08-19T11:00:00Z",
                   maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"1": "A", "2": "B", "3": "C", "4": "D"}),
                   expected_latest_snapshot_id=first.position_history_id)
    monkeypatch.setattr(position_history, "_OBSERVED_STACK",
                        {1: "A", 2: "B", 3: "C", 4: "D", 5: None})
    monkeypatch.setattr(position_history, "_PRESENCE_SOURCES", {
        position: {"console": {"identity": serial, "timestamp": 100,
                                "source": "console"}}
        for position, serial in ((1, "A"), (2, "B"), (3, "C"), (4, "D"))
    })
    monkeypatch.setattr(position_history, "_COMMUNICATION_HEALTHY", True)
    response = PositionHistoryApi(service, module_count_provider=lambda: 5).handle(
        "GET", "/api/position-history/current")
    assert response.body["expected_module_count"] == 5
    assert response.body["presence"]["5"]["status"] == "absent"
    assert response.body["presence"]["6"]["status"] == "not_expected"
    assert response.body["snapshot"]["positions"]["5"] is None
    assert response.body["snapshot"]["positions"]["6"] is None
    assert response.body["documented"]["5"] == "E"
    assert response.body["documented"]["6"] == "HISTORICAL"


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


def test_documented_measurement_identity_never_backfills_from_the_future(tmp_path):
    event, service=env(tmp_path)
    first=service.record(effective_at="2026-08-19T10:00:00Z",maintenance_event_id=event.maintenance_event_id,
                         positions=state(**{"5":"SN-X"}),expected_latest_snapshot_id=None)
    service.record(effective_at="2026-08-20T10:00:00Z",maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"5":"SN-Y"}),expected_latest_snapshot_id=first.position_history_id)
    assert documented_identity_at(service.log.path,5,"2026-08-18T10:00:00Z") == (None,None)
    assert documented_identity_at(service.log.path,5,"2026-08-19T12:00:00Z") == ("SN-X",first.position_history_id)


def test_runtime_maintenance_identity_uses_position_history_at_event_time(tmp_path):
    from position_history import resolve_maintenance_event_identities
    event, service = env(tmp_path)
    first = service.record(effective_at="2026-08-19T10:00:00Z",
                           maintenance_event_id=event.maintenance_event_id,
                           positions=state(**{"5": "SN-X"}), expected_latest_snapshot_id=None)
    service.record(effective_at="2026-08-20T10:00:00Z",
                   maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"5": "SN-Y"}),
                   expected_latest_snapshot_id=first.position_history_id)
    raw = {**event.to_dict(), "occurred_at": "2026-08-19T12:00:00+00:00",
           "module_number": 5, "module_serial": None}
    resolved = resolve_maintenance_event_identities([raw], service.log.path)[0]
    assert resolved["resolved_module_serial"] == "SN-X"
    assert resolved["identity_status"] == "position_history"


def test_runtime_maintenance_identity_does_not_guess_before_first_snapshot(tmp_path):
    from position_history import resolve_maintenance_event_identities
    event, service = env(tmp_path)
    service.record(effective_at="2026-08-19T10:00:00Z",
                   maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"5": "SN-X"}), expected_latest_snapshot_id=None)
    raw = {**event.to_dict(), "occurred_at": "2026-08-18T12:00:00+00:00",
           "module_number": 5, "module_serial": None}
    resolved = resolve_maintenance_event_identities([raw], service.log.path)[0]
    assert resolved["resolved_module_serial"] is None
    assert resolved["identity_status"] == "unknown"


def test_observed_identity_requires_repeated_stable_bms_reads(monkeypatch):
    import position_history
    monkeypatch.setattr(position_history,"_OBSERVED_STACK",{})
    monkeypatch.setattr(position_history,"_OBSERVATION_CANDIDATES",{})
    monkeypatch.setattr(position_history,"_PRESENCE_SOURCES",{})
    monkeypatch.setattr(position_history,"_MISSING_CANDIDATES",{})
    monkeypatch.setattr(position_history,"_COMMUNICATION_HEALTHY",None)
    update_observed_stack({5:{"barcode":"SN-X"}}); update_observed_stack({5:{"barcode":"SN-Y"}})
    assert observed_stack().get(5) is None
    for _ in range(3): update_observed_stack({5:{"barcode":"SN-Y"}})
    assert observed_stack()[5] == "SN-Y"
    update_observed_stack({5:{}})
    assert observed_stack()[5] == "SN-Y"


def test_physical_serial_history_tracks_swaps_removal_and_reinsertion(tmp_path):
    event, service=env(tmp_path)
    first=service.record(effective_at="2026-08-19T10:00:00Z",maintenance_event_id=event.maintenance_event_id,
                         positions=state(**{"5":"SN-X","6":"SN-Y"}),expected_latest_snapshot_id=None)
    second=service.record(effective_at="2026-08-20T10:00:00Z",maintenance_event_id=event.maintenance_event_id,
                          positions=state(**{"5":"SN-Y","6":"SN-X"}),expected_latest_snapshot_id=first.position_history_id)
    third=service.record(effective_at="2026-08-21T10:00:00Z",maintenance_event_id=event.maintenance_event_id,
                         positions=state(**{"5":"SN-Y"}),expected_latest_snapshot_id=second.position_history_id)
    service.record(effective_at="2026-08-22T10:00:00Z",maintenance_event_id=event.maintenance_event_id,
                   positions=state(**{"2":"SN-X","5":"SN-Y"}),expected_latest_snapshot_id=third.position_history_id)
    x=next(item for item in service.serial_histories() if item["serial"]=="SN-X")
    assert [interval["position"] for interval in x["intervals"]] == [5,6,None,2]
    assert x["intervals"][0]["valid_to"] == "2026-08-20T10:00:00+00:00"


def test_stack_change_semantics_do_not_call_first_identification_a_replacement():
    empty=state(); initial=state(**{"1":"A","2":"B"})
    assert classify_stack_change(empty,initial)["kind"]=="initial_identification"
    assert classify_stack_change(initial,state(**{"1":"B","2":"A"}))["kind"]=="position_change"
    assert classify_stack_change(initial,state(**{"1":"A","2":"C"}))["kind"]=="module_replacement"
    later_identification=state(**{"1":"A","2":"B","3":"C"})
    assert classify_stack_change(initial,later_identification)["kind"]=="initial_identification"
    assert classify_stack_change(initial,later_identification,
                                 confirmed_empty_positions={"3"})["kind"]=="module_added"
    assert classify_stack_change(initial,state(**{"1":"A"}))["kind"]=="module_removed"
