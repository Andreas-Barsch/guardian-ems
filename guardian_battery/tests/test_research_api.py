import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest

from position_history import PositionSnapshot
from research_api import GuardianResearchApi, ResearchPaths, research_envelope
from research_identity import ResearchIdentityResolver
from hycube_evidence import policy_observation


def snapshot(at, positions):
    return PositionSnapshot(schema_version=1,
        position_history_id="PHS-" + str(uuid.uuid4()), effective_at=at, created_at=at,
        maintenance_event_id="MEV-" + str(uuid.uuid4()),
        positions={str(number): positions.get(number) for number in range(1, 7)})


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def environment(tmp_path):
    positions = tmp_path / "position.jsonl"
    first = snapshot("2026-09-10T00:00:00+00:00", {4: "SERIAL-M4", 5: "SERIAL-M5", 6: "SERIAL-M6"})
    second = snapshot("2026-09-12T00:00:00+00:00", {2: "SERIAL-M4", 5: "SERIAL-M5", 6: "SERIAL-M6"})
    write_jsonl(positions, [first.to_dict(), second.to_dict()])
    history = tmp_path / "cells"
    base = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    records = []
    for index, soc in enumerate((70, 69, 67, 65, 65)):
        records.append({"schema_version": 1, "timestamp": base + index * 120,
            "module": 4, "module_serial": "SERIAL-M4", "soc_percent": soc,
            "current_a": -2.0, "voltages_mv": [3300 + cell for cell in range(15)],
            "temperatures_c": [20 + cell / 10 for cell in range(15)]})
    for module in (5, 6):
        records.append({"schema_version": 1, "timestamp": base, "module": module,
            "module_serial": f"SERIAL-M{module}", "soc_percent": 60 + module,
            "current_a": -1.0, "voltages_mv": [3290 + cell for cell in range(15)],
            "temperatures_c": [21 + cell / 10 for cell in range(15)]})
    write_jsonl(history / "2026-09-11.jsonl", records)
    paths = ResearchPaths(history, positions, tmp_path / "maintenance.jsonl",
                          tmp_path / "canonical", tmp_path / "daily")
    return GuardianResearchApi(paths, cursor_secret=b"test"), first, second


def get(api, suffix):
    return api.handle("GET", "/api/research/" + suffix)


def test_envelope_contract_contains_only_observed_or_derived():
    value = research_envelope(source="x", evidence_class="OBSERVED", authoritative=True,
        timestamp_from=None, timestamp_to=None, resolution="event", data={})
    assert value["research_schema_version"] == 1
    assert value["evidence_class"] in {"OBSERVED", "DERIVED"}
    assert value["quality"]["status"] == "complete"


def test_identity_is_time_valid_across_position_change(tmp_path):
    api, first, second = environment(tmp_path)
    before = api.identity.position_at("SERIAL-M4", "2026-09-11T10:00:00Z")
    after = api.identity.position_at("SERIAL-M4", "2026-09-12T10:00:00Z")
    assert (before["position_at_time"], after["position_at_time"]) == (4, 2)
    assert before["position_history_id"] == first.position_history_id
    assert after["position_history_id"] == second.position_history_id
    assert before["identity_epoch_id"] != after["identity_epoch_id"]


def test_removal_reintegration_and_unresolved_remain_explicit():
    records = [snapshot("2026-01-01T00:00:00Z", {1: "S"}),
               snapshot("2026-01-02T00:00:00Z", {}),
               snapshot("2026-01-03T00:00:00Z", {3: "S"})]
    resolver = ResearchIdentityResolver(records)
    assert resolver.position_at("S", "2026-01-02T12:00:00Z")["resolved"] is False
    assert resolver.position_at("S", "2026-01-03T12:00:00Z")["position_at_time"] == 3
    assert resolver.position_at("UNKNOWN", "2026-01-03T12:00:00Z")["position_at_time"] is None
    assert len(resolver.epochs("S")) == 3


def test_topology_and_identity_epoch_endpoints(tmp_path):
    api, first, _ = environment(tmp_path)
    topology = get(api, "topology?timestamp=2026-09-11T10:00:00Z")
    assert topology.status == 200
    assert topology.body["data"]["positions"][3]["physical_serial"] == "SERIAL-M4"
    assert topology.body["data"]["position_history_id"] == first.position_history_id
    epochs = get(api, "identity-epochs?physical_serial=SERIAL-M4")
    assert epochs.status == 200 and len(epochs.body["data"]["epochs"]) == 2


def test_identity_refreshes_after_append_without_runtime_restart(tmp_path):
    api, _, _ = environment(tmp_path)
    assert get(api, "topology?timestamp=2026-09-13T10:00:00Z").body[
        "data"]["positions"][1]["physical_serial"] == "SERIAL-M4"
    third = snapshot("2026-09-13T00:00:00Z", {3: "SERIAL-M4"})
    with api.paths.position_history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(third.to_dict()) + "\n")
    assert get(api, "topology?timestamp=2026-09-13T10:00:00Z").body[
        "data"]["positions"][2]["physical_serial"] == "SERIAL-M4"


def test_status_is_read_only_and_sources_are_explicit(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "status")
    assert response.status == 200 and response.body["read_only"] is True
    assert "guardian.cell_history" in response.body["available_sources"]
    rejected = api.handle("POST", "/api/research/status")
    assert rejected.status == 405 and rejected.headers["Allow"] == "GET"


@pytest.mark.parametrize("metric", ["soc", "module_current", "module_voltage",
                                     "module_temperature"])
def test_module_history_metrics(tmp_path, metric):
    api, _, _ = environment(tmp_path)
    response = get(api, "module-history?physical_serial=SERIAL-M4&metric=" + metric +
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert response.status == 200
    assert response.body["data"]["points"]
    assert {item["position_at_time"] for item in response.body["data"]["points"]} == {4}


def test_cell_history_and_median_deviation_definition(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "cell-history?physical_serial=SERIAL-M4&metric=cell_deviation"
        "&cell_numbers=15&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert response.status == 200
    assert response.body["data"]["points"][0]["value"] == 7
    assert response.body["data"]["points"][0]["cell_number"] == 15


def test_auto_resolution_and_range_limits(tmp_path):
    api, _, _ = environment(tmp_path)
    short = get(api, "timeseries?source=guardian.cell_history&metric=soc&physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert short.body["resolution"] == "full"
    long = get(api, "timeseries?metric=soc&physical_serial=SERIAL-M4"
        "&from=2026-08-01T00:00:00Z&to=2026-09-11T12:00:00Z")
    assert long.body["resolution"] == "display"
    too_long = get(api, "timeseries?metric=soc&physical_serial=SERIAL-M4&resolution=full"
        "&from=2026-08-01T00:00:00Z&to=2026-09-11T12:00:00Z")
    assert too_long.status == 400 and too_long.body["error"]["code"] == "range_too_large"


def test_pagination_cursor_is_query_bound_and_tamper_protected(tmp_path):
    api, _, _ = environment(tmp_path)
    query = ("timeseries?metric=soc&physical_serial=SERIAL-M4&resolution=full&max_points=2"
             "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    first = get(api, query)
    assert first.body["truncated"] is True and first.body["next_cursor"]
    second = get(api, query + "&cursor=" + first.body["next_cursor"])
    assert second.status == 200 and second.body["data"]["points"][0]["value"] == 67
    tampered = get(api, query + "&cursor=" + first.body["next_cursor"] + "x")
    assert tampered.status == 400 and tampered.body["error"]["code"] == "cursor_invalid"


def test_cursor_expires_when_source_signature_changes(tmp_path):
    api, _, _ = environment(tmp_path)
    query = ("timeseries?metric=soc&physical_serial=SERIAL-M4&resolution=full&max_points=2"
             "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    first = get(api, query)
    with (api.paths.cell_history / "2026-09-11.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    expired = get(api, query + "&cursor=" + first.body["next_cursor"])
    assert expired.status == 400 and expired.body["error"]["code"] == "cursor_invalid"


def test_coverage_complete_absent_and_unknown(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "coverage?physical_serial=SERIAL-M4&datasets=soc,policy"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    rows = {row["dataset"]: row for row in response.body["data"]["datasets"]}
    assert rows["soc"]["quality"] == "complete"
    assert rows["policy"]["quality"] == "unknown"
    absent = get(api, "coverage?physical_serial=NONE&datasets=soc"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert absent.body["data"]["datasets"][0]["quality"] == "absent"


def test_soc_crash_is_deterministic_and_requires_discharge(tmp_path):
    api, _, _ = environment(tmp_path)
    query = ("events/soc-crashes?physical_serial=SERIAL-M4"
             "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    first, second = get(api, query), get(api, query)
    events = first.body["data"]["events"]
    assert len(events) == 1 and events[0]["step_count"] == 2
    assert events[0]["soc_loss"] == 4
    assert events[0]["detector_version"] == "guardian_soc_crash_v1"
    assert events[0]["lowest_cell"] == 1 and events[0]["cell_spread_mv"] == 14
    assert events[0]["event_id"] == second.body["data"]["events"][0]["event_id"]


def test_low_voltage_returns_evidence_without_invented_alarm(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "events/low-voltage?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z&max_points=50")
    assert response.status == 200
    assert [item["evidence_kind"] for item in response.body["data"]["evidence"]] == [
        "cell_voltage", "guardian_alarm", "rs485_low_voltage"]
    assert response.body["data"]["alarm_asserted"] is None


def test_low_voltage_includes_existing_rs485_threshold_evidence(tmp_path):
    api, _, _ = environment(tmp_path)
    rs485 = tmp_path / "rs485"
    identity = {"schema_version": 1, "record_type": "frame",
        "timestamp": "2026-09-11T09:30:00+00:00", "direction": "response",
        "paired_command": 0x93, "checksum_valid": True, "frame_complete": True,
        "request_matched": True, "adr": 4,
        "info_raw": "04" + b"SERIAL-M4       ".hex(), "decoder_supported": False,
        "decoded": None}
    threshold = {"schema_version": 1, "record_type": "frame",
        "timestamp": "2026-09-11T10:00:00+00:00", "direction": "response",
        "paired_command": 0x47, "checksum_valid": True, "frame_complete": True,
        "request_matched": True, "adr": 4, "decoded": {
            "cell_low_voltage_alarm_limit_v": 3.0,
            "module_low_voltage_alarm_limit_v": 45.0}}
    write_jsonl(rs485 / "2026-09-11.jsonl", [identity, threshold])
    api = GuardianResearchApi(replace(api.paths, rs485_history=rs485), cursor_secret=b"test")
    response = get(api, "events/low-voltage?physical_serial=SERIAL-M4       "
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    evidence = response.body["data"]["evidence"][2]
    assert evidence["quality"] == "complete"
    assert evidence["records"][0]["decoded"]["cell_low_voltage_alarm_limit_v"] == 3.0


def test_evidence_package_is_reproducible_and_has_no_inference(tmp_path):
    api, _, _ = environment(tmp_path)
    crash_query = ("events/soc-crashes?physical_serial=SERIAL-M4"
                   "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    event_id = get(api, crash_query).body["data"]["events"][0]["event_id"]
    query = (f"evidence-package?event_id={event_id}&physical_serial=SERIAL-M4"
             "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    first, second = get(api, query), get(api, query)
    assert first.status == 200
    assert first.body["data"]["inferred"] is False
    assert first.body["data"]["input_fingerprint"] == second.body["data"]["input_fingerprint"]
    assert first.body["data"]["identity_topology"]["position_at_time"] == 4
    assert set(first.body["data"]["trend_windows"]) == {"PT6H", "P1D", "P7D"}
    assert "alarms" in first.body["data"] and "low_voltage" in first.body["data"]


def test_evidence_package_uses_event_and_default_window(tmp_path):
    api, _, _ = environment(tmp_path)
    crashes = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    event = crashes.body["data"]["events"][0]
    package = get(api, "evidence-package?event_id=" + event["event_id"])
    assert package.status == 200
    assert package.body["timestamp_range"]["from"] == "2026-09-10T10:02:00+00:00"
    assert package.body["timestamp_range"]["to"] == "2026-09-11T10:36:00+00:00"


def package_for_crash(api):
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    return event, get(api, "evidence-package?event_id=" + event["event_id"])


def test_soc_crash_package_v2_has_core_identity_and_provenance(tmp_path):
    api, first, _ = environment(tmp_path)
    event, response = package_for_crash(api)
    assert response.status == 200
    data = response.body["data"]
    assert response.body["semantics_version"] == "research_soc_crash_evidence_v2"
    assert data["crash_core"]["soc_delta"] == -4
    assert data["crash_core"]["detector_version"] == "guardian_soc_crash_v1"
    assert data["identity_topology"]["position_at_time"] == 4
    assert data["identity_topology"]["position_history_id"] == first.position_history_id
    assert response.body["provenance"]["event_id"] == event["event_id"]


def test_soc_crash_package_has_all_structured_windows(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = package_for_crash(api)
    assert list(response.body["data"]["comparison_windows"]) == [
        "minus_24h", "minus_6h", "minus_1h", "minus_10m",
        "immediate_pre_crash", "crash", "plus_10m", "plus_30m"]
    assert response.body["data"]["comparison_windows"]["crash"]["records_resolution"] == "full"
    assert response.body["data"]["comparison_windows"]["minus_24h"]["records"] == []


def test_soc_crash_package_preserves_all_15_cells_and_derivations(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = package_for_crash(api)
    record = response.body["data"]["cell_evidence"]["records"][0]
    assert len(record["cell_voltages_mv"]) == 15
    assert len(record["cell_temperatures_c"]) == 15
    assert record["derived"]["lowest_cell"] == 1
    assert record["derived"]["highest_cell"] == 15
    assert len(record["derived"]["cell_deviation_from_module_median_mv"]) == 15
    assert response.body["data"]["cell_evidence"]["derived_fields_evidence_class"] == "DERIVED"


def test_soc_crash_package_does_not_invent_cell_temperature(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows: row.pop("temperatures_c", None)
    write_jsonl(path, rows)
    _, response = package_for_crash(api)
    assert response.body["data"]["cell_evidence"]["records"][0]["cell_temperatures_c"] == []
    assert response.body["data"]["coverage"]["cell_temperature"]["quality"] == "absent"


def test_soc_crash_package_peer_modules_use_historical_stack(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = package_for_crash(api)
    peers = response.body["data"]["peer_evidence"]["modules"]
    assert {(row["physical_serial"], row["position_at_event"]) for row in peers} == {
        ("SERIAL-M5", 5), ("SERIAL-M6", 6)}
    assert all(row["records"] for row in peers)


def test_soc_crash_package_historical_identity_not_current_position(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = package_for_crash(api)
    assert response.body["data"]["identity_topology"]["position_at_time"] == 4
    assert api.identity.position_at("SERIAL-M4", "2026-09-12T10:00:00Z")["position_at_time"] == 2


def test_soc_crash_package_missing_context_is_unavailable_not_zero(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = package_for_crash(api)
    management = response.body["data"]["bms_management"]
    assert management["quality"] == "unavailable" and management["records"] == []
    assert response.body["data"]["coverage"]["rs485_management"]["quality"] == "unavailable"
    assert response.body["data"]["coverage"]["dcl"]["quality"] == "unavailable"
    assert response.body["data"]["optional_evidence"]["soc_recalibration"]["records"] == []
    assert response.body["data"]["alarms"]["alarms"] == []


def test_soc_crash_package_has_only_observed_and_derived_classes(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = package_for_crash(api)
    encoded = json.dumps(response.body)
    assert response.body["data"]["evidence_classes"] == ["OBSERVED", "DERIVED"]
    assert "INFERRED" not in encoded and response.body["data"]["causality_determined"] is False


def test_soc_crash_package_unknown_event_is_rejected(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "evidence-package?event_id=SCE-invalid")
    assert response.status == 400 and response.body["error"]["code"] == "invalid_argument"


def test_soc_crash_package_valid_but_unavailable_event_is_rejected(tmp_path):
    api, _, _ = environment(tmp_path)
    event_id = api._event_id("SERIAL-M4", "2026-09-11T11:00:00+00:00",
                             "2026-09-11T11:01:00+00:00")
    response = get(api, "evidence-package?event_id=" + event_id)
    assert response.status == 404 and response.body["error"]["code"] == "coverage_absent"


def test_soc_crash_package_supports_explicit_iso_windows(tmp_path):
    api, _, _ = environment(tmp_path)
    event, _ = package_for_crash(api)
    response = get(api, f"evidence-package?event_id={event['event_id']}&before=PT10M&after=PT10M")
    assert response.status == 200
    assert response.body["timestamp_range"]["from"] == "2026-09-11T09:52:00+00:00"
    assert response.body["timestamp_range"]["to"] == "2026-09-11T10:16:00+00:00"


def test_soc_crash_package_reports_partial_cell_evidence_when_bounded(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    original = api.series.evidence_by_serial
    def bounded(*args, **kwargs):
        result = original(*args, **kwargs); result["truncated"] = True; return result
    monkeypatch.setattr(api.series, "evidence_by_serial", bounded)
    _, response = package_for_crash(api)
    assert response.status == 200
    assert response.body["data"]["cell_evidence"]["source_sample_count"] >= 1
    assert response.body["data"]["coverage"]["cell_evidence"]["quality"] == "partial"


def test_soc_crash_package_is_utc_and_fingerprint_deterministic(tmp_path):
    api, _, _ = environment(tmp_path)
    event, first = package_for_crash(api)
    second = get(api, "evidence-package?event_id=" + event["event_id"])
    assert first.body["data"]["input_fingerprint"] == second.body["data"]["input_fingerprint"]
    assert first.body["data"]["event"]["start"].endswith("+00:00")


def test_rs485_management_evidence_uses_observed_serial_mapping(tmp_path):
    api, _, _ = environment(tmp_path)
    serial = "M4SERIAL12345678"
    rs485 = tmp_path / "rs485"
    records = [{"record_type": "frame", "timestamp": "2026-09-11T09:59:00+00:00",
        "direction": "response", "paired_command": 0x93, "checksum_valid": True,
        "frame_complete": True, "request_matched": True, "adr": 4,
        "info_raw": "04" + serial.encode().hex(), "decoder_supported": False,
        "decoded": None},
        {"record_type": "frame", "timestamp": "2026-09-11T10:00:00+00:00",
        "direction": "response", "paired_command": 0x92, "checksum_valid": True,
        "frame_complete": True, "request_matched": True, "adr": 4,
        "decoded": {"charge_current_limit_a": 10, "discharge_current_limit_a": -25,
            "charge_voltage_limit_v": 53.25, "discharge_voltage_limit_v": 45,
            "charge_enable": True, "discharge_enable": True}}]
    write_jsonl(rs485 / "2026-09-11.jsonl", records)
    api = GuardianResearchApi(replace(api.paths, rs485_history=rs485), cursor_secret=b"test")
    result = api._rs485_management(serial, "2026-09-11T09:00:00+00:00",
                                   "2026-09-11T11:00:00+00:00", float("inf"))
    assert result["records"][0]["charge_current_limit_a"] == 10
    assert result["records"][0]["discharge_enable"] is True
    assert result["evidence_class"] == "OBSERVED"


def test_soc_crash_package_remains_bounded_for_large_24h_input(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-10.jsonl"
    start = datetime(2026, 9, 10, 10, 2, tzinfo=timezone.utc).timestamp()
    rows = [{"schema_version": 1, "timestamp": start + index * 30, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": 70, "current_a": -1,
        "voltages_mv": [3300 + cell for cell in range(15)]}
        for index in range(2880)]
    write_jsonl(path, rows)
    _, response = package_for_crash(api)
    assert response.status == 200
    evidence = response.body["data"]["cell_evidence"]
    assert evidence["source_sample_count"] > 600
    assert len(evidence["records"]) == 600 and evidence["truncated"] is True
    assert len(json.dumps(response.body).encode()) < 2 * 1024 * 1024


def test_evidence_package_event_id_survives_api_restart(tmp_path):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    restarted = GuardianResearchApi(api.paths, cursor_secret=b"new-process")
    package = get(restarted, "evidence-package?event_id=" + event["event_id"])
    assert package.status == 200
    assert package.body["data"]["event"]["event_id"] == event["event_id"]


def test_reads_leave_authoritative_files_byte_identical(tmp_path):
    api, _, _ = environment(tmp_path)
    paths = [api.paths.cell_history / "2026-09-11.jsonl", api.paths.position_history]
    before = [path.read_bytes() for path in paths]
    get(api, "topology?timestamp=2026-09-11T10:00:00Z")
    get(api, "timeseries?metric=soc&physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    package_for_crash(api)
    assert [path.read_bytes() for path in paths] == before


def test_query_audit_has_no_payload_but_has_counts(tmp_path):
    api, _, _ = environment(tmp_path)
    get(api, "timeseries?metric=soc&physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    audit = api.gate.audit[-1]
    assert audit["request_id"] and audit["duration_seconds"] >= 0
    assert audit["records"] == 5 and audit["bytes"] > 0
    assert "data" not in audit


def test_source_paths_cannot_be_supplied(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "timeseries?source=/etc/passwd&metric=soc&physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert response.status == 400


def test_hycube_and_policy_use_existing_readers(tmp_path):
    api, _, _ = environment(tmp_path)
    hycube, projection, policy = tmp_path / "hycube", tmp_path / "projection", tmp_path / "policy"
    hycube.mkdir(); policy.mkdir()
    record = {"schema_version": 1, "record_type": "hycube_system_observation",
        "received_at": "2026-09-11T10:00:00+00:00", "BatteryCapacity": 77,
        "Date2": None, "device_timestamp": None, "timezone_semantics": "unavailable",
        "parse_quality": "complete", "payload_sha256": "abc",
        "configured_interval_seconds": 5, "actual_interval_seconds": 5,
        "actual_interval_quality": "observed"}
    write_jsonl(hycube / "2026-09-11.jsonl", [record])
    policy_record = policy_observation(
        b'{"normalMode":82,"bufferMode":3,"emergency":10,"batProtection":5}',
        datetime(2026, 9, 11, 9, tzinfo=timezone.utc).timestamp())
    from hycube_evidence import HycubePolicyHistory
    HycubePolicyHistory(policy).append(policy_record)
    api = GuardianResearchApi(replace(api.paths, hycube_history=hycube,
        hycube_projection=projection, hycube_policy=policy), cursor_secret=b"test")
    capacity = get(api, "timeseries?source=guardian.hycube&metric=battery_capacity"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert capacity.status == 200 and capacity.body["data"]["points"][0]["value"] == 77
    boundaries = get(api, "timeseries?source=guardian.hycube&metric=policy"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert boundaries.status == 200 and boundaries.body["data"]["segments"]


def test_display_source_uses_existing_reader_with_raw_fallback(tmp_path):
    api, _, _ = environment(tmp_path)
    display = tmp_path / "display"; display.mkdir()
    api = GuardianResearchApi(replace(api.paths, display_history=display), cursor_secret=b"test")
    response = get(api, "timeseries?source=guardian.display_history&metric=soc"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert response.status == 200 and response.body["data"]["points"]
    assert response.body["evidence_class"] == "DERIVED"


def test_direct_serial_evidence_does_not_invent_historical_position(tmp_path):
    api, _, _ = environment(tmp_path)
    record = {"schema_version": 1,
        "timestamp": datetime(2026, 9, 11, 11, tzinfo=timezone.utc).timestamp(),
        "module": 1, "module_serial": "UNRESOLVED", "soc_percent": 50,
        "current_a": 0, "voltages_mv": [3300] * 15, "temperatures_c": [20] * 15}
    with (api.paths.cell_history / "2026-09-11.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    response = get(api, "timeseries?metric=soc&physical_serial=UNRESOLVED"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    point = response.body["data"]["points"][0]
    assert point["identity_resolved"] is False
    assert point["position_at_time"] is None


def test_query_gate_busy_is_controlled(tmp_path):
    api, _, _ = environment(tmp_path)
    assert api.gate.all.acquire(blocking=False)
    assert api.gate.all.acquire(blocking=False)
    try:
        response = get(api, "status")
        assert response.status == 429 and response.body["error"]["code"] == "busy"
    finally:
        api.gate.all.release(); api.gate.all.release()


@pytest.mark.parametrize("serial,cell", [("SERIAL-M4", 15), ("SERIAL-M5", 8),
                                          ("SERIAL-M6", 5)])
def test_historical_module_cell_comparison_capability(tmp_path, serial, cell):
    api, _, _ = environment(tmp_path)
    for metric in ("soc", "module_current", "module_voltage", "cell_spread"):
        response = get(api, f"module-history?physical_serial={serial}&metric={metric}"
            "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
        assert response.status == 200 and response.body["data"]["points"]
    for metric in ("cell_voltage", "cell_temperature", "cell_deviation"):
        response = get(api, f"cell-history?physical_serial={serial}&metric={metric}"
            f"&cell_numbers={cell}&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
        assert response.status == 200 and response.body["data"]["points"]


@pytest.mark.parametrize("hours", [1, 6, 24, 168])
def test_m4_acceptance_windows_are_bounded_and_queryable(tmp_path, hours):
    api, _, _ = environment(tmp_path)
    start = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
    left = (start - timedelta(hours=hours / 2)).isoformat()
    right = (start + timedelta(hours=hours / 2)).isoformat()
    response = get(api, "cell-history?physical_serial=SERIAL-M4&metric=cell_voltage"
        f"&cell_numbers=15&from={quote(left)}&to={quote(right)}&resolution=auto")
    assert response.status == 200


def test_config_handler_recognizes_research_route():
    from config_ui import Handler
    handler = object.__new__(Handler)
    handler.path = "/ingress/session/api/research/status?x=1"
    assert handler._is_research_api() is True


def test_public_observability_is_compact_by_default(monkeypatch):
    import config_ui
    monkeypatch.delenv("GUARDIAN_TECHNICAL_DEBUG", raising=False)
    startup = config_ui.public_display_projection_startup_status()
    assert set(startup) == {"startup_status", "started_at", "last_startup_error"}
    timing = config_ui._public_collector_timing({"poll_target_s": 10,
        "cell_analysis_profiling": {"modules": [{"secret": "detail"}]},
        "last_completed_cycle": {"cycle_id": 2, "mqtt_subtiming": {
            "connected_after": True, "mqtt_publish_count": 4,
            "mqtt_json_thread_cpu_seconds": 3, "groups": {"modules": {}}}}})
    assert "cell_analysis_profiling" not in timing
    assert "groups" not in timing["last_completed_cycle"]["mqtt_subtiming"]


def test_debug_mode_retains_detailed_observability(monkeypatch):
    import config_ui
    monkeypatch.setenv("GUARDIAN_TECHNICAL_DEBUG", "true")
    detailed = {"cell_analysis_profiling": {"modules": [1]}}
    assert config_ui._public_collector_timing(detailed) is detailed
    assert "startup_stage" in config_ui.public_display_projection_startup_status()


def test_config_handler_dispatches_research_get_and_rejects_post(tmp_path, monkeypatch):
    import config_ui
    api, _, _ = environment(tmp_path)
    monkeypatch.setattr(config_ui, "_RESEARCH_API", api)
    handler = object.__new__(config_ui.Handler)
    handler.path = "/api/hassio_ingress/token/api/research/status"
    handler._ingress_allowed = lambda: True
    captured = []
    handler._send = lambda status, body, *args, **kwargs: captured.append((status, body, kwargs))
    handler.do_GET()
    assert captured[-1][0] == 200 and captured[-1][1]["read_only"] is True
    handler.do_POST()
    assert captured[-1][0] == 405
