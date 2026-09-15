import json
import io
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest

from position_history import PositionSnapshot
from research_api import (GuardianResearchApi, PACKAGE_PROFILE_STAGES, ResearchPaths,
                          research_envelope)
from research_identity import ResearchIdentityResolver
from hycube_evidence import policy_observation
from history_block_index import build_index
import research_timeseries
from version import (DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION,
                     RESEARCH_SEMANTICS_VERSION, SOURCE_COMMIT)


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


def test_acceptance_build_identity_is_explicit_and_non_secret():
    assert GUARDIAN_VERSION == "0.8.1"
    assert DIAGNOSTIC_ENGINE_VERSION == "0.4.12"
    assert SOURCE_COMMIT == "43c04ab0b67fec4bcf2e4bcdb34b31767a90b620"
    assert RESEARCH_SEMANTICS_VERSION == "research_soc_crash_evidence_v2"


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


def test_soc_crash_event_is_identical_to_legacy_authoritative_scan(tmp_path):
    api, _, _ = environment(tmp_path)
    start, end = "2026-09-11T09:00:00+00:00", "2026-09-11T12:00:00+00:00"
    optimized = api._soc_crashes({"physical_serial": "SERIAL-M4",
                                  "from": start, "to": end}, float("inf"))
    original = api.series.soc_current_by_serial
    def legacy(serials, timestamp_from, timestamp_to, deadline=None, profile=None):
        normalized_start, normalized_end = api.series.normalize_range(
            timestamp_from, timestamp_to)
        lower = datetime.fromisoformat(normalized_start).timestamp()
        upper = datetime.fromisoformat(normalized_end).timestamp()
        wanted = set(serials); result = {serial: [] for serial in wanted}
        for path in api.series._paths(normalized_start, normalized_end):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line); epoch = float(record["timestamp"])
                if not lower <= epoch <= upper:
                    continue
                timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                observed = record.get("module_serial")
                if observed is None:
                    observed = api.identity.serial_at(int(record.get("module", 0)), timestamp)[
                        "physical_serial"]
                if observed not in wanted:
                    continue
                identity = api.identity.position_at(observed, timestamp)
                voltages = record.get("voltages_mv") or []
                result[observed].append({"timestamp": timestamp,
                    "soc": float(record["soc_percent"]),
                    "current": float(record["current_a"]),
                    "identity_epoch_id": identity.get("identity_epoch_id"),
                    "position_at_time": identity.get("position_at_time"),
                    "lowest_cell": voltages.index(min(voltages)) + 1 if voltages else None,
                    "cell_spread_mv": max(voltages) - min(voltages) if voltages else None})
        return result
    api.series.soc_current_by_serial = legacy
    try:
        legacy_result = api._soc_crashes({"physical_serial": "SERIAL-M4",
                                          "from": start, "to": end}, float("inf"))
    finally:
        api.series.soc_current_by_serial = original
    assert optimized == legacy_result


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


def test_returned_event_with_submicrosecond_source_timestamps_resolves_to_package(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    base = datetime(2026, 9, 11, 15, tzinfo=timezone.utc).timestamp()
    # Binary float epochs can round outward when converted to the microsecond ISO
    # timestamps embedded in an event id.  Both event edge records must remain
    # available to the bounded reconstruction query.
    rows = [
        {"schema_version": 1, "timestamp": base + 0.1234567, "module": 4,
         "module_serial": "SERIAL-M4", "soc_percent": 70, "current_a": -1,
         "voltages_mv": [3300] * 15},
        {"schema_version": 1, "timestamp": base + 60.7654323, "module": 4,
         "module_serial": "SERIAL-M4", "soc_percent": 68, "current_a": -1,
         "voltages_mv": [3300] * 15},
    ]
    write_jsonl(path, rows)
    source = path.read_bytes()
    found = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T15:00:00Z&to=2026-09-11T15:02:00Z")
    event = found.body["data"]["events"][0]

    package = get(api, f"evidence-package?event_id={event['event_id']}"
                       "&before=P1D&after=PT30M")

    assert package.status == 200
    assert package.body["data"]["event"]["event_id"] == event["event_id"]
    assert package.body["data"]["event"] == event
    assert package.body["data"]["identity_topology"]["position_at_time"] == 4
    assert path.read_bytes() == source


def test_evidence_package_event_lookup_is_microsecond_bounded(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    calls = []
    original = api._soc_crashes

    def tracked(values, deadline):
        calls.append(dict(values))
        return original(values, deadline)

    monkeypatch.setattr(api, "_soc_crashes", tracked)
    response = get(api, "evidence-package?event_id=" + event["event_id"])

    assert response.status == 200
    assert calls == [{"physical_serial": "SERIAL-M4",
        "from": "2026-09-11T10:01:59.999999+00:00",
        "to": "2026-09-11T10:06:00.000001+00:00"}]


def package_profile(caplog):
    return json.loads(next(item.message.removeprefix("RESEARCH_PACKAGE_PROFILE ")
        for item in caplog.records
        if item.message.startswith("RESEARCH_PACKAGE_PROFILE ")))


def test_evidence_package_profiling_is_opt_in_response_neutral_and_read_only(
        tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    suffix = "evidence-package?event_id=" + event["event_id"]
    source = (api.paths.cell_history / "2026-09-11.jsonl").read_bytes()
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        normal = get(api, suffix)
        disabled = get(api, suffix + "&profile=false")
    assert "RESEARCH_PACKAGE_PROFILE" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        profiled = get(api, suffix + "&profile=true")
    assert normal.status == disabled.status == profiled.status == 200
    assert normal.body["data"] == disabled.body["data"] == profiled.body["data"]
    assert (api.paths.cell_history / "2026-09-11.jsonl").read_bytes() == source
    profile = package_profile(caplog)
    assert profile["status"] == "ok"
    assert set(profile["stages"]) == set(PACKAGE_PROFILE_STAGES)
    assert profile["stages"]["module_soc"]["calls"] == 4
    assert profile["stages"]["module_soc"]["files_opened"] >= 1
    assert profile["stages"]["module_soc"]["bytes_read"] > 0
    assert profile["stages"]["main_multi_metric_read"]["samples_returned"] > 0
    assert profile["coverage_status"]["module_soc"] in {"complete", "partial"}
    assert profile["stages"]["soc_recalibration"]["status"] == "unavailable"


def test_evidence_package_profile_adds_no_history_scans(tmp_path, monkeypatch, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    suffix = "evidence-package?event_id=" + event["event_id"]
    counts = {"query": 0, "evidence": 0}
    query, evidence = api.series.query, api.series.evidence_by_serial
    def tracked_query(*args, **kwargs):
        counts["query"] += 1
        return query(*args, **kwargs)
    def tracked_evidence(*args, **kwargs):
        counts["evidence"] += 1
        return evidence(*args, **kwargs)
    monkeypatch.setattr(api.series, "query", tracked_query)
    monkeypatch.setattr(api.series, "evidence_by_serial", tracked_evidence)
    get(api, suffix)
    normal = dict(counts)
    counts.update(query=0, evidence=0)
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        get(api, suffix + "&profile=true")
    assert counts == normal == {"query": 1, "evidence": 1}


def test_evidence_package_timeout_logs_complete_redacted_profile(tmp_path, monkeypatch, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    def timeout_on_main_scan(*args, **kwargs):
        raise research_timeseries.ResearchQueryError(
            "timeout", "research query timed out", 503)
    monkeypatch.setattr(api.series, "evidence_by_serial", timeout_on_main_scan)
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        response = get(api, "evidence-package?event_id=" + event["event_id"] + "&profile=true")
    assert response.status == 503 and response.body["error"]["code"] == "timeout"
    profile = package_profile(caplog)
    assert profile["status"] == "timeout"
    assert profile["stages"]["main_multi_metric_read"]["status"] == "timeout"
    assert profile["stages"]["module_soc"]["status"] == "not_run"
    assert profile["stages"]["module_current"]["status"] == "not_run"
    assert profile["stages"]["module_voltage"]["status"] == "not_run"
    encoded = json.dumps(profile)
    assert event["event_id"] not in encoded
    assert "SERIAL-M4" not in encoded
    assert "soc_percent" not in encoded
    assert "token" not in encoded.lower() and "secret" not in encoded.lower()


def test_package_single_scan_projections_match_existing_query_contracts(tmp_path):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    start, end = "2026-09-10T10:02:00+00:00", "2026-09-11T10:36:00+00:00"
    metrics = ("soc", "module_current", "module_voltage", "cell_voltage",
               "cell_temperature")
    expected = {metric: api.series.query(metric=metric, physical_serial="SERIAL-M4",
        timestamp_from=start, timestamp_to=end, resolution="auto", max_points=800)
        for metric in metrics}
    expected_low_voltage = api.series.query(metric="cell_voltage",
        physical_serial="SERIAL-M4", timestamp_from=start, timestamp_to=end,
        resolution="auto", max_points=200)

    package = get(api, "evidence-package?event_id=" + event["event_id"])

    assert package.status == 200
    for metric in metrics:
        actual = dict(package.body["data"]["timeseries"][metric])
        assert actual.pop("evidence_class") in {"OBSERVED", "DERIVED"}
        assert actual == expected[metric]
    assert package.body["data"]["low_voltage"]["points"] == expected_low_voltage["points"]
    assert package.body["data"]["low_voltage"]["coverage"] == expected_low_voltage["coverage"]
    assert package.body["data"]["evidence_classes"] == ["OBSERVED", "DERIVED"]
    assert package.body["data"]["inferred"] is False


def test_package_uses_indexed_chunks_across_two_utc_days_without_source_changes(
        tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    first_path = api.paths.cell_history / "2026-09-11.jsonl"
    second_path = api.paths.cell_history / "2026-09-12.jsonl"
    first_epoch = datetime(2026, 9, 11, 23, 59, tzinfo=timezone.utc).timestamp()
    before = {"schema_version": 1, "timestamp": first_epoch, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": 70, "current_a": -1,
        "voltages_mv": [3300] * 15, "temperatures_c": [20] * 15}
    after = {**before, "timestamp": first_epoch + 60, "module": 2, "soc_percent": 68}
    write_jsonl(first_path, [before])
    write_jsonl(second_path, [after])
    for path in (first_path, second_path):
        build_index(path, timestamp_field="timestamp", iso_timestamp=False,
                    block_records=1)
    source = [path.read_bytes() for path in (first_path, second_path)]
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T23:58:00Z&to=2026-09-12T00:01:00Z").body["data"]["events"][0]
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        package = get(api, "evidence-package?event_id=" + event["event_id"] + "&profile=true")

    assert package.status == 200
    assert package.body["data"]["event"]["event_id"] == event["event_id"]
    assert package.body["data"]["identity_topology"]["position_at_time"] == 4
    profile = package_profile(caplog)
    assert profile["counts"]["cell_history_scans"] == 2
    assert profile["stages"]["main_multi_metric_read"]["files_opened"] == 2
    assert profile["stages"]["main_multi_metric_read"]["read_mode"] == "indexed_chunk"
    for stage in ("module_soc", "module_current", "module_voltage",
                  "cell_voltages", "temperature_channels"):
        assert profile["stages"][stage]["read_mode"] == "indexed_chunk"
    assert [path.read_bytes() for path in (first_path, second_path)] == source


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


def test_soc_crash_single_serial_realistic_24h_stays_within_deadline(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    start = datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp()
    rows = []
    for minute in range(24 * 60):
        for module in range(1, 7):
            rows.append({"schema_version": 1, "timestamp": start + minute * 60,
                "module": module, "module_serial": f"SERIAL-M{module}",
                "soc_percent": 70 - (2 if module == 4 and minute == 721 else 0),
                "current_a": -1, "voltages_mv": [3300 + cell for cell in range(15)]})
    write_jsonl(path, rows)
    started = time.monotonic()
    response = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T00:00:00Z&to=2026-09-11T23:59:59Z")
    elapsed = time.monotonic() - started
    assert response.status == 200
    assert response.body["data"]["events"][0]["soc_loss"] == 2
    assert elapsed < 5


def test_soc_crash_scan_uses_indexed_time_range_and_rejects_foreign_serial_early(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    start = datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp()
    rows = []
    for hour in range(24):
        for module in range(1, 7):
            rows.append({"schema_version": 1, "timestamp": start + hour * 3600,
                "module": module, "module_serial": f"SERIAL-M{module}",
                "soc_percent": 70, "current_a": -1, "voltages_mv": [3300] * 15})
    write_jsonl(path, rows)
    build_index(path, timestamp_field="timestamp", iso_timestamp=False,
                block_records=6)
    original = research_timeseries.json.loads
    decoded_records = []
    def tracked(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if isinstance(result, dict) and "module_serial" in result:
            decoded_records.append(result)
        return result
    monkeypatch.setattr(research_timeseries.json, "loads", tracked)
    response = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T10:00:00Z&to=2026-09-11T11:00:00Z")
    assert response.status == 200
    assert decoded_records
    assert {item["module_serial"] for item in decoded_records} == {"SERIAL-M4"}
    assert all(start + 10 * 3600 <= item["timestamp"] <= start + 11 * 3600
               for item in decoded_records)


def test_soc_crash_timeout_remains_fail_closed(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    clock = iter((0, 0, 20, 20, 20, 20))
    monkeypatch.setattr(research_timeseries.time, "monotonic", lambda: next(clock, 20))
    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        api.series.soc_current_by_serial(["SERIAL-M4"],
            "2026-09-11T09:00:00Z", "2026-09-11T12:00:00Z", deadline=10)
    assert error.value.code == "timeout" and error.value.status == 503


def test_soc_crash_scan_preserves_inclusive_utc_epoch_boundaries(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    start = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    rows = [{"schema_version": 1, "timestamp": epoch, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": soc, "current_a": -1,
        "voltages_mv": [3300] * 15}
        for epoch, soc in ((start - 1, 90), (start, 70), (start + 60, 68),
                           (start + 120, 66), (start + 121, 40))]
    write_jsonl(path, rows)
    result = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T10:00:00Z&to=2026-09-11T10:02:00Z")
    event = result.body["data"]["events"][0]
    assert event["start"] == "2026-09-11T10:00:00+00:00"
    assert event["end"] == "2026-09-11T10:02:00+00:00"
    assert event["soc_before"] == 70 and event["soc_after"] == 66


def test_soc_crash_profiling_is_opt_in_and_response_neutral(tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    suffix = ("events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        normal = get(api, suffix)
        explicitly_disabled = get(api, suffix + "&profile=false")
    assert explicitly_disabled.body == normal.body
    assert "RESEARCH_PROFILE" not in caplog.text
    caplog.clear()
    source = (api.paths.cell_history / "2026-09-11.jsonl").read_bytes()
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        profiled = get(api, suffix + "&profile=true")
    assert profiled.body == normal.body
    assert (api.paths.cell_history / "2026-09-11.jsonl").read_bytes() == source
    record = json.loads(next(item.message.removeprefix("RESEARCH_PROFILE ")
        for item in caplog.records if item.message.startswith("RESEARCH_PROFILE ")))
    assert record["status"] == "ok"
    assert set(record["stages_seconds"]) == {
        "request_range_validation", "identity_epoch_preparation", "file_discovery",
        "block_index_discovery", "indexed_range_selection", "jsonl_scan",
        "range_seek", "range_position_check", "raw_line_read", "raw_chunk_read",
        "range_tail_read", "serial_prefilter",
        "serial_token_decode", "full_json_decode", "timestamp_parse_range_check",
        "timestamp_format", "identity_assignment", "soc_current_extract",
        "cell_context_extract", "deadline_check",
        "candidate_detection", "grouping", "historical_position_resolution"}
    assert record["counts"]["raw_records_inspected"] == 7
    assert record["counts"]["records_skipped_serial_prefilter"] == 2
    assert record["counts"]["relevant_soc_current_samples"] == 5
    assert record["counts"]["raw_bytes_read"] == len(source)
    assert record["counts"]["average_raw_line_bytes"] > 0
    assert record["counts"]["maximum_raw_line_bytes"] > 0
    assert record["counts"]["raw_chunk_reads"] >= 1
    assert record["files"][0]["file"] == "2026-09-11.jsonl"
    assert record["files"][0]["size_bytes"] == len(source)
    assert record["files"][0]["raw_bytes_read"] == len(source)
    assert record["files"][0]["selected_progress_percent"] == 100
    assert "SERIAL-M4" not in caplog.text and "soc_percent" not in caplog.text


def test_soc_crash_timeout_still_logs_bounded_profile(tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    values = {"physical_serial": "SERIAL-M4", "profile": "true",
        "from": "2026-09-11T09:00:00Z", "to": "2026-09-11T12:00:00Z"}
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        with pytest.raises(research_timeseries.ResearchQueryError) as error:
            api._soc_crashes(values, deadline=-1)
    assert error.value.code == "timeout"
    record = json.loads(next(item.message.removeprefix("RESEARCH_PROFILE ")
        for item in caplog.records if item.message.startswith("RESEARCH_PROFILE ")))
    assert record["status"] == "timeout"
    assert record["total_elapsed_seconds"] >= 0
    assert record["counts"]["raw_records_inspected"] == 0
    assert record["counts"]["raw_chunk_reads"] == 1
    assert record["counts"]["raw_bytes_read"] > 0
    assert record["files"][0]["records_inspected"] == 0
    assert record["stages_seconds"]["raw_line_read"] >= 0
    assert record["stages_seconds"]["raw_chunk_read"] >= 0
    assert record["stages_seconds"]["deadline_check"] >= 0
    assert "SERIAL-M4" not in caplog.text and "soc_percent" not in caplog.text


def test_soc_crash_profile_rejects_ambiguous_activation(tmp_path):
    api, _, _ = environment(tmp_path)
    response = get(api, "events/soc-crashes?physical_serial=SERIAL-M4&profile=yes"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert response.status == 400


def test_soc_crash_profile_counts_large_lines_and_multiple_index_ranges(tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    base = datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp()
    rows = []
    for hour in (10, 10, 20, 20, 10, 10):
        rows.append({"schema_version": 1, "timestamp": base + hour * 3600,
            "module": 4, "module_serial": "SERIAL-M4", "soc_percent": 70,
            "current_a": -1, "voltages_mv": [3300] * 15, "padding": "x" * 10_000})
    write_jsonl(path, rows)
    build_index(path, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        response = get(api, "events/soc-crashes?physical_serial=SERIAL-M4&profile=true"
            "&from=2026-09-11T09:00:00Z&to=2026-09-11T11:00:00Z")
    assert response.status == 200
    record = json.loads(next(item.message.removeprefix("RESEARCH_PROFILE ")
        for item in caplog.records if item.message.startswith("RESEARCH_PROFILE ")))
    file_profile = record["files"][0]
    assert file_profile["index_valid"] is True
    assert file_profile["range_count"] == 2
    assert file_profile["records_inspected"] == 4
    assert file_profile["maximum_raw_line_bytes"] > 10_000
    assert file_profile["raw_bytes_read"] == record["counts"]["raw_bytes_read"]
    assert record["counts"]["raw_chunk_reads"] == 2


def _legacy_range_lines(blob, ranges):
    handle = io.BytesIO(blob); result = []
    for start, end in ranges:
        handle.seek(start)
        while handle.tell() < end:
            result.append(handle.readline())
    return result


@pytest.mark.parametrize("blob,ranges", [
    (b'{"a":1}\n{"b":2}\n', ((0, 8), (8, 16))),
    (b'{"a":1}\n{"b":2}', ((0, 15),)),
    ('{"text":"Gr\u00fc\u00dfe"}\n{"broken":'.encode(), ((0, 28),)),
    (b'first-line\nsecond-line\n', ((2, 15),)),
])
def test_chunk_reader_is_byte_identical_to_legacy_readline(blob, ranges):
    expected = _legacy_range_lines(blob, ranges)
    actual = []
    handle = io.BytesIO(blob)
    for start, end in ranges:
        actual.extend(research_timeseries.iter_binary_range_lines(
            handle, start, end, chunk_size=3))
    assert actual == expected


def test_chunk_reader_reassembles_record_spanning_many_chunks_and_large_line():
    blob = (b'{"padding":"' + b'x' * 100_000 + b'"}\n'
            b'{"tail":true}')
    expected = _legacy_range_lines(blob, ((0, len(blob)),))
    actual = list(research_timeseries.iter_binary_range_lines(
        io.BytesIO(blob), 0, len(blob), chunk_size=257))
    assert actual == expected
    assert len(actual) == 2 and len(actual[0]) > 100_000


def test_soc_crash_chunk_reader_preserves_two_day_utc_boundary_and_bad_line(tmp_path):
    api, _, _ = environment(tmp_path)
    first_epoch = datetime(2026, 9, 11, 23, 59, tzinfo=timezone.utc).timestamp()
    before = {"schema_version": 1, "timestamp": first_epoch, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": 70, "current_a": -1,
        "voltages_mv": [3300] * 15}
    after = {**before, "timestamp": first_epoch + 60, "soc_percent": 68}
    late = {**after, "timestamp": first_epoch + 120, "soc_percent": 20}
    write_jsonl(api.paths.cell_history / "2026-09-11.jsonl", [before])
    second = api.paths.cell_history / "2026-09-12.jsonl"
    second.write_bytes((json.dumps(after) + "\n{broken json\n" + json.dumps(late) + "\n").encode())
    source = [path.read_bytes() for path in (
        api.paths.cell_history / "2026-09-11.jsonl", second)]
    response = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T23:59:00Z&to=2026-09-12T00:00:00Z")
    event = response.body["data"]["events"][0]
    assert event["start"] == "2026-09-11T23:59:00+00:00"
    assert event["end"] == "2026-09-12T00:00:00+00:00"
    assert event["soc_loss"] == 2
    assert [path.read_bytes() for path in (
        api.paths.cell_history / "2026-09-11.jsonl", second)] == source


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
