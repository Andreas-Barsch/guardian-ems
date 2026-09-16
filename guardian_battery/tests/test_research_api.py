import json
import io
import logging
import time
import tracemalloc
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest

from position_history import PositionHistoryLog, PositionSnapshot
from maintenance import new_maintenance_event
from research_api import (CORE_EVIDENCE_VERSION, CORE_PROFILE_STAGES,
                          EVIDENCE_PACKAGE_TIMEOUT_SECONDS, GuardianResearchApi,
                          PACKAGE_PROFILE_STAGES, QUERY_TIMEOUT_SECONDS,
                          RAW_EVIDENCE_MAX_PAGE_RECORDS,
                          RAW_EVIDENCE_MAX_SCAN_RECORDS,
                          RAW_EVIDENCE_MAX_WINDOW_SECONDS,
                          QueryGate, READER_ACCOUNTED_TIMINGS, ResearchPaths,
                          research_envelope)
from research_identity import ResearchIdentityResolver
from hycube_evidence import policy_observation
from history_block_index import build_index, index_path
from rs485_history_index import rebuild as rebuild_rs485_index
from timeline_index import rebuild as rebuild_timeline_index
import research_timeseries
import research_api
import research_identity
from version import (DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION,
                     RESEARCH_SEMANTICS_VERSION, SOURCE_COMMIT,
                     UNAVAILABLE_SOURCE_COMMIT, load_source_commit,
                     require_source_commit)


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


@pytest.mark.parametrize("endpoint,seconds", [
    ("timeseries", 10), ("events/soc-crashes", 10), ("evidence-core", 10),
    ("evidence-package", 15)])
def test_query_gate_uses_fixed_endpoint_specific_absolute_deadline(
        monkeypatch, endpoint, seconds):
    clock = iter((100.0, 100.25, 100.5))
    monkeypatch.setattr("research_api.time.monotonic", lambda: next(clock))
    captured = []
    result = QueryGate().run(endpoint, False,
        lambda deadline: captured.append(deadline) or {"data": {}})
    assert result == {"data": {}}
    assert captured == [100.0 + seconds]
    assert (QUERY_TIMEOUT_SECONDS, EVIDENCE_PACKAGE_TIMEOUT_SECONDS) == (10, 15)


@pytest.mark.parametrize("endpoint,elapsed", [
    ("timeseries", 10.001), ("evidence-core", 10.001),
    ("evidence-package", 15.001)])
def test_query_gate_endpoint_deadlines_remain_hard_and_fail_closed(
        monkeypatch, endpoint, elapsed):
    clock = iter((100.0, 100.0 + elapsed, 100.0 + elapsed))
    monkeypatch.setattr("research_api.time.monotonic", lambda: next(clock))
    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        QueryGate().run(endpoint, False, lambda deadline: {"data": {}})
    assert error.value.code == "timeout" and error.value.status == 503


def test_package_stage_does_not_call_callback_when_deadline_already_expired(
        monkeypatch):
    monkeypatch.setattr("research_api.time.monotonic", lambda: 100.0)
    profile = GuardianResearchApi._package_profile()
    called = []

    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        GuardianResearchApi._run_package_stage(
            profile, "event_id_decode_checksum", 100.0,
            lambda: called.append(True))

    assert error.value.code == "timeout"
    assert called == []
    stage = profile["stages"]["event_id_decode_checksum"]
    assert stage["calls"] == 0
    assert stage["status"] == "timeout"
    assert stage["elapsed_seconds"] == 0
    assert stage["deadline_remaining_seconds_at_entry"] == 0
    assert stage["deadline_remaining_seconds_at_exit"] == 0


def test_package_stage_deadline_between_stages_prevents_blocking_successor(
        monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("research_api.time.monotonic", lambda: clock[0])
    profile = GuardianResearchApi._package_profile()
    called = []

    def consume_budget():
        called.append("first")
        clock[0] = 114.999
        return "complete"

    assert GuardianResearchApi._run_package_stage(
        profile, "event_id_decode_checksum", 115.0, consume_budget) == "complete"
    clock[0] = 115.0
    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        GuardianResearchApi._run_package_stage(
            profile, "low_voltage", 115.0,
            lambda: called.append("ten-second-block"))

    assert error.value.code == "timeout"
    assert called == ["first"]
    assert profile["stages"]["event_id_decode_checksum"]["status"] == "complete"
    assert profile["stages"]["low_voltage"]["status"] == "timeout"
    assert profile["stages"]["config_context"]["status"] == "not_run"


def test_package_stage_callback_timeout_propagates_without_following_stage(
        monkeypatch):
    monkeypatch.setattr("research_api.time.monotonic", lambda: 100.0)
    profile = GuardianResearchApi._package_profile()
    called = []

    def timeout():
        called.append("first")
        raise research_timeseries.ResearchQueryError(
            "timeout", "research query timed out", 503)

    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        GuardianResearchApi._run_package_stage(
            profile, "target_multi_metric_read", 115.0, timeout)

    assert error.value.code == "timeout"
    assert called == ["first"]
    assert profile["stages"]["target_multi_metric_read"]["status"] == "timeout"
    assert profile["stages"]["peer_immediate_read"]["status"] == "not_run"


def test_package_stage_that_returns_after_deadline_fails_immediately(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("research_api.time.monotonic", lambda: clock[0])
    profile = GuardianResearchApi._package_profile()

    def crosses_deadline():
        clock[0] = 115.001
        return "late-result"

    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        GuardianResearchApi._run_package_stage(
            profile, "target_multi_metric_read", 115.0, crosses_deadline)

    assert error.value.code == "timeout"
    assert profile["stages"]["target_multi_metric_read"]["status"] == "timeout"
    assert profile["stages"]["peer_immediate_read"]["status"] == "not_run"


def test_unprofiled_package_stage_enforces_same_post_callback_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("research_api.time.monotonic", lambda: clock[0])

    def crosses_deadline():
        clock[0] = 115.001
        return "late-result"

    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        GuardianResearchApi._run_package_stage(
            None, "target_multi_metric_read", 115.0, crosses_deadline)

    assert error.value.code == "timeout"


def test_expired_package_logs_timeout_and_leaves_following_stages_not_run(
        tmp_path, caplog):
    api, _, _ = environment(tmp_path)

    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        with pytest.raises(research_timeseries.ResearchQueryError) as error:
            api._package({"event_id": "not-decoded", "profile": "true"}, deadline=-1)

    assert error.value.code == "timeout"
    profile = package_profile(caplog)
    assert profile["status"] == "timeout"
    assert profile["stages"]["event_id_decode_checksum"]["status"] == "timeout"
    assert profile["stages"]["event_id_decode_checksum"]["calls"] == 0
    assert profile["stages"]["bounded_event_reconstruction"]["status"] == "not_run"
    assert profile["stages"]["low_voltage"]["status"] == "not_run"
    assert profile["total_elapsed_seconds"] < 0.1


def test_envelope_contract_contains_only_observed_or_derived():
    value = research_envelope(source="x", evidence_class="OBSERVED", authoritative=True,
        timestamp_from=None, timestamp_to=None, resolution="event", data={})
    assert value["research_schema_version"] == 1
    assert value["evidence_class"] in {"OBSERVED", "DERIVED"}
    assert value["quality"]["status"] == "complete"


def test_acceptance_build_identity_is_explicit_and_non_secret():
    assert GUARDIAN_VERSION == "0.8.1"
    assert DIAGNOSTIC_ENGINE_VERSION == "0.4.12"
    assert SOURCE_COMMIT == UNAVAILABLE_SOURCE_COMMIT
    assert RESEARCH_SEMANTICS_VERSION == "research_soc_crash_evidence_v2"


def test_build_provenance_reports_supplied_revision_independently_of_version(tmp_path):
    revision = "a" * 40
    build_info = tmp_path / "build-info.json"
    build_info.write_text(json.dumps({
        "guardian_version": "0.8.1", "source_commit": revision,
    }))
    assert load_source_commit(build_info) == revision
    assert GUARDIAN_VERSION == "0.8.1"


@pytest.mark.parametrize("payload", [
    None,
    {},
    {"guardian_version": "0.8.0", "source_commit": "a" * 40},
    {"guardian_version": "0.8.1", "source_commit": "43c04ab"},
    {"guardian_version": "0.8.1", "source_commit": "G" * 40},
])
def test_missing_or_invalid_build_provenance_is_visible_and_never_stale(tmp_path, payload):
    build_info = tmp_path / "build-info.json"
    if payload is not None:
        build_info.write_text(json.dumps(payload))
    value = load_source_commit(build_info)
    assert value == UNAVAILABLE_SOURCE_COMMIT
    with pytest.raises(RuntimeError, match="source provenance unavailable"):
        require_source_commit(value)


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


def test_external_research_capabilities_are_scan_free_and_explicit(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    monkeypatch.setattr(api, "_refresh_identity",
                        lambda: pytest.fail("status must not refresh history"))
    monkeypatch.setattr(api.series, "evidence_by_serial",
                        lambda *args, **kwargs: pytest.fail("status must not scan history"))
    response = get(api, "status")
    contract = response.body["external_research_contract"]
    source = contract["sources"]["guardian.cell_history"]
    assert response.status == 200
    assert contract["scan_free_capability_discovery"] is True
    assert contract["acquisition_priority"] == "not_enforced_for_research_io"
    assert contract["background_io_budget_available"] is False
    assert contract["raw_evidence_endpoint"] == "/api/research/evidence/raw"
    assert contract["required_parameters"] == [
        "source", "physical_serial", "from", "to", "fields"]
    assert contract["unbounded_access"] is False
    assert source == {"physical_access": "required_block_index",
        "index_required": True, "index_availability": "validated_per_requested_day",
        "fallback": "fail_closed", "max_window_seconds": RAW_EVIDENCE_MAX_WINDOW_SECONDS,
        "max_page_records": RAW_EVIDENCE_MAX_PAGE_RECORDS,
        "max_scanned_records": RAW_EVIDENCE_MAX_SCAN_RECORDS,
        "fields": sorted(research_api.RAW_EVIDENCE_FIELDS),
        "pagination": "signed_cursor"}


def test_external_raw_evidence_requires_bounds_and_rejects_before_history_io(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    monkeypatch.setattr(api, "_refresh_identity",
                        lambda: pytest.fail("invalid request refreshed identity history"))
    monkeypatch.setattr(api.series, "evidence_by_serial",
                        lambda *args, **kwargs: pytest.fail("invalid request touched history"))
    base = ("evidence/raw?source=guardian.cell_history&physical_serial=SERIAL-M4"
            "&fields=timestamp,soc")
    for missing_range in (base + "&to=2026-09-11T01:00:00Z",
                          base + "&from=2026-09-11T00:00:00Z"):
        missing = get(api, missing_range)
        assert missing.status == 400 and missing.body["error"]["code"] == "invalid_argument"
    empty = get(api, base + "&from=2026-09-11T01:00:00Z"
                "&to=2026-09-11T01:00:00Z")
    assert empty.status == 400 and empty.body["error"]["code"] == "invalid_argument"
    oversized = get(api, "evidence/raw?source=guardian.cell_history&physical_serial=SERIAL-M4"
        "&from=2026-09-11T00:00:00Z&to=2026-09-11T06:00:01Z&fields=timestamp,soc")
    assert oversized.status == 413 and oversized.body["error"]["code"] == "range_too_large"
    excessive_page = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z"
        "&to=2026-09-11T12:00:00Z&fields=soc&max_records=501")
    assert excessive_page.status == 400
    unknown_source = get(api, "evidence/raw?source=guardian.unknown"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z"
        "&to=2026-09-11T12:00:00Z&fields=soc")
    assert unknown_source.status == 400
    invalid_field = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z"
        "&to=2026-09-11T12:00:00Z&fields=soc,secret_internal_field")
    assert invalid_field.status == 400


def test_external_raw_evidence_is_indexed_bounded_identity_aware_and_paginated(tmp_path):
    api, first, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    original = path.read_bytes()
    build_index(path, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    query = ("evidence/raw?source=guardian.cell_history&physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:59:00Z&to=2026-09-11T10:10:00Z"
        "&fields=timestamp,soc,current,voltage,cell_01,cell_15,temperature_channels"
        "&max_records=2")
    first_page = get(api, query)
    assert first_page.status == 200
    assert first_page.body["semantics_version"] == "guardian_external_raw_evidence_v1"
    assert first_page.body["truncated"] is True and first_page.body["next_cursor"]
    assert first_page.body["data"]["physical_access"]["read_mode"] == "indexed_chunk"
    assert first_page.body["data"]["physical_access"]["index_valid"] is True
    first_record = first_page.body["data"]["records"][0]
    assert first_record["physical_serial"] == "SERIAL-M4"
    assert first_record["position_at_time"] == 4
    assert first_record["position_history_id"] == first.position_history_id
    assert first_record["cell_01"] == 3300.0 and first_record["cell_15"] == 3314.0
    assert first_record["voltage"] == pytest.approx(sum(range(3300, 3315)) / 1000)
    second_page = get(api, query + "&cursor=" + quote(first_page.body["next_cursor"]))
    assert second_page.status == 200
    assert second_page.body["data"]["records"][0]["soc"] == 67
    wrong_query = query.replace("fields=timestamp", "fields=soc,timestamp")
    invalid = get(api, wrong_query + "&cursor=" + quote(first_page.body["next_cursor"]))
    assert invalid.status == 400 and invalid.body["error"]["code"] == "cursor_invalid"
    other_serial = query.replace("SERIAL-M4", "SERIAL-M5")
    invalid = get(api, other_serial + "&cursor=" + quote(first_page.body["next_cursor"]))
    assert invalid.status == 400 and invalid.body["error"]["code"] == "cursor_invalid"
    other_window = query.replace("09:59:00Z", "09:58:00Z")
    invalid = get(api, other_window + "&cursor=" + quote(first_page.body["next_cursor"]))
    assert invalid.status == 400 and invalid.body["error"]["code"] == "cursor_invalid"
    assert path.read_bytes() == original


def test_external_raw_evidence_preserves_missing_values_as_null(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    epoch = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    write_jsonl(path, [{"timestamp": epoch, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": 70}])
    build_index(path, timestamp_field="timestamp", iso_timestamp=False, block_records=1)
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T10:01:00Z&fields=current,voltage,cell_01")
    assert response.status == 200
    record = response.body["data"]["records"][0]
    assert record["current"] is record["voltage"] is record["cell_01"] is None


@pytest.mark.parametrize("index_state", ["missing", "invalid"])
def test_external_raw_evidence_fails_closed_without_valid_index(
        tmp_path, monkeypatch, index_state):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    original_open = Path.open
    source_reads = []
    if index_state == "invalid":
        index_path(source).write_text("{}", encoding="utf-8")

    def tracked_open(path, mode="r", *args, **kwargs):
        if Path(path) == source and "r" in mode:
            source_reads.append(mode)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z"
        "&to=2026-09-11T12:00:00Z&fields=timestamp,soc")
    assert response.status == 503
    assert response.body["error"]["code"] == "source_unavailable"
    assert source_reads == []


def test_external_raw_evidence_unknown_serial_is_explicitly_absent(tmp_path):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=UNKNOWN-SERIAL&from=2026-09-11T09:00:00Z"
        "&to=2026-09-11T12:00:00Z&fields=timestamp,soc")
    assert response.status == 200
    assert response.body["quality"]["status"] == "unavailable"
    assert response.body["data"]["records"] == []
    assert response.body["data"]["coverage"]["quality"] == "unavailable"


def test_external_raw_evidence_cursor_expires_when_source_changes(tmp_path):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    query = ("evidence/raw?source=guardian.cell_history&physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:59:00Z&to=2026-09-11T10:10:00Z"
        "&fields=timestamp,soc&max_records=2")
    first = get(api, query)
    assert first.status == 200 and first.body["next_cursor"]
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": datetime(
            2026, 9, 11, 10, 9, tzinfo=timezone.utc).timestamp(),
            "module": 4, "module_serial": "SERIAL-M4", "soc_percent": 60}) + "\n")
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    expired = get(api, query + "&cursor=" + quote(first.body["next_cursor"]))
    assert expired.status == 400 and expired.body["error"]["code"] == "cursor_invalid"


def test_external_raw_evidence_matching_record_limit_fails_closed(tmp_path):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    base = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    write_jsonl(source, [{"timestamp": base + index / 2, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": 70}
        for index in range(RAW_EVIDENCE_MAX_SCAN_RECORDS + 1)])
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=256)
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T12:00:00Z&fields=timestamp,soc")
    assert response.status == 413
    assert response.body["error"]["code"] == "range_too_dense"


def test_external_raw_evidence_does_not_touch_unrequested_historical_days(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    requested = api.paths.cell_history / "2026-09-11.jsonl"
    build_index(requested, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    unrelated = api.paths.cell_history / "2020-01-01.jsonl"
    write_jsonl(unrelated, [{"timestamp": 1, "module_serial": "SERIAL-M4"}])
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if Path(path) == unrelated:
            pytest.fail("unrequested system-age history was opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:00:00Z"
        "&to=2026-09-11T12:00:00Z&fields=timestamp,soc")
    assert response.status == 200


def test_external_raw_evidence_selects_only_overlapping_index_blocks(tmp_path):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    index = build_index(source, timestamp_field="timestamp", iso_timestamp=False,
                        block_records=2)
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T10:03:30Z"
        "&to=2026-09-11T10:04:30Z&fields=timestamp,soc")
    access = response.body["data"]["physical_access"]
    start_epoch = datetime(2026, 9, 11, 10, 3, 30, tzinfo=timezone.utc).timestamp()
    end_epoch = datetime(2026, 9, 11, 10, 4, 30, tzinfo=timezone.utc).timestamp()
    overlapping = [block for block in index["blocks"]
                   if block["max_timestamp"] >= start_epoch
                   and block["min_timestamp"] <= end_epoch]
    expected = sum(block["end_offset"] - block["start_offset"]
                   for block in overlapping)
    assert response.status == 200
    assert len(overlapping) == 2
    assert access["selected_ranges"] == 1
    assert access["selected_bytes"] == access["raw_bytes_read"] == expected
    assert access["records_inspected"] == 4
    assert access["full_json_decode_count"] == 3
    assert access["record_materialization_count"] == 1


def test_external_raw_evidence_never_reads_complete_position_history(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    monkeypatch.setattr(PositionHistoryLog, "read_all",
                        lambda self: pytest.fail("bounded request called read_all"))
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T10:10:00Z&fields=timestamp,soc")
    assert response.status == 200
    assert response.body["data"]["physical_access"]["identity_assignment_count"] == 5


def test_deferred_production_identity_snapshot_keeps_status_and_raw_requests_scan_free(
        tmp_path, monkeypatch):
    prepared, first, second = environment(tmp_path)
    source = prepared.paths.cell_history / "2026-09-11.jsonl"
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    api = GuardianResearchApi(prepared.paths, cursor_secret=b"test", defer_identity=True)
    monkeypatch.setattr(PositionHistoryLog, "read_all",
                        lambda self: pytest.fail("HTTP request called read_all"))
    assert get(api, "status").status == 200
    unavailable = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T10:10:00Z&fields=timestamp,soc")
    assert unavailable.status == 503
    api.install_identity_snapshot((first, second),
                                  api._file_signature(api.paths.position_history))
    available = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T10:10:00Z&fields=timestamp,soc")
    assert available.status == 200


def test_external_raw_identity_lookup_cost_ignores_unrelated_older_history(
        tmp_path, monkeypatch):
    positions = tmp_path / "position.jsonl"
    old = [snapshot((datetime(2020, 1, 1, tzinfo=timezone.utc)
        + timedelta(minutes=index)).isoformat(), {4: "SERIAL-M4"})
        for index in range(1000)]
    current = snapshot("2026-09-11T00:00:00Z", {4: "SERIAL-M4"})
    write_jsonl(positions, [item.to_dict() for item in (*old, current)])
    history = tmp_path / "cells"
    base = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    write_jsonl(history / "2026-09-11.jsonl", [{"timestamp": base + index * 60,
        "module": 4, "module_serial": "SERIAL-M4", "soc_percent": 70}
        for index in range(4)])
    build_index(history / "2026-09-11.jsonl", timestamp_field="timestamp",
                iso_timestamp=False, block_records=2)
    api = GuardianResearchApi(ResearchPaths(history, positions,
        tmp_path / "maintenance.jsonl", tmp_path / "canonical", tmp_path / "daily"),
        cursor_secret=b"test")
    bisect_calls = []
    real_bisect = research_identity.bisect_right
    monkeypatch.setattr(research_identity, "bisect_right",
                        lambda values, target: bisect_calls.append(len(values))
                        or real_bisect(values, target))
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T10:05:00Z&fields=timestamp,soc")
    access = response.body["data"]["physical_access"]
    assert response.status == 200
    assert access["identity_assignment_count"] == 4
    assert access["record_materialization_count"] == 4
    assert len(bisect_calls) == 8


def test_external_raw_identity_snapshot_preserves_position_epoch_change(tmp_path):
    positions = tmp_path / "position.jsonl"
    before = snapshot("2026-09-11T10:00:00Z", {4: "SERIAL-M4"})
    after = snapshot("2026-09-11T10:03:00Z", {2: "SERIAL-M4"})
    write_jsonl(positions, [before.to_dict(), after.to_dict()])
    history = tmp_path / "cells"
    write_jsonl(history / "2026-09-11.jsonl", [
        {"timestamp": datetime(2026, 9, 11, 10, 2, tzinfo=timezone.utc).timestamp(),
         "module": 4, "module_serial": "SERIAL-M4",
         "position_history_id": before.position_history_id, "soc_percent": 70},
        {"timestamp": datetime(2026, 9, 11, 10, 4, tzinfo=timezone.utc).timestamp(),
         "module": 2, "module_serial": "SERIAL-M4",
         "position_history_id": after.position_history_id, "soc_percent": 69}])
    build_index(history / "2026-09-11.jsonl", timestamp_field="timestamp",
                iso_timestamp=False, block_records=1)
    api = GuardianResearchApi(ResearchPaths(history, positions,
        tmp_path / "maintenance.jsonl", tmp_path / "canonical", tmp_path / "daily"),
        cursor_secret=b"test")
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T10:01:00Z"
        "&to=2026-09-11T10:05:00Z&fields=timestamp,soc")
    records = response.body["data"]["records"]
    assert [item["position_at_time"] for item in records] == [4, 2]
    assert [item["position_history_id"] for item in records] == [
        before.position_history_id, after.position_history_id]
    assert records[0]["identity_epoch_id"] != records[1]["identity_epoch_id"]


def test_external_raw_identity_change_fails_closed_without_refresh(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    source = api.paths.cell_history / "2026-09-11.jsonl"
    build_index(source, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    with api.paths.position_history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot(
            "2026-09-13T00:00:00Z", {3: "SERIAL-M4"}).to_dict()) + "\n")
    monkeypatch.setattr(PositionHistoryLog, "read_all",
                        lambda self: pytest.fail("stale snapshot triggered read_all"))
    response = get(api, "evidence/raw?source=guardian.cell_history"
        "&physical_serial=SERIAL-M4&from=2026-09-11T09:59:00Z"
        "&to=2026-09-11T10:10:00Z&fields=timestamp,soc")
    assert response.status == 503
    assert response.body["error"]["code"] == "source_unavailable"


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


@pytest.mark.parametrize("max_points", [1, 2, 200])
def test_soc_lazy_projection_preserves_sampling_contract(tmp_path, max_points):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    base = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    rows = [{"schema_version": 1, "timestamp": base + index * 60, "module": 4,
        "module_serial": "SERIAL-M4", "soc_percent": 70 - index / 10,
        "current_a": -1, "voltages_mv": [3300 + cell for cell in range(15)],
        "temperatures_c": [20 + cell / 10 for cell in range(15)]}
        for index in range(401)]
    write_jsonl(path, rows)
    profile = {}
    result = api.series.query(metric="soc", physical_serial="SERIAL-M4",
        timestamp_from="2026-09-11T10:00:00Z", timestamp_to="2026-09-11T16:40:00Z",
        resolution="display", max_points=max_points, io_profile=profile)
    expected_indexes = ([0] if max_points == 1 else
        [round(index * ((len(rows) - 1) / (max_points - 1))) for index in range(max_points)])
    assert [(point["timestamp"], point["value"]) for point in result["points"]] == [
        (datetime.fromtimestamp(rows[index]["timestamp"], timezone.utc).isoformat(),
         float(rows[index]["soc_percent"])) for index in expected_indexes]
    assert result["source_point_count"] == result["coverage"]["sample_count"] == len(rows)
    assert profile["full_json_decode_count"] == len(rows)
    assert profile["serial_at_calls"] == 0
    assert profile["position_at_calls"] == max_points
    assert profile["materialized_output_points"] == max_points


def test_soc_lazy_projection_keeps_position_history_fallback_and_unresolved(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-09.jsonl"
    epoch = datetime(2026, 9, 9, 23, 59, tzinfo=timezone.utc).timestamp()
    write_jsonl(path, [{"timestamp": epoch, "module": 4, "soc_percent": 71}])
    unresolved = api.series.query(metric="soc", physical_serial="SERIAL-M4",
        timestamp_from="2026-09-09T23:58:00Z", timestamp_to="2026-09-09T23:59:59Z")
    assert unresolved["points"] == []
    path = api.paths.cell_history / "2026-09-11.jsonl"
    observed_epoch = datetime(2026, 9, 11, 0, 0, 30, tzinfo=timezone.utc).timestamp()
    write_jsonl(path, [{"timestamp": observed_epoch, "module": 4, "soc_percent": 70}])
    profile = {}
    resolved = api.series.query(metric="soc", physical_serial="SERIAL-M4",
        timestamp_from="2026-09-11T00:00:00Z", timestamp_to="2026-09-11T00:01:00Z",
        io_profile=profile)
    assert resolved["points"][0]["identity_source"] == "position_history"
    assert resolved["points"][0]["position_at_time"] == 4
    assert profile["serial_at_calls"] == profile["position_at_calls"] == 1


@pytest.mark.parametrize("index_mode", ["valid", "missing", "invalid"])
def test_pt7d_soc_reuse_matches_complete_query_across_eight_days(
        tmp_path, index_mode):
    api, _, _ = environment(tmp_path)
    api.paths.cell_history.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    reuse_start = end - timedelta(days=1)
    sources = {}
    for day_offset in range(8):
        day = (start + timedelta(days=day_offset)).date()
        path = api.paths.cell_history / f"{day.isoformat()}.jsonl"
        records = []
        for hour in (0, 6, 12, 18):
            stamp = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=hour)
            for module in range(1, 7):
                records.append({"timestamp": stamp.timestamp(), "module": module,
                    "module_serial": f"SERIAL-M{module}", "soc_percent": 80 - day_offset - hour / 24,
                    "voltages_mv": [3300] * 15, "temperatures_c": [20] * 15})
        write_jsonl(path, records)
        if index_mode == "valid":
            build_index(path, timestamp_field="timestamp", iso_timestamp=False, block_records=6)
        elif index_mode == "invalid":
            index_path(path).write_text("{}", encoding="utf-8")
        sources[path] = path.read_bytes()
    recent = api.series.evidence_by_serial(["SERIAL-M4"], reuse_start.isoformat(),
                                           (end + timedelta(minutes=30)).isoformat())
    complete = api.series.query(metric="soc", physical_serial="SERIAL-M4",
        timestamp_from=start.isoformat(), timestamp_to=end.isoformat(),
        resolution="display", max_points=200)
    merged = api.series.soc_query_with_evidence(physical_serial="SERIAL-M4",
        timestamp_from=start.isoformat(), timestamp_to=end.isoformat(),
        reusable_evidence=recent, reusable_from=reuse_start.isoformat(),
        resolution="display", max_points=200)
    assert merged == complete
    assert all(path.read_bytes() == content for path, content in sources.items())


def test_pt7d_soc_reuse_preserves_duplicate_timestamps_and_boundary_once(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    boundary = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    rows = [{"timestamp": boundary - 60, "module": 4, "module_serial": "SERIAL-M4",
             "soc_percent": 72},
            {"timestamp": boundary, "module": 4, "module_serial": "SERIAL-M4",
             "soc_percent": 71},
            {"timestamp": boundary, "module": 4, "module_serial": "SERIAL-M4",
             "soc_percent": 70}]
    write_jsonl(path, rows)
    recent = api.series.evidence_by_serial(["SERIAL-M4"],
        datetime.fromtimestamp(boundary, timezone.utc).isoformat(),
        datetime.fromtimestamp(boundary + 60, timezone.utc).isoformat())
    merged = api.series.soc_query_with_evidence(physical_serial="SERIAL-M4",
        timestamp_from=datetime.fromtimestamp(boundary - 60, timezone.utc).isoformat(),
        timestamp_to=datetime.fromtimestamp(boundary + 60, timezone.utc).isoformat(),
        reusable_evidence=recent,
        reusable_from=datetime.fromtimestamp(boundary, timezone.utc).isoformat(),
        resolution="full", max_points=200)
    assert [point["value"] for point in merged["points"]] == [72.0, 71.0, 70.0]
    assert merged["source_point_count"] == 3


def test_soc_lazy_projection_deadline_remains_fail_closed(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    monkeypatch.setattr(research_timeseries.time, "monotonic", lambda: 20)
    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        api.series.query(metric="soc", physical_serial="SERIAL-M4",
            timestamp_from="2026-09-11T09:00:00Z", timestamp_to="2026-09-11T12:00:00Z",
            deadline=10)
    assert error.value.code == "timeout"


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
    assert 14 < profile["stages"]["event_id_decode_checksum"][
        "deadline_remaining_seconds_at_entry"] <= EVIDENCE_PACKAGE_TIMEOUT_SECONDS
    assert set(profile["stages"]) == set(PACKAGE_PROFILE_STAGES)
    assert profile["stages"]["module_soc"]["calls"] == 4
    soc_reader = profile["stages"]["module_soc"]["reader"]
    assert soc_reader["reused_evidence_records"] > 0
    assert soc_reader["position_at_calls"] <= profile["stages"]["module_soc"]["samples_returned"]
    assert soc_reader["materialized_output_points"] > 0
    assert profile["stages"]["target_multi_metric_read"]["samples_returned"] > 0
    reader = profile["stages"]["target_multi_metric_read"]["reader"]
    peer_reader = profile["stages"]["peer_immediate_read"]["reader"]
    assert reader["requested_serials"] == 1
    assert peer_reader["requested_serials"] == 2
    assert reader["requested_window_seconds"] == pytest.approx(24.5 * 3600 + 240)
    assert peer_reader["requested_window_seconds"] == pytest.approx(14 * 60)
    assert reader["raw_bytes_read"] == reader["bytes_read"] > 0
    assert reader["raw_records_inspected"] == reader["records_inspected"] > 0
    assert reader["selected_bytes"] >= reader["selected_progress_bytes"] > 0
    assert 0 < reader["selected_progress_percent"] <= 100
    assert reader["range_count"] > 0 and reader["raw_chunk_reads"] > 0
    assert (reader["target_records_accepted"] + reader["peer_records_accepted"]
            == reader["records_accepted_wanted_serial"])
    assert reader["full_json_decode_count"] >= reader["records_accepted_wanted_serial"]
    assert reader["identity_assignment_count"] == reader["records_accepted_wanted_serial"]
    assert reader["cell_array_conversion_count"] == reader["records_accepted_wanted_serial"]
    assert reader["derived_cell_context_count"] == reader["records_accepted_wanted_serial"]
    assert reader["record_materialization_count"] == reader["records_accepted_wanted_serial"]
    assert set(reader["timings_seconds"]) == {
        "raw_chunk_read", "serial_prefilter", "full_json_decode",
        "timestamp_range_check", "identity_assignment", "cell_array_conversion",
        "derived_cell_context", "record_materialization", "deadline_check",
        "balancing_extraction", "temperature_extraction", "module_metric_extraction",
        "file_discovery_setup", "block_index_load_validate_select",
        "source_open_range_seek", "binary_line_framing",
        "result_sort_signature_fingerprint"}
    assert all(reader["timings_seconds"][name] >= 0 for name in (
        "file_discovery_setup", "block_index_load_validate_select",
        "source_open_range_seek", "binary_line_framing",
        "result_sort_signature_fingerprint"))
    accounted = sum(reader["timings_seconds"][name]
                    for name in READER_ACCOUNTED_TIMINGS)
    assert reader["reader_accounted_seconds"] == pytest.approx(accounted)
    assert reader["reader_unattributed_seconds"] == pytest.approx(max(
        0.0, profile["stages"]["target_multi_metric_read"]["elapsed_seconds"] - accounted))
    assert profile["coverage_status"]["module_soc"] in {"complete", "partial"}
    assert profile["stages"]["soc_recalibration"]["status"] == "unavailable"


def test_evidence_package_passes_one_absolute_deadline_to_detector_and_readers(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    deadlines = []
    original_detector = api.series.soc_current_by_serial
    original_evidence = api.series.evidence_by_serial
    def detector(*args, **kwargs):
        deadlines.append(kwargs.get("deadline", args[3]))
        return original_detector(*args, **kwargs)
    def evidence(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return original_evidence(*args, **kwargs)
    monkeypatch.setattr(api.series, "soc_current_by_serial", detector)
    monkeypatch.setattr(api.series, "evidence_by_serial", evidence)
    response = get(api, "evidence-package?event_id=" + event["event_id"])
    assert response.status == 200
    assert len(deadlines) >= 3 and len(set(deadlines)) == 1


def test_unknown_evidence_event_fails_before_history_package_reads(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    reads = []
    monkeypatch.setattr(api.series, "evidence_by_serial",
                        lambda *args, **kwargs: reads.append(True))
    response = get(api, "evidence-package?event_id=invalid")
    assert response.status == 400
    assert response.body["error"]["code"] == "invalid_argument"
    assert reads == []


def test_evidence_package_profile_adds_no_history_scans(tmp_path, monkeypatch, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    suffix = "evidence-package?event_id=" + event["event_id"]
    counts = {"query": 0, "soc_reuse": 0, "evidence": 0}
    evidence_profiles = []
    query, soc_reuse = api.series.query, api.series.soc_query_with_evidence
    evidence = api.series.evidence_by_serial
    def tracked_query(*args, **kwargs):
        counts["query"] += 1
        return query(*args, **kwargs)
    def tracked_evidence(*args, **kwargs):
        counts["evidence"] += 1
        evidence_profiles.append(kwargs.get("io_profile"))
        return evidence(*args, **kwargs)
    def tracked_soc_reuse(*args, **kwargs):
        counts["soc_reuse"] += 1
        return soc_reuse(*args, **kwargs)
    monkeypatch.setattr(api.series, "query", tracked_query)
    monkeypatch.setattr(api.series, "soc_query_with_evidence", tracked_soc_reuse)
    monkeypatch.setattr(api.series, "evidence_by_serial", tracked_evidence)
    get(api, suffix)
    normal = dict(counts)
    assert evidence_profiles == [None, None]
    counts.update(query=0, soc_reuse=0, evidence=0)
    evidence_profiles.clear()
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        get(api, suffix + "&profile=true")
    assert counts == normal == {"query": 0, "soc_reuse": 1, "evidence": 2}
    assert len(evidence_profiles) == 2 and all(item is not None for item in evidence_profiles)


def test_evidence_package_timeout_logs_complete_redacted_profile(tmp_path, monkeypatch, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    def timeout_on_main_scan(*args, **kwargs):
        io = kwargs["io_profile"]
        io.update({"files_discovered": 2, "files_opened": 1,
            "bytes_read": 1024, "raw_bytes_read": 1024,
            "records_inspected": 4, "raw_records_inspected": 4,
            "samples_returned": 0, "index_present": True, "index_valid": True,
            "read_mode": "indexed_chunk", "selected_bytes": 4096,
            "selected_progress_bytes": 0, "selected_progress_percent": 0.0,
            "range_count": 2, "raw_chunk_reads": 1,
            "records_skipped_serial_prefilter": 2,
            "records_accepted_wanted_serial": 2,
            "target_records_accepted": 1, "peer_records_accepted": 1,
            "full_json_decode_count": 2, "identity_assignment_count": 2,
            "cell_array_conversion_count": 2, "derived_cell_context_count": 2,
            "record_materialization_count": 1,
            "timings_seconds": {name: 0.001 for name in (
                "raw_chunk_read", "serial_prefilter", "full_json_decode",
                "timestamp_range_check", "identity_assignment", "cell_array_conversion",
                "derived_cell_context", "record_materialization", "deadline_check",
                "balancing_extraction", "temperature_extraction", "module_metric_extraction",
                "file_discovery_setup", "block_index_load_validate_select",
                "source_open_range_seek", "binary_line_framing",
                "result_sort_signature_fingerprint")}})
        raise research_timeseries.ResearchQueryError(
            "timeout", "research query timed out", 503)
    monkeypatch.setattr(api.series, "evidence_by_serial", timeout_on_main_scan)
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        response = get(api, "evidence-package?event_id=" + event["event_id"] + "&profile=true")
    assert response.status == 503 and response.body["error"]["code"] == "timeout"
    profile = package_profile(caplog)
    assert profile["status"] == "timeout"
    assert profile["stages"]["target_multi_metric_read"]["status"] == "timeout"
    assert profile["stages"]["peer_immediate_read"]["status"] == "not_run"
    assert profile["stages"]["module_soc"]["status"] == "not_run"
    assert profile["stages"]["module_current"]["status"] == "not_run"
    assert profile["stages"]["module_voltage"]["status"] == "not_run"
    reader = profile["stages"]["target_multi_metric_read"]["reader"]
    assert reader["selected_progress_bytes"] == 1024
    assert reader["selected_progress_percent"] == 25
    assert reader["target_records_accepted"] + reader["peer_records_accepted"] == 2
    assert reader["timings_seconds"]["raw_chunk_read"] == pytest.approx(0.001)
    assert reader["reader_accounted_seconds"] == pytest.approx(
        len(READER_ACCOUNTED_TIMINGS) * 0.001)
    assert reader["reader_unattributed_seconds"] >= 0
    encoded = json.dumps(profile)
    assert event["event_id"] not in encoded
    assert "SERIAL-M4" not in encoded
    assert "soc_percent" not in encoded
    assert "token" not in encoded.lower() and "secret" not in encoded.lower()


def test_evidence_package_peer_read_timeout_remains_fail_closed(tmp_path, monkeypatch, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    original = api.series.evidence_by_serial
    calls = 0

    def timeout_on_peer_scan(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise research_timeseries.ResearchQueryError(
                "timeout", "research query timed out", 503)
        return original(*args, **kwargs)

    monkeypatch.setattr(api.series, "evidence_by_serial", timeout_on_peer_scan)
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        response = get(api, "evidence-package?event_id=" + event["event_id"] + "&profile=true")

    assert response.status == 503 and response.body["error"]["code"] == "timeout"
    profile = package_profile(caplog)
    assert profile["stages"]["target_multi_metric_read"]["status"] == "complete"
    assert profile["stages"]["peer_immediate_read"]["status"] == "timeout"
    assert profile["stages"]["module_soc"]["status"] == "not_run"


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
    assert profile["counts"]["cell_history_scans"] == 3
    assert profile["stages"]["target_multi_metric_read"]["files_opened"] == 2
    assert profile["stages"]["target_multi_metric_read"]["read_mode"] == "indexed_chunk"
    assert profile["stages"]["peer_immediate_read"]["files_opened"] == 2
    assert profile["stages"]["peer_immediate_read"]["read_mode"] == "indexed_chunk"
    for stage in ("module_soc", "module_current", "module_voltage",
                  "cell_voltages", "temperature_channels"):
        assert profile["stages"][stage]["read_mode"] == "indexed_chunk"
    assert [path.read_bytes() for path in (first_path, second_path)] == source


def package_for_crash(api):
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    return event, get(api, "evidence-package?event_id=" + event["event_id"])


def core_for_crash(api, suffix=""):
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    return event, get(api, "evidence-core?event_id=" + event["event_id"] + suffix)


def test_soc_crash_core_contract_windows_identity_and_evidence_classes(tmp_path):
    api, first, _ = environment(tmp_path)
    event, response = core_for_crash(api)

    assert response.status == 200
    assert response.body["semantics_version"] == CORE_EVIDENCE_VERSION
    data = response.body["data"]
    assert data["event_core"] == {
        "event_id": event["event_id"], "detector_version": "guardian_soc_crash_v1",
        "detector_thresholds": {"soc_loss_pp": 2, "sample_gap_seconds": 300,
            "discharge_current_below_a": -0.2, "merge_gap_seconds": 600},
        "event_start": event["start"], "event_end": event["end"],
        "duration_seconds": 240.0, "soc_before": 69.0, "soc_after": 65.0,
        "delta_soc": -4.0, "physical_serial": "SERIAL-M4",
        "historical_position": 4, "identity_epoch_id": event["identity_epoch_id"],
        "position_history_id": first.position_history_id, "identity_resolved": True,
        "event_quality": "complete", "source_references": ["guardian.cell_history"]}
    assert data["requested_intervals"] == {
        "target": {"from": "2026-09-11T09:52:00+00:00",
                   "to": "2026-09-11T10:36:00+00:00"},
        "peers": {"from": "2026-09-11T09:57:00+00:00",
                  "to": "2026-09-11T10:11:00+00:00"}}
    assert data["evidence_classes"] == ["OBSERVED", "DERIVED"]
    assert data["inferred"] is False and data["causality_determined"] is False
    assert "INFERRED" not in json.dumps(response.body)


def test_soc_crash_core_target_and_peer_records_are_strictly_bounded(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    target_template = next(row for row in rows if row["module_serial"] == "SERIAL-M4")
    peer_template = next(row for row in rows if row["module_serial"] == "SERIAL-M5")
    rows.extend([
        {**target_template, "timestamp": datetime(
            2026, 9, 11, 9, 51, tzinfo=timezone.utc).timestamp()},
        {**target_template, "timestamp": datetime(
            2026, 9, 11, 10, 37, tzinfo=timezone.utc).timestamp()},
        {**peer_template, "timestamp": datetime(
            2026, 9, 11, 9, 56, tzinfo=timezone.utc).timestamp()},
        {**peer_template, "timestamp": datetime(
            2026, 9, 11, 10, 12, tzinfo=timezone.utc).timestamp()},
    ])
    write_jsonl(path, rows)

    _, response = core_for_crash(api)

    target = response.body["data"]["target_evidence"]["records"]
    assert target and all("2026-09-11T09:52:00" <= row["timestamp"]
                          <= "2026-09-11T10:36:00+00:00" for row in target)
    peers = response.body["data"]["peer_evidence"]["modules"]
    assert {row["physical_serial"] for row in peers} == {"SERIAL-M5", "SERIAL-M6"}
    assert all("2026-09-11T09:57:00" <= sample["timestamp"]
               <= "2026-09-11T10:11:00+00:00"
               for peer in peers for sample in peer["records"])


def test_soc_crash_core_preserves_cells_temperatures_and_derived_context(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = core_for_crash(api)
    target = response.body["data"]["target_evidence"]
    record = target["records"][0]

    assert len(record["cell_voltages_mv"]) == 15
    assert len(record["cell_temperatures_c"]) == 15
    assert target["temperature_semantics"] == "recorded_module_temperature_channels_only"
    assert record["derived"]["minimum_cell_voltage_mv"] == 3300
    assert record["derived"]["maximum_cell_voltage_mv"] == 3314
    assert record["derived"]["median_cell_voltage_mv"] == 3307
    assert record["derived"]["cell_spread_mv"] == 14
    assert record["derived"]["lowest_cell"] == 1
    assert record["derived"]["highest_cell"] == 15
    assert record["derived"]["cell_deviation_from_module_median_mv"] == list(range(-7, 8))
    assert record["module_power_w"] == pytest.approx(
        record["module_voltage_v"] * record["module_current_a"])


def test_soc_crash_core_missing_optional_sources_are_unavailable_not_zero(tmp_path):
    api, _, _ = environment(tmp_path)
    _, response = core_for_crash(api)
    context = response.body["data"]["event_context"]

    assert context["bms_management"]["quality"] == "unavailable"
    assert context["bms_management"]["records"] == []
    assert context["low_voltage"]["quality"] == "unavailable"
    assert context["alarms"]["quality"] == "unknown"
    assert context["soc_recalibration"] == {
        "evidence_class": "OBSERVED", "quality": "unavailable", "records": []}
    assert response.body["data"]["coverage"]["alarms"]["quality"] == "unavailable"
    assert response.body["data"]["coverage"]["maintenance"]["quality"] == "unavailable"


def test_soc_crash_core_missing_temperatures_have_metric_unavailable_coverage(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row.get("module_serial") == "SERIAL-M4":
            row.pop("temperatures_c", None)
    write_jsonl(path, rows)

    _, response = core_for_crash(api)

    assert response.status == 200
    assert all(not row["cell_temperatures_c"]
               for row in response.body["data"]["target_evidence"]["records"])
    assert response.body["data"]["coverage"]["target_metrics"][
        "cell_temperature"]["quality"] == "unavailable"
    assert response.body["data"]["event_context"]["canonical_phase"][
        "evidence_class"] == "DERIVED"


def test_soc_crash_core_is_deterministic_read_only_and_v2_remains_unchanged(tmp_path):
    api, _, _ = environment(tmp_path)
    source = (api.paths.cell_history / "2026-09-11.jsonl").read_bytes()
    event, first = core_for_crash(api)
    _, second = core_for_crash(api)
    v2 = get(api, "evidence-package?event_id=" + event["event_id"])

    assert first.status == second.status == v2.status == 200
    assert first.body["data"] == second.body["data"]
    assert first.body["data"]["input_fingerprint"] == second.body["data"]["input_fingerprint"]
    assert first.body["provenance"]["package_created_at"] != ""
    assert v2.body["semantics_version"] == "research_soc_crash_evidence_v2"
    assert (api.paths.cell_history / "2026-09-11.jsonl").read_bytes() == source


def test_soc_crash_core_exposes_config_revision_but_not_config_values(tmp_path):
    api, _, _ = environment(tmp_path)
    config = tmp_path / "config.jsonl"
    write_jsonl(config, [{"effective_at": "2026-09-10T00:00:00+00:00",
        "config_revision": "cfg-7", "guardian_research_api_token": "must-not-leak"}])
    api = GuardianResearchApi(replace(api.paths, config_history=config),
                              cursor_secret=b"test")

    _, response = core_for_crash(api)
    encoded = json.dumps(response.body)

    assert response.body["provenance"]["config_revision"] == "cfg-7"
    assert "config" not in response.body["data"]
    assert "must-not-leak" not in encoded
    assert "guardian_research_api_token" not in encoded


def test_soc_crash_core_rejects_resource_expansion_and_unknown_event(tmp_path):
    api, _, _ = environment(tmp_path)
    event, _ = core_for_crash(api)
    expanded = get(api, "evidence-core?event_id=" + event["event_id"] + "&before=P1D")
    unknown = get(api, "evidence-core?event_id=SCE-invalid")

    assert expanded.status == 400
    assert expanded.body["error"]["code"] == "invalid_argument"
    assert unknown.status == 400
    assert unknown.body["error"]["code"] == "invalid_argument"


def test_soc_crash_core_profile_has_two_scans_and_bounded_counts(tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    build_index(path, timestamp_field="timestamp", iso_timestamp=False,
                block_records=2)
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        _, response = core_for_crash(api, "&profile=true")
    profile = json.loads(next(record.message.removeprefix("RESEARCH_CORE_PROFILE ")
        for record in caplog.records
        if record.message.startswith("RESEARCH_CORE_PROFILE ")))

    assert response.status == 200 and profile["status"] == "ok"
    assert set(profile["stages"]) == set(CORE_PROFILE_STAGES)
    assert profile["counts"]["cell_history_scans"] == 2
    assert profile["counts"]["rs485_scans"] == 1
    assert profile["counts"]["target_records"] == 5
    assert profile["counts"]["peer_records"] == 2
    assert profile["stages"]["target_core_read"]["read_mode"] == "indexed_chunk"
    assert profile["stages"]["peer_core_read"]["read_mode"] == "indexed_chunk"
    assert profile["total_elapsed_seconds"] < QUERY_TIMEOUT_SECONDS


def profiled_rs485_core_environment(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    rs485 = tmp_path / "rs485"
    rows = [
        {"record_type": "frame", "timestamp": "2026-09-11T09:50:00+00:00",
         "direction": "response", "paired_command": 0x93, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True},
        {"record_type": "frame", "timestamp": "2026-09-11T10:03:00+00:00",
         "direction": "response", "paired_command": 0x92, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True,
         "decoded": {"charge_current_limit_a": 10,
                     "discharge_current_limit_a": -25,
                     "charge_enable": True, "discharge_enable": True}},
        {"record_type": "frame", "timestamp": "2026-09-11T10:04:00+00:00",
         "direction": "response", "paired_command": 0x44, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True,
         "decoded": {"low_voltage_warning": True}},
        {"record_type": "frame", "timestamp": "2026-09-11T10:05:00+00:00",
         "direction": "response", "paired_command": 0x47, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True,
         "decoded": {"module_under_voltage_limit_v": 45.0}},
        {"record_type": "frame", "timestamp": "2026-09-11T10:06:00+00:00",
         "direction": "response", "paired_command": 0x42, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True,
         "decoded": {}},
    ]
    source = rs485 / "2026-09-11.jsonl"
    write_jsonl(source, rows)
    rebuild_rs485_index(source, block_records=2)
    monkeypatch.setattr(research_api, "decode_identity_record",
        lambda record: ({"serial_string": "SERIAL-M4"}
                        if record.get("paired_command") == 0x93 else None))
    return GuardianResearchApi(replace(api.paths, rs485_history=rs485),
                               cursor_secret=b"test"), source


def test_rs485_core_profile_has_non_overlapping_substages_and_preserves_evidence(
        tmp_path, monkeypatch):
    api, source = profiled_rs485_core_environment(tmp_path, monkeypatch)
    start, end = "2026-09-11T09:52:00+00:00", "2026-09-11T10:36:00+00:00"
    raw_before = source.read_bytes()
    expected = api._rs485_core_context("SERIAL-M4", start, end, float("inf"))
    profile = api._core_profile()
    io_profile = {}
    actual = api._run_package_stage(
        profile, "rs485_core_context", float("inf"),
        lambda: api._rs485_core_context(
            "SERIAL-M4", start, end, float("inf"), io_profile=io_profile),
        io_profile=io_profile)

    assert actual == expected
    assert source.read_bytes() == raw_before
    reader = profile["stages"]["rs485_core_context"]["reader"]
    assert set(reader["timings_seconds"]) == set(
        research_api.RS485_CORE_ACCOUNTED_TIMINGS)
    assert reader["raw_records"] == reader["full_json_decode_count"] == 5
    assert {key: reader[key] for key in (
        "0x93_records", "0x92_records", "0x44_records", "0x47_records",
        "other_records", "identity_updates")} == {
            "0x93_records": 1, "0x92_records": 1, "0x44_records": 1,
            "0x47_records": 1, "other_records": 1, "identity_updates": 1}
    assert reader["target_identity_matches"] == 4
    accounted = sum(reader["timings_seconds"].values())
    assert reader["reader_accounted_seconds"] == pytest.approx(accounted)
    assert reader["reader_unattributed_seconds"] == pytest.approx(max(
        0.0, profile["stages"]["rs485_core_context"]["elapsed_seconds"] - accounted))
    encoded = json.dumps(reader)
    assert "SERIAL-M4" not in encoded and "charge_current_limit_a" not in encoded


def test_rs485_core_unprofiled_path_does_not_read_profiling_clock(
        tmp_path, monkeypatch):
    api, _ = profiled_rs485_core_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(research_api.time, "perf_counter",
                        lambda: pytest.fail("unprofiled RS485 reader used profiling clock"))

    result = api._rs485_core_context(
        "SERIAL-M4", "2026-09-11T09:52:00+00:00",
        "2026-09-11T10:36:00+00:00", float("inf"))

    assert result["management"]["records"]


def test_rs485_core_timeout_retains_complete_profile_balance(tmp_path, monkeypatch):
    api, _ = profiled_rs485_core_environment(tmp_path, monkeypatch)
    profile = api._core_profile()
    io_profile = {}
    checks = 0
    original = api._ensure_package_deadline

    def timeout_after_first_record(deadline):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise research_timeseries.ResearchQueryError(
                "timeout", "research query timed out", 503)
        return original(float("inf"))

    monkeypatch.setattr(api, "_ensure_package_deadline", timeout_after_first_record)
    with pytest.raises(research_timeseries.ResearchQueryError):
        api._run_package_stage(
            profile, "rs485_core_context", float("inf"),
            lambda: api._rs485_core_context(
                "SERIAL-M4", "2026-09-11T09:52:00+00:00",
                "2026-09-11T10:36:00+00:00", float("inf"),
                io_profile=io_profile), io_profile=io_profile)

    stage = profile["stages"]["rs485_core_context"]
    reader = stage["reader"]
    assert stage["status"] == "timeout"
    assert reader["raw_records"] == reader["full_json_decode_count"] == 1
    assert reader["reader_accounted_seconds"] == pytest.approx(
        sum(reader["timings_seconds"].values()))
    assert reader["reader_unattributed_seconds"] == pytest.approx(max(
        0.0, stage["elapsed_seconds"] - reader["reader_accounted_seconds"]))


def test_soc_crash_core_golden_contract_cases(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "soc_crash_core_evidence_v1.json"
    golden = json.loads(fixture.read_text(encoding="utf-8"))
    api, _, _ = environment(tmp_path)
    event, response = core_for_crash(api)
    data = response.body["data"]

    assert golden["semantics_version"] == response.body["semantics_version"]
    assert golden["complete"]["event_id"] == event["event_id"]
    assert golden["complete"]["target_cell_count"] == len(
        data["target_evidence"]["records"][0]["cell_voltages_mv"])
    assert golden["complete"]["peer_count"] == len(data["peer_evidence"]["modules"])
    assert golden["historical_topology"]["position"] == data[
        "event_core"]["historical_position"]
    assert golden["missing_optional_sources"] == {
        "bms": data["event_context"]["bms_management"]["quality"],
        "alarms": data["coverage"]["alarms"]["quality"],
        "soc_recalibration": data["event_context"]["soc_recalibration"]["quality"],
    }
    assert golden["resource_contract"]["deadline_seconds"] == QUERY_TIMEOUT_SECONDS
    assert golden["resource_contract"]["response_limit_bytes"] == 2 * 1024 * 1024


def test_soc_crash_core_deadline_is_fail_closed_and_stops_following_stages(
        tmp_path, monkeypatch, caplog):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    called = []

    def timeout(*args, **kwargs):
        raise research_timeseries.ResearchQueryError(
            "timeout", "research query timed out", 503)

    monkeypatch.setattr(api.series, "evidence_by_serial", timeout)
    monkeypatch.setattr(api, "_rs485_core_context",
                        lambda *args: called.append("rs485"))
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        response = get(api, "evidence-core?event_id=" + event["event_id"] + "&profile=true")
    profile = json.loads(next(record.message.removeprefix("RESEARCH_CORE_PROFILE ")
        for record in caplog.records
        if record.message.startswith("RESEARCH_CORE_PROFILE ")))

    assert response.status == 503 and response.body["error"]["code"] == "timeout"
    assert called == []
    assert profile["status"] == "timeout"
    assert profile["stages"]["target_core_read"]["status"] == "timeout"
    assert profile["stages"]["peer_core_read"]["status"] == "not_run"
    assert profile["stages"]["rs485_core_context"]["status"] == "not_run"


def test_soc_crash_core_no_peers_and_partial_coverage(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    lone = snapshot("2026-09-10T00:00:00+00:00", {4: "SERIAL-M4"})
    write_jsonl(api.paths.position_history, [lone.to_dict()])
    api = GuardianResearchApi(api.paths, cursor_secret=b"test")
    original = api.series.evidence_by_serial

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        result["truncated"] = True
        result["truncated_serials"] = list(args[0])
        return result

    monkeypatch.setattr(api.series, "evidence_by_serial", partial)
    _, response = core_for_crash(api)

    assert response.status == 200
    assert response.body["data"]["peer_evidence"] == {
        "historical_stack_at": response.body["data"]["event_core"]["event_start"],
        "modules": [], "quality": "unavailable"}
    assert response.body["data"]["coverage"]["target"]["quality"] == "partial"


def test_soc_crash_core_uses_event_time_peers_not_later_topology(tmp_path):
    api, _, _ = environment(tmp_path)
    first = snapshot("2026-09-10T00:00:00+00:00", {
        4: "SERIAL-M4", 5: "SERIAL-M5"})
    after = snapshot("2026-09-12T00:00:00+00:00", {
        2: "SERIAL-M4", 6: "SERIAL-M6"})
    write_jsonl(api.paths.position_history, [first.to_dict(), after.to_dict()])
    api = GuardianResearchApi(api.paths, cursor_secret=b"test")

    _, response = core_for_crash(api)

    assert response.body["data"]["event_core"]["historical_position"] == 4
    assert {(row["physical_serial"], row["position_at_event"])
            for row in response.body["data"]["peer_evidence"]["modules"]} == {
                ("SERIAL-M5", 5)}


def test_soc_crash_core_context_sources_are_bounded_to_target_window(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    technical = tmp_path / "technical.jsonl"
    base = datetime(2026, 9, 11, tzinfo=timezone.utc)
    write_jsonl(technical, [
        {"type": "alarm_started", "timestamp": (base + timedelta(
            hours=10, minutes=3)).timestamp(), "status": "active",
         "alarm": {"code": "4:test", "message": "inside", "module": 4,
                   "level": "warning"}},
        {"type": "alarm_started", "timestamp": (base + timedelta(
            hours=9, minutes=30)).timestamp(), "status": "active",
         "alarm": {"code": "4:outside", "message": "outside", "module": 4,
                   "level": "warning"}},
    ])
    inside = new_maintenance_event(
        occurred_at="2026-09-11T10:04:00+00:00", category="inspection",
        title="inside", affected_system="battery", module_number=4,
        module_serial="SERIAL-M4", now=base)
    outside = new_maintenance_event(
        occurred_at="2026-09-11T09:30:00+00:00", category="inspection",
        title="outside", affected_system="battery", module_number=4,
        module_serial="SERIAL-M4", now=base)
    write_jsonl(api.paths.maintenance, [inside.to_dict(), outside.to_dict()])
    rs485 = tmp_path / "rs485"
    write_jsonl(rs485 / "2026-09-11.jsonl", [
        {"record_type": "frame", "timestamp": "2026-09-11T09:50:00+00:00",
         "direction": "response", "paired_command": 0x93, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True},
        {"record_type": "frame", "timestamp": "2026-09-11T10:03:00+00:00",
         "direction": "response", "paired_command": 0x92, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True,
         "decoded": {"charge_current_limit_a": 10,
                     "discharge_current_limit_a": -25,
                     "charge_enable": True, "discharge_enable": True}},
        {"record_type": "frame", "timestamp": "2026-09-11T09:30:00+00:00",
         "direction": "response", "paired_command": 0x92, "adr": 4,
         "checksum_valid": True, "frame_complete": True, "request_matched": True,
         "decoded": {"charge_current_limit_a": 0}},
    ])
    monkeypatch.setattr(research_api, "decode_identity_record",
        lambda record: ({"serial_string": "SERIAL-M4"}
                        if record.get("paired_command") == 0x93 else None))
    api = GuardianResearchApi(replace(api.paths, technical_events=technical,
        rs485_history=rs485), cursor_secret=b"test")

    _, response = core_for_crash(api)
    context = response.body["data"]["event_context"]
    golden = json.loads((Path(__file__).parent / "fixtures" /
                         "soc_crash_core_evidence_v1.json").read_text(encoding="utf-8"))

    assert len(context["alarms"]["records"]) == golden["complete"]["alarm_count"]
    assert [row["summary"] for row in context["alarms"]["records"]] == ["inside"]
    assert [row["title"] for row in context["maintenance"]["records"]] == ["inside"]
    assert len(context["bms_management"]["records"]) == golden["complete"]["bms_count"]
    assert context["bms_management"]["records"][0]["charge_current_limit_a"] == 10
    assert all("2026-09-11T09:52:00" <= row["timestamp"]
               <= "2026-09-11T10:36:00+00:00"
               for row in context["bms_management"]["records"])


def test_soc_crash_core_uses_bounded_alarm_timeline_index(tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    technical = tmp_path / "events.jsonl"
    origin = datetime(2026, 9, 4, tzinfo=timezone.utc)
    rows = [{"type": "alarm_started",
             "timestamp": (origin + timedelta(minutes=index)).timestamp(),
             "status": "active", "alarm": {"code": f"4:test-{index}",
             "message": f"event {index}", "module": 4, "level": "warning"}}
            for index in range(20_749)]
    write_jsonl(technical, rows)
    raw = technical.read_bytes()
    rebuild_timeline_index(technical)
    api = GuardianResearchApi(replace(api.paths, technical_events=technical),
                              cursor_secret=b"test")

    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        _, response = core_for_crash(api, "&profile=true")

    assert response.status == 200
    profile = json.loads(next(record.message.removeprefix("RESEARCH_CORE_PROFILE ")
        for record in caplog.records
        if record.message.startswith("RESEARCH_CORE_PROFILE ")))
    stage = profile["stages"]["alarms"]
    assert stage["status"] == "complete"
    assert stage["read_mode"] == "timeline_block_index"
    assert stage["index_present"] is True and stage["index_valid"] is True
    assert 0 < stage["selected_blocks"] < 10
    assert stage["raw_bytes_read"] < len(raw)
    assert stage["records_inspected"] < len(rows)
    assert stage["full_json_decode_count"] == stage["records_inspected"]
    assert technical.read_bytes() == raw


def test_soc_crash_core_large_alarm_source_without_index_is_unavailable(tmp_path):
    api, _, _ = environment(tmp_path)
    technical = tmp_path / "events.jsonl"
    row = {"type": "alarm_started", "timestamp": datetime(
        2026, 9, 11, 10, tzinfo=timezone.utc).timestamp(), "status": "active",
        "alarm": {"code": "4:test", "message": "test", "module": 4,
                  "level": "warning"}}
    write_jsonl(technical, [row] * 4_000)
    api = GuardianResearchApi(replace(api.paths, technical_events=technical),
                              cursor_secret=b"test")

    _, response = core_for_crash(api)

    assert response.status == 200
    assert response.body["data"]["event_context"]["alarms"] == {
        "evidence_class": "OBSERVED", "quality": "unavailable", "records": []}
    assert response.body["data"]["coverage"]["alarms"]["quality"] == "unavailable"


def test_soc_crash_core_passes_one_absolute_deadline_to_detector_and_readers(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    deadlines = []
    original_detector = api.series.soc_current_by_serial
    original_evidence = api.series.evidence_by_serial

    def detector(*args, **kwargs):
        deadlines.append(kwargs.get("deadline", args[3]))
        return original_detector(*args, **kwargs)

    def evidence(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return original_evidence(*args, **kwargs)

    monkeypatch.setattr(api.series, "soc_current_by_serial", detector)
    monkeypatch.setattr(api.series, "evidence_by_serial", evidence)
    response = get(api, "evidence-core?event_id=" + event["event_id"])

    assert response.status == 200
    assert len(deadlines) == 3 and len(set(deadlines)) == 1


def test_soc_crash_core_response_size_gate_remains_fail_closed():
    oversized = "x" * (2 * 1024 * 1024)
    with pytest.raises(research_timeseries.ResearchQueryError) as error:
        QueryGate().run("evidence-core", False,
            lambda deadline: {"data": {"records": [oversized]}})
    assert error.value.code == "response_too_large" and error.value.status == 413


def test_soc_crash_core_realistic_six_module_resource_benchmark(tmp_path, caplog):
    api, _, _ = environment(tmp_path)
    stack = snapshot("2026-09-10T00:00:00+00:00", {
        number: f"SERIAL-M{number}" for number in range(1, 7)})
    write_jsonl(api.paths.position_history, [stack.to_dict()])
    api = GuardianResearchApi(api.paths, cursor_secret=b"test")
    path = api.paths.cell_history / "2026-09-11.jsonl"
    start = datetime(2026, 9, 11, 9, 40, tzinfo=timezone.utc).timestamp()
    rows = []
    for minute in range(61):
        for module in range(1, 7):
            soc = (70 if minute <= 22 else 68 if minute == 23 else 66
                   if module == 4 else 60 + module)
            rows.append({"schema_version": 1, "timestamp": start + minute * 60,
                "module": module, "module_serial": f"SERIAL-M{module}",
                "soc_percent": soc, "current_a": -2.0 if module == 4 else -1.0,
                "voltages_mv": [3290 + module + cell for cell in range(15)],
                "temperatures_c": [20 + module / 10 + cell / 100
                                   for cell in range(15)]})
    write_jsonl(path, rows)
    build_index(path, timestamp_field="timestamp", iso_timestamp=False,
                block_records=12)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:40:00Z&to=2026-09-11T10:40:00Z").body["data"]["events"][0]

    tracemalloc.start()
    started = time.perf_counter()
    with caplog.at_level(logging.INFO, logger="guardian_battery.research"):
        response = get(api, "evidence-core?event_id=" + event["event_id"] + "&profile=true")
    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    response_bytes = len(json.dumps(response.body, ensure_ascii=False,
                                    separators=(",", ":")).encode())
    profile = json.loads(next(record.message.removeprefix("RESEARCH_CORE_PROFILE ")
        for record in caplog.records
        if record.message.startswith("RESEARCH_CORE_PROFILE ")))

    assert response.status == 200
    assert elapsed < 5
    assert response_bytes < 500 * 1024
    assert profile["counts"]["cell_history_scans"] == 2
    assert profile["counts"]["rs485_scans"] == 1
    assert profile["counts"]["target_records"] == 43
    assert profile["counts"]["peer_records"] == 65
    assert profile["stages"]["target_core_read"]["reader"][
        "full_json_decode_count"] == 44
    assert profile["stages"]["peer_core_read"]["reader"][
        "full_json_decode_count"] == 70
    print(json.dumps({"core_benchmark_wall_seconds": elapsed,
        "target_records": profile["counts"]["target_records"],
        "peer_records": profile["counts"]["peer_records"],
        "cell_history_scans": profile["counts"]["cell_history_scans"],
        "rs485_scans": profile["counts"]["rs485_scans"],
        "raw_bytes": (profile["stages"]["target_core_read"]["bytes_read"]
                      + profile["stages"]["peer_core_read"]["bytes_read"]),
        "fully_decoded_records": 114, "peak_memory_bytes": peak_bytes,
        "response_bytes": response_bytes}, sort_keys=True))


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


def test_package_splits_target_and_peer_windows_without_changing_evidence(
        tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    event = get(api, "events/soc-crashes?physical_serial=SERIAL-M4"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z").body["data"]["events"][0]
    target_start = "2026-09-10T10:02:00+00:00"
    target_end = "2026-09-11T10:36:00+00:00"
    peer_start = "2026-09-11T09:57:00+00:00"
    peer_end = "2026-09-11T10:11:00+00:00"
    legacy = api.series.evidence_by_serial(
        ["SERIAL-M4", "SERIAL-M5", "SERIAL-M6"], target_start, target_end)
    expected_target = legacy["records"]["SERIAL-M4"]
    expected_peers = {serial: [row for row in legacy["records"][serial]
        if peer_start <= row["timestamp"] <= peer_end]
        for serial in ("SERIAL-M5", "SERIAL-M6")}
    calls = []
    original = api.series.evidence_by_serial

    def tracked(serials, timestamp_from, timestamp_to, **kwargs):
        calls.append((tuple(serials), timestamp_from, timestamp_to))
        return original(serials, timestamp_from, timestamp_to, **kwargs)

    monkeypatch.setattr(api.series, "evidence_by_serial", tracked)
    response = get(api, "evidence-package?event_id=" + event["event_id"])

    assert response.status == 200
    assert calls == [(("SERIAL-M4",), target_start, target_end),
                     (("SERIAL-M5", "SERIAL-M6"), peer_start, peer_end)]
    assert response.body["data"]["cell_evidence"]["records"] == expected_target
    peers = {row["physical_serial"]: row for row in
             response.body["data"]["peer_evidence"]["modules"]}
    assert {serial: row["records"] for serial, row in peers.items()} == expected_peers
    for row in peers.values():
        sample = row["records"][0]
        assert {"physical_serial", "position_at_time", "soc", "module_current_a",
                "module_voltage_v", "cell_temperatures_c", "derived"} <= set(sample)
        assert {"minimum_cell_voltage_mv", "cell_spread_mv", "lowest_cell"} <= set(
            sample["derived"])


def test_package_peer_read_excludes_records_outside_immediate_window(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    outside = {**next(row for row in rows if row["module_serial"] == "SERIAL-M5"),
        "timestamp": datetime(2026, 9, 11, 9, 30, tzinfo=timezone.utc).timestamp()}
    write_jsonl(path, [outside, *rows])

    _, response = package_for_crash(api)

    peer = next(row for row in response.body["data"]["peer_evidence"]["modules"]
                if row["physical_serial"] == "SERIAL-M5")
    assert all(row["timestamp"] >= "2026-09-11T09:57:00+00:00"
               for row in peer["records"])
    assert len(peer["records"]) == 1


def test_package_uses_event_time_peers_not_later_topology(tmp_path):
    api, _, _ = environment(tmp_path)
    positions = api.paths.position_history
    first = snapshot("2026-09-10T00:00:00+00:00", {4: "SERIAL-M4", 5: "SERIAL-M5"})
    after = snapshot("2026-09-12T00:00:00+00:00", {2: "SERIAL-M4", 6: "SERIAL-M6"})
    write_jsonl(positions, [first.to_dict(), after.to_dict()])

    _, response = package_for_crash(api)

    peers = response.body["data"]["peer_evidence"]["modules"]
    assert {(row["physical_serial"], row["position_at_event"]) for row in peers} == {
        ("SERIAL-M5", 5)}


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


def test_chunk_reader_framing_timer_excludes_consumer_time():
    io_profile = {"raw_chunk_reads": 0, "timings_seconds": {
        "raw_chunk_read": 0.0, "deadline_check": 0.0,
        "source_open_range_seek": 0.0, "binary_line_framing": 0.0}}
    iterator = research_timeseries.iter_binary_range_lines(
        io.BytesIO(b"first\nsecond\n"), 0, 13, chunk_size=13,
        io_profile=io_profile)

    assert next(iterator) == b"first\n"
    measured_before_wait = io_profile["timings_seconds"]["binary_line_framing"]
    time.sleep(0.05)
    assert next(iterator) == b"second\n"

    assert io_profile["timings_seconds"]["binary_line_framing"] < 0.01
    assert (io_profile["timings_seconds"]["binary_line_framing"]
            - measured_before_wait) < 0.01


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
