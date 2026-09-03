import fcntl
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import daily_diagnostics as daily
from bms_management_evidence import EvidenceParameters


SERIAL = "SYNTHETIC-MODULE-0001"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n"
                            for item in records), encoding="utf-8")


def frame(timestamp, command, *, adr=6, serial=SERIAL, decoded=None, info_raw=""):
    return {"record_type": "frame", "direction": "response",
            "paired_command": command, "timestamp": timestamp, "adr": adr,
            "checksum_valid": True, "frame_complete": True,
            "request_matched": True, "physical_serial": serial,
            "decoded": decoded, "info_raw": info_raw,
            "source_frame_reference": f"ref-{timestamp}"}


def management(timestamp, dcl, *, ccl=25, serial=SERIAL, adr=6):
    return frame(timestamp, 0x92, serial=serial, adr=adr, decoded={
        "discharge_current_limit_a": dcl, "discharge_enable": True,
        "charge_current_limit_a": ccl, "charge_enable": True,
        "charge_voltage_limit_v": 53.25, "discharge_voltage_limit_v": 45.0})


def cell(timestamp, *, serial=SERIAL, module=5, c8=3000, current=-2):
    values = [3300] * 15
    values[7] = c8
    return {"timestamp": timestamp, "module": module, "module_serial": serial,
            "soc_percent": 30, "current_a": current, "voltages_mv": values,
            "temperatures_c": [30] * 15, "balancing": [False] * 15}


def position(timestamp, serial=SERIAL, module=5):
    positions = {str(value): None for value in range(1, 7)}
    positions[str(module)] = serial
    return {"schema_version": 1,
            "position_history_id": "PHS-" + str(uuid.uuid4()),
            "effective_at": timestamp, "created_at": timestamp,
            "maintenance_event_id": "MEV-" + str(uuid.uuid4()),
            "positions": positions}


def source_set(tmp_path, rs485, cells=(), positions=(), configs=()):
    roots = {name: tmp_path / name for name in ("rs485", "cells")}
    for root in roots.values():
        root.mkdir()
    write_jsonl(roots["rs485"] / "raw.jsonl", rs485)
    write_jsonl(roots["cells"] / "odd-name.jsonl", cells)
    position_path = tmp_path / "positions.jsonl"
    config_path = tmp_path / "configs.jsonl"
    if positions:
        write_jsonl(position_path, positions)
    if configs:
        write_jsonl(config_path, configs)
    return daily.DailyDiagnosticSources(
        cell_history_root=roots["cells"], rs485_history_root=roots["rs485"],
        position_history_path=position_path, config_history_path=config_path)


def run(tmp_path, sources, day="2026-08-31", **kwargs):
    return daily.run_daily_diagnostic(day, sources, tmp_path / "output", **kwargs)


def berlin_timestamp(day="2026-08-31", hour=12, minute=0):
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ZoneInfo("Europe/Berlin")).timestamp()


def test_import_contract_contains_no_production_paths_or_runtime_dependencies():
    source = Path(daily.__file__).read_text()
    assert "/share" not in source and "/config" not in source
    for forbidden in ("import main", "serial.write", "mqtt", "Console", "Hycube"):
        assert forbidden not in source


def test_cell_risk_daily_component_is_compact_failure_isolated_and_uses_lookback(tmp_path):
    timestamp = berlin_timestamp()
    cells = [cell(timestamp + index, current=-2, c8=3200) for index in range(300)]
    sources = source_set(tmp_path, [management(timestamp, -25)], cells,
                         [position(datetime.fromtimestamp(timestamp - 3600,
                                                         timezone.utc).isoformat())])
    result = run(tmp_path, sources)
    component = result["components"]["cell_risk"]
    assert component["status"] == "complete" and component["metrics"]["cell_count"] == 15
    aggregate_path = tmp_path / "output/aggregates/cell_risk/2026-08-31.json"
    aggregate = json.loads(aggregate_path.read_text())
    assert len(aggregate["cells"]) == 15 and len(aggregate["top10"]) == 10
    assert aggregate["top10"][0]["physical_serial"] == SERIAL
    assert aggregate["top10"][0]["current_position"] == 5
    assert not list((tmp_path / "output/aggregates/cell_risk").rglob("*.jsonl"))
    assert all("voltages_mv" not in row for row in aggregate["cells"])


def test_cell_risk_no_history_is_not_a_daily_failure(tmp_path):
    timestamp = berlin_timestamp()
    result = run(tmp_path, source_set(tmp_path, [management(timestamp, -25)]))
    assert result["overall_status"] == "partial"
    assert result["components"]["cell_risk"]["status"] == "complete"
    assert result["components"]["cell_risk"]["warnings"] == [
        "no_qualifying_cell_risk_samples"]


def test_cell_risk_exception_isolated_from_existing_daily_component(tmp_path, monkeypatch):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)], [cell(timestamp)])
    monkeypatch.setattr(daily, "analyze_cell_risk",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("risk failed")))
    result = run(tmp_path, sources)
    assert result["components"]["bms_management"]["status"] == "partial"
    assert result["components"]["cell_risk"]["status"] == "failed"
    assert result["components"]["cell_risk"]["errors"][0]["type"] == "RuntimeError"
    assert result["persisted"] is True


@pytest.mark.parametrize("day,hours", [("2026-02-01", 24),
                                        ("2026-03-29", 23),
                                        ("2026-10-25", 25)])
def test_guardian_day_dst_duration(day, hours):
    value = daily.guardian_day(day)
    assert value.duration_seconds == hours * 3600
    assert value.start.tzinfo == ZoneInfo("Europe/Berlin")


def test_day_start_inclusive_day_end_exclusive_and_outside_ignored(tmp_path):
    bounds = daily.guardian_day("2026-08-31")
    sources = source_set(tmp_path, [management(bounds.start.timestamp(), -25),
                                    management(bounds.end.timestamp(), 0)])
    result = run(tmp_path, sources)
    source = result["sources"]["rs485"]
    assert source["records_used"] == 1
    assert source["records_ignored_outside_day"] == 1


def test_timezone_aware_text_timestamp_and_naive_timestamp_is_invalid(tmp_path):
    instant = berlin_timestamp()
    aware = datetime.fromtimestamp(instant, timezone.utc).isoformat()
    sources = source_set(tmp_path, [management(aware, -25),
                                    management("2026-08-31T12:00:00", 0)])
    result = run(tmp_path, sources)
    assert result["sources"]["rs485"]["records_used"] == 1
    assert result["sources"]["rs485"]["records_invalid"] == 1
    assert result["overall_status"] == "partial"


def test_rs485_local_day_crosses_two_utc_named_files(tmp_path):
    root = tmp_path / "rs485"
    root.mkdir()
    first = datetime(2026, 8, 30, 22, 30, tzinfo=timezone.utc).timestamp()
    last = datetime(2026, 8, 31, 21, 30, tzinfo=timezone.utc).timestamp()
    write_jsonl(root / "2026-08-30.jsonl", [management(first, -25)])
    write_jsonl(root / "2026-08-31.jsonl", [management(last, 0)])
    cells = tmp_path / "cells"
    cells.mkdir()
    result = run(tmp_path, daily.DailyDiagnosticSources(
        rs485_history_root=root, cell_history_root=cells))
    assert result["sources"]["rs485"]["records_used"] == 2
    assert len(result["sources"]["rs485"]["files"]) == 2


def test_deterministic_id_idempotent_events_and_result_revision(tmp_path):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path,
                         [management(timestamp, -25), management(timestamp + 10, 0)],
                         [cell(timestamp + 5)],
                         [position(datetime.fromtimestamp(timestamp - 3600,
                                                         timezone.utc).isoformat())])
    first = run(tmp_path, sources)
    stored_path = Path(first["result_path"])
    stored_bytes = stored_path.read_bytes()
    event_path = tmp_path / "output/events/bms_management/2026-08-31.jsonl"
    event_bytes = event_path.read_bytes()
    second = run(tmp_path, sources)
    assert second["daily_result_id"] == first["daily_result_id"]
    assert second["input_fingerprint"] == first["input_fingerprint"]
    assert Path(second["result_path"]) == stored_path
    assert stored_path.read_bytes() == stored_bytes
    assert event_path.read_bytes() == event_bytes
    assert second["components"]["bms_management"]["events"]["appended"] == 0


def test_relevant_change_creates_revision_and_preserves_old_result(tmp_path):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)])
    first = run(tmp_path, sources)
    rs_path = Path(sources.rs485_history_root) / "raw.jsonl"
    with rs_path.open("a") as handle:
        handle.write(json.dumps(management(timestamp + 10, 0)) + "\n")
    second = run(tmp_path, sources)
    assert second["daily_result_id"] != first["daily_result_id"]
    assert Path(first["result_path"]).exists() and Path(second["result_path"]).exists()
    index = json.loads(Path(second["index_path"]).read_text())
    assert index["result"] == Path(second["result_path"]).name


def test_outside_day_change_preserves_semantic_fingerprint(tmp_path):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)])
    first = run(tmp_path, sources)
    rs_path = Path(sources.rs485_history_root) / "raw.jsonl"
    with rs_path.open("a") as handle:
        handle.write(json.dumps(management(timestamp + 2 * 86400, 0)) + "\n")
    second = run(tmp_path, sources)
    assert second["input_fingerprint"] == first["input_fingerprint"]
    assert (second["sources"]["rs485"]["files"][0]["sha256"] !=
            first["sources"]["rs485"]["files"][0]["sha256"])


def test_corrupt_line_is_counted_and_result_is_partial(tmp_path):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)])
    path = Path(sources.rs485_history_root) / "raw.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"truncated"')
    result = run(tmp_path, sources)
    assert result["overall_status"] == "partial"
    assert result["sources"]["rs485"]["records_invalid"] == 1
    assert "invalid_records:rs485" in result["components"]["bms_management"]["warnings"]


@pytest.mark.parametrize("missing", ["cell", "rs485"])
def test_partial_source_is_not_reported_as_zero_evidence(tmp_path, missing):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)], [cell(timestamp)])
    if missing == "cell":
        sources = daily.DailyDiagnosticSources(rs485_history_root=sources.rs485_history_root,
                                               cell_history_root=tmp_path / "absent-cell")
    else:
        sources = daily.DailyDiagnosticSources(cell_history_root=sources.cell_history_root,
                                               rs485_history_root=tmp_path / "absent-rs485")
    result = run(tmp_path, sources)
    assert result["overall_status"] == "partial"
    assert result["sources"][missing]["quality"] == "missing"
    assert result["components"]["bms_management"]["quality"] == "limited"


def test_no_usable_component_input_is_failed_and_not_persisted(tmp_path):
    sources = daily.DailyDiagnosticSources(tmp_path / "missing-cell",
                                           tmp_path / "missing-rs485")
    result = run(tmp_path, sources)
    assert result["overall_status"] == "failed" and result["persisted"] is False
    assert not (tmp_path / "output/daily").exists()


def test_component_exception_isolated_and_existing_index_untouched(tmp_path, monkeypatch):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)])
    good = run(tmp_path, sources)
    index_path = Path(good["index_path"])
    before = index_path.read_bytes()
    monkeypatch.setattr(daily.BmsManagementEvidenceAnalyzer, "analyze",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = run(tmp_path, sources)
    assert failed["overall_status"] == "failed" and failed["persisted"] is False
    assert failed["errors"][0]["type"] == "RuntimeError"
    assert index_path.read_bytes() == before


def test_source_change_during_read_aborts_without_index_switch(tmp_path, monkeypatch):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)])
    target = Path(sources.rs485_history_root) / "raw.jsonl"
    original = daily._stat
    calls = 0
    def changing(path):
        nonlocal calls
        value = original(path)
        if path == target:
            calls += 1
            if calls == 2:
                return value[0] + 1, value[1], value[2]
        return value
    monkeypatch.setattr(daily, "_stat", changing)
    with pytest.raises(daily.SourceChangedError):
        run(tmp_path, sources)
    assert not (tmp_path / "output/daily").exists()


def test_lock_contention_is_reported(tmp_path):
    output = tmp_path / "output"
    lock_path = output / "locks/daily_job.lock"
    lock_path.parent.mkdir(parents=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(daily.DailyDiagnosticBusyError):
            run(tmp_path, daily.DailyDiagnosticSources(), lock_timeout_seconds=0)


def test_historical_position_resolver_uses_position_at_event_time(tmp_path):
    timestamp = berlin_timestamp()
    before = position(datetime.fromtimestamp(timestamp - 100, timezone.utc).isoformat(),
                      module=4)
    after = position(datetime.fromtimestamp(timestamp + 100, timezone.utc).isoformat(),
                     module=5)
    sources = source_set(tmp_path,
                         [management(timestamp - 10, -25), management(timestamp, 0)],
                         [cell(timestamp - 2)], [before, after])
    result = run(tmp_path, sources)
    event_path = tmp_path / "output/events/bms_management/2026-08-31.jsonl"
    event = json.loads(event_path.read_text().splitlines()[0])
    assert event["physical_serial"] == SERIAL
    assert event["position_at_time"] == 4
    assert event["position_history_id"] == before["position_history_id"]
    assert result["components"]["bms_management"]["coverage"]["physical_serials"] == [SERIAL]


def test_missing_position_history_keeps_physical_serial_evidence(tmp_path):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path,
                         [management(timestamp - 10, -25), management(timestamp, 0)],
                         [cell(timestamp - 2)])
    result = run(tmp_path, sources)
    event = json.loads((tmp_path / "output/events/bms_management/2026-08-31.jsonl").read_text())
    assert event["physical_serial"] == SERIAL and event["position_at_time"] is None
    assert result["overall_status"] == "partial"


def test_synthetic_ccl_reduction_and_aggregate_persistence(tmp_path):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path,
                         [management(timestamp, -25, ccl=25),
                          management(timestamp + 10, -25, ccl=10),
                          management(timestamp + 20, -25, ccl=25)],
                         [cell(timestamp + 5, current=8)])
    result = run(tmp_path, sources)
    aggregate = json.loads((tmp_path /
        "output/aggregates/bms_management/2026-08-31.json").read_text())["aggregates"][0]
    assert aggregate["ccl_reduction_event_count"] == 1
    assert aggregate["ccl_min_a"] == 10
    assert result["components"]["bms_management"]["events"]["count"] == 1


def test_daily_adapter_preserves_reference_seven_event_semantics(tmp_path):
    spec = json.loads((Path(__file__).parent /
        "fixtures/bms_management_reference_v1.json").read_text())
    base = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc).timestamp()
    rs485, cells = [], []
    for index, offset in enumerate(spec["event_offsets_seconds"]):
        event_time = base + offset
        rs485.append(management(event_time - 40, -25))
        rs485.extend([frame(event_time - 5, 0x44, info_raw="1106"),
                      frame(event_time - 4, 0x44, info_raw="0006"),
                      management(event_time, 0)])
        recovery = event_time + spec["zero_durations_seconds"][index]
        poll = event_time + 60
        while poll < recovery:
            rs485.append(management(poll, 0))
            poll += 60
        rs485.append(management(recovery, -25))
        cells.append(cell(event_time - 10, c8=spec["cell_8_mv"][index],
                          current=spec["current_before_a"][index]))
    result = run(tmp_path, source_set(tmp_path, rs485, cells))
    events = [json.loads(line) for line in (tmp_path /
        "output/events/bms_management/2026-08-31.jsonl").read_text().splitlines()]
    aggregate = json.loads((tmp_path /
        "output/aggregates/bms_management/2026-08-31.json").read_text())["aggregates"][0]
    assert len(events) == aggregate["dcl_zero_count"] == 7
    assert sum(item["cell_context"]["min_cell_number"] == 8 for item in events) == 7
    assert sum(item["transition_0x44"]["old_hex"] == "11" and
               item["transition_0x44"]["new_hex"] == "00" for item in events) == 7
    assert result["components"]["bms_management"]["events"]["count"] == 7


def test_daily_adapter_redecodes_historical_0x93_identity(tmp_path):
    timestamp = berlin_timestamp()
    serial = "ABCDEFGHIJKLMNOP"
    identity = frame(timestamp - 20, 0x93, serial=None,
                     info_raw=(b"\x06" + serial.encode()).hex())
    identity["decoder_supported"] = False
    identity["decoded"] = None
    sources = source_set(tmp_path, [identity,
                                    management(timestamp - 10, -25, serial=None),
                                    management(timestamp, 0, serial=None)])
    run(tmp_path, sources)
    event = json.loads((tmp_path /
        "output/events/bms_management/2026-08-31.jsonl").read_text())
    assert event["physical_serial"] == serial


def test_parameters_and_config_projection_change_fingerprint(tmp_path):
    timestamp = berlin_timestamp()
    config_time = datetime.fromtimestamp(timestamp - 3600, timezone.utc).isoformat()
    sources = source_set(tmp_path, [management(timestamp, -25)], configs=[
        {"schema_version": 1, "timestamp": config_time, "config_id": "one",
         "parameters": {"module_count": 5}}])
    first = run(tmp_path, sources)
    second = daily.run_daily_diagnostic(
        "2026-08-31", sources, tmp_path / "other-output",
        bms_parameters=EvidenceParameters(peer_cycle_seconds=11))
    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert first["provenance"]["config_history_projection"][0]["config_id"] == "one"


def test_future_history_records_do_not_change_old_day_but_relevant_records_do(tmp_path):
    timestamp = berlin_timestamp()
    before = datetime.fromtimestamp(timestamp - 3600, timezone.utc).isoformat()
    sources = source_set(tmp_path, [management(timestamp, -25)],
                         positions=[position(before)], configs=[
                             {"schema_version": 1, "timestamp": before,
                              "config_id": "before", "parameters": {}}])
    maintenance = tmp_path / "maintenance.jsonl"
    sources = daily.DailyDiagnosticSources(
        sources.cell_history_root, sources.rs485_history_root,
        sources.position_history_path, sources.config_history_path, maintenance)
    first = daily.probe_daily_inputs("2026-08-31", sources).input_fingerprint
    future = "2026-09-02T10:00:00+00:00"
    with Path(sources.position_history_path).open("a") as handle:
        handle.write(json.dumps(position(future, serial="FUTURE")) + "\n")
    with Path(sources.config_history_path).open("a") as handle:
        handle.write(json.dumps({"schema_version": 1, "timestamp": future,
                                 "config_id": "future", "parameters": {}}) + "\n")
    write_jsonl(maintenance, [{"occurred_at": future, "event": "future"}])
    assert daily.probe_daily_inputs("2026-08-31", sources).input_fingerprint == first

    relevant = datetime.fromtimestamp(timestamp + 60, timezone.utc).isoformat()
    with Path(sources.position_history_path).open("a") as handle:
        handle.write(json.dumps(position(relevant, serial="RELEVANT")) + "\n")
    second = daily.probe_daily_inputs("2026-08-31", sources).input_fingerprint
    assert second != first
    with Path(sources.config_history_path).open("a") as handle:
        handle.write(json.dumps({"schema_version": 1, "timestamp": relevant,
                                 "config_id": "relevant", "parameters": {}}) + "\n")
    third = daily.probe_daily_inputs("2026-08-31", sources).input_fingerprint
    assert third != second
    with maintenance.open("a") as handle:
        handle.write(json.dumps({"occurred_at": relevant,
                                 "event": "late-documentation"}) + "\n")
    assert daily.probe_daily_inputs("2026-08-31", sources).input_fingerprint != third


@pytest.mark.parametrize("failure_stage", ["event", "aggregate", "result", "index"])
def test_persistence_failures_keep_old_index_and_rerun_is_idempotent(
        tmp_path, monkeypatch, failure_stage):
    timestamp = berlin_timestamp()
    sources = source_set(tmp_path, [management(timestamp, -25)])
    original = run(tmp_path, sources)
    index_path = Path(original["index_path"])
    old_index = index_path.read_bytes()
    with (Path(sources.rs485_history_root) / "raw.jsonl").open("a") as handle:
        handle.write(json.dumps(management(timestamp + 10, 0)) + "\n")

    append = daily.BmsManagementEvidenceStore.append_events
    aggregate = daily.BmsManagementEvidenceStore.save_daily_aggregates
    atomic = daily._atomic_json
    if failure_stage == "event":
        monkeypatch.setattr(daily.BmsManagementEvidenceStore, "append_events",
                            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("event full")))
    elif failure_stage == "aggregate":
        monkeypatch.setattr(daily.BmsManagementEvidenceStore, "save_daily_aggregates",
                            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("aggregate full")))
    else:
        def fail_atomic(path, payload):
            if ((failure_stage == "index" and path.name == "index.json")
                    or (failure_stage == "result" and path.name != "index.json")):
                raise OSError(f"{failure_stage} full")
            atomic(path, payload)
        monkeypatch.setattr(daily, "_atomic_json", fail_atomic)
    with pytest.raises(OSError, match="full"):
        run(tmp_path, sources)
    assert index_path.read_bytes() == old_index

    monkeypatch.setattr(daily.BmsManagementEvidenceStore, "append_events", append)
    monkeypatch.setattr(daily.BmsManagementEvidenceStore, "save_daily_aggregates", aggregate)
    monkeypatch.setattr(daily, "_atomic_json", atomic)
    repaired = run(tmp_path, sources)
    assert repaired["input_fingerprint"] != original["input_fingerprint"]
    event_path = tmp_path / "output/events/bms_management/2026-08-31.jsonl"
    assert len(event_path.read_text().splitlines()) == 1


def test_daily_schema_has_required_contract_and_atomic_index(tmp_path):
    timestamp = berlin_timestamp()
    result = run(tmp_path, source_set(tmp_path, [management(timestamp, -25)]))
    assert {"schema_version", "diagnostic_date", "timezone", "day_start", "day_end",
            "day_duration_seconds", "daily_result_id", "input_fingerprint",
            "overall_status", "sources", "components", "trend_inputs",
            "provenance"} <= result.keys()
    component = result["components"]["bms_management"]
    assert {"component_name", "component_version", "schema_version", "status",
            "coverage", "metrics", "events", "quality", "warnings", "errors",
            "provenance"} <= component.keys()
    index = json.loads(Path(result["index_path"]).read_text())
    assert index["daily_result_id"] == result["daily_result_id"]
    assert not list((tmp_path / "output").rglob("*.tmp"))
