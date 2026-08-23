import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from cell_diagnostics import CellDiagnosticStore, CellSample
from config_ui import DEFAULTS
from current_condition_backfill import CurrentConditionBackfill


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def raw(timestamp, module=1, serial="SN-A", delta=0):
    voltages = [3300.0] * 15
    voltages[0] += delta
    result = {
        "schema_version": 1, "timestamp": timestamp, "module": module,
        "voltages_mv": voltages, "current_a": -2.0, "soc_percent": 50.0,
        "temperatures_c": [25.0] * 15, "balancing": [False] * 15,
    }
    if serial is not None:
        result["module_serial"] = serial
    return result


def write_day(directory, day, records, extra=()):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in records) + "".join(extra))
    return path


def snapshot(path, effective, positions):
    value = {
        "schema_version": 1,
        "position_history_id": "PHS-" + str(uuid.uuid4()),
        "effective_at": effective,
        "created_at": effective,
        "maintenance_event_id": "MEV-" + str(uuid.uuid4()),
        "positions": {str(index): positions.get(index) for index in range(1, 7)},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")
    return value


def setup(tmp_path, maximum=8640):
    store = CellDiagnosticStore(tmp_path / "cell_diagnostics.json", maximum)
    rebuild = CurrentConditionBackfill(
        tmp_path / "cell_history", tmp_path / "position_history.jsonl"
    )
    return store, rebuild


def test_complete_rebuild_multiple_modules_and_analysis_matches_direct_store(tmp_path):
    store, rebuild = setup(tmp_path)
    records = [raw(epoch("2026-08-20T10:00:00+00:00"), 1, delta=10),
               raw(epoch("2026-08-20T10:01:00+00:00"), 2, "SN-B", 20)]
    write_day(tmp_path / "cell_history", "2026-08-20", records)
    report = rebuild.run(store)
    assert report["samples_merged"] == 2
    assert set(store.samples) == {1, 2}

    direct = CellDiagnosticStore(tmp_path / "direct.json")
    direct.add(CurrentConditionBackfill._sample(records[0], ([], [])))
    rebuilt_result = store.analyse(1, DEFAULTS)
    direct_result = direct.analyse(1, DEFAULTS)
    assert rebuilt_result["status"] == direct_result["status"]
    assert rebuilt_result["confidence"] == direct_result["confidence"]
    assert rebuilt_result["sample_count"] == direct_result["sample_count"]
    assert rebuilt_result["cells"] == direct_result["cells"]


def test_partial_cache_is_merged_without_duplicates_and_is_idempotent(tmp_path):
    store, rebuild = setup(tmp_path)
    records = [raw(epoch("2026-08-20T10:00:00+00:00")),
               raw(epoch("2026-08-20T11:00:00+00:00"))]
    write_day(tmp_path / "cell_history", "2026-08-20", records)
    store.add(CurrentConditionBackfill._sample(records[-1], ([], [])))
    first = rebuild.run(store)
    assert first["samples_merged"] == 1 and first["duplicates"] == 1
    assert len(store.samples[1]) == 2
    reloaded = CellDiagnosticStore(store.path)
    second = rebuild.run(reloaded)
    assert second["files_skipped"] == 1 and second["files_scanned"] == 0
    assert len(reloaded.samples[1]) == 2


def test_corrupt_cache_and_raw_line_rebuild_safely(tmp_path):
    store_path = tmp_path / "cell_diagnostics.json"
    store_path.write_text("{broken")
    store = CellDiagnosticStore(store_path)
    rebuild = CurrentConditionBackfill(tmp_path / "cell_history", tmp_path / "positions.jsonl")
    write_day(tmp_path / "cell_history", "2026-08-20",
              [raw(epoch("2026-08-20T10:00:00+00:00"))], ("{bad\n",))
    report = rebuild.run(store)
    assert report["invalid_lines"] == 1
    assert len(store.samples[1]) == 1
    assert json.loads(store_path.read_text())["samples"]["1"]


def test_unknown_before_first_snapshot_is_skipped_and_later_identity_is_resolved(tmp_path):
    store, rebuild = setup(tmp_path)
    position_path = tmp_path / "position_history.jsonl"
    snapshot(position_path, "2026-08-20T10:13:00+00:00", {1: "SN-X"})
    write_day(tmp_path / "cell_history", "2026-08-19", [
        raw(epoch("2026-08-19T12:00:00+00:00"), serial=None)
    ])
    write_day(tmp_path / "cell_history", "2026-08-21", [
        raw(epoch("2026-08-21T12:00:00+00:00"), serial=None)
    ])
    report = rebuild.run(store)
    assert report["identity_unknown"] == 1
    assert [value["module_serial"] for value in store.samples[1]] == ["SN-X"]


def test_real_upgrade_rebuilds_only_safely_identified_samples_and_reaches_evidence_minimum(tmp_path):
    store, rebuild = setup(tmp_path)
    snapshot(tmp_path / "position_history.jsonl", "2026-08-20T10:13:00+00:00", {4: "SN-M4"})
    for day in range(16, 24):
        records = [
            raw(epoch(f"2026-08-{day:02d}T{hour:02d}:30:00+00:00"), 4, None, hour)
            for hour in range(24)
        ]
        write_day(tmp_path / "cell_history", f"2026-08-{day:02d}", records)
    report = rebuild.run(store)
    expected = (14 + 3 * 24)
    assert report["identity_unknown"] == 4 * 24 + 10
    assert len(store.samples[4]) == expected
    result = store.analyse(4, DEFAULTS)
    assert result["sample_count"] == expected
    assert result["cells"][0]["phases"]["discharge"]["samples"] >= 30


def test_position_swap_separates_identity_at_sample_time(tmp_path):
    store, rebuild = setup(tmp_path)
    position_path = tmp_path / "position_history.jsonl"
    snapshot(position_path, "2026-08-20T10:00:00+00:00", {1: "SN-X", 2: "SN-Y"})
    snapshot(position_path, "2026-08-20T12:00:00+00:00", {1: "SN-Y", 2: "SN-X"})
    write_day(tmp_path / "cell_history", "2026-08-20", [
        raw(epoch("2026-08-20T11:00:00+00:00"), 1, None),
        raw(epoch("2026-08-20T13:00:00+00:00"), 1, None),
    ])
    rebuild.run(store)
    assert [item["module_serial"] for item in store.samples[1]] == ["SN-X", "SN-Y"]
    assert store.analyse(1, DEFAULTS)["sample_count"] == 1


def test_new_position_history_revisits_unknown_raw_without_duplicate_cache_slot(tmp_path):
    store, rebuild = setup(tmp_path)
    timestamp = epoch("2026-08-20T11:00:00+00:00")
    record = raw(timestamp, serial=None)
    write_day(tmp_path / "cell_history", "2026-08-20", [record])
    store.add(CellSample(timestamp, 1, record["voltages_mv"], -2, 50,
                         record["temperatures_c"], record["balancing"], None, None))
    assert rebuild.run(store)["identity_unknown"] == 1
    snapshot(tmp_path / "position_history.jsonl", "2026-08-20T10:00:00+00:00", {1: "SN-X"})
    report = rebuild.run(store)
    assert report["files_scanned"] == 1
    assert len(store.samples[1]) == 1
    assert store.samples[1][0]["module_serial"] == "SN-X"


def test_ring_limit_applies_after_chronological_merge(tmp_path):
    store, rebuild = setup(tmp_path, maximum=3)
    records = [raw(epoch(f"2026-08-20T10:0{minute}:00+00:00"), delta=minute)
               for minute in range(5)]
    write_day(tmp_path / "cell_history", "2026-08-20", records)
    rebuild.run(store)
    assert [item["timestamp"] for item in store.samples[1]] == [
        records[index]["timestamp"] for index in (2, 3, 4)
    ]


def test_grown_file_reads_only_tail_and_replaced_file_is_fully_scanned(tmp_path, monkeypatch):
    store, rebuild = setup(tmp_path)
    path = write_day(tmp_path / "cell_history", "2026-08-20", [
        raw(epoch("2026-08-20T10:00:00+00:00"))
    ])
    rebuild.run(store)
    with path.open("a") as handle:
        handle.write(json.dumps(raw(epoch("2026-08-20T11:00:00+00:00"))) + "\n")
    report = rebuild.run(store)
    assert report["incremental_files"] == 1 and report["lines_seen"] == 1
    assert len(store.samples[1]) == 2

    path.write_text(json.dumps(raw(epoch("2026-08-20T12:00:00+00:00"))) + "\n")
    report = rebuild.run(store)
    assert report["incremental_files"] == 0 and report["lines_seen"] == 1
    assert len(store.samples[1]) == 3


def test_rebuild_does_not_read_or_change_diagnostic_aggregates(tmp_path):
    store, rebuild = setup(tmp_path)
    aggregate = tmp_path / "diagnostic_aggregates.json"
    aggregate.write_text('{"sentinel":true}')
    write_day(tmp_path / "cell_history", "2026-08-20", [
        raw(epoch("2026-08-20T10:00:00+00:00"))
    ])
    rebuild.run(store)
    assert aggregate.read_text() == '{"sentinel":true}'
