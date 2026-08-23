import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cell_diagnostics import CellDiagnosticStore, CellSample
from config_history import config_id, diagnostic_parameters
from config_ui import DEFAULTS
from diagnostic_aggregates import DiagnosticAggregateStore
from diagnostic_backfill import DiagnosticAggregateBackfill


def raw(timestamp, module=1, serial="SN-A", current=-2.0, soc=50.0, delta=0):
    voltages = [3300.0] * 15
    voltages[0] += delta
    value = {
        "schema_version": 1, "timestamp": timestamp, "module": module,
        "voltages_mv": voltages, "current_a": current, "soc_percent": soc,
        "temperatures_c": [25.0] * 15, "balancing": [False] * 15,
    }
    if serial is not None:
        value["module_serial"] = serial
    return value


def write_day(directory, day, records, extra_lines=()):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.jsonl"
    lines = [json.dumps(item) for item in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def epoch(day, hour=12):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp()


def stores(tmp_path):
    aggregate = DiagnosticAggregateStore(
        tmp_path / "diagnostic_aggregates.json", CellDiagnosticStore.phases
    )
    backfill = DiagnosticAggregateBackfill(
        tmp_path / "cell_history", tmp_path / "position_history.jsonl"
    )
    return aggregate, backfill


def phase_records(store, phase="discharge"):
    return [item for item in store.records.values() if item["phase"] == phase]


def test_empty_aggregates_backfill_multiple_days_and_phases(tmp_path):
    aggregate, backfill = stores(tmp_path)
    write_day(tmp_path / "cell_history", "2026-08-16", [
        raw(epoch("2026-08-16"), current=-2, soc=20),
        raw(epoch("2026-08-16", 13), current=2, soc=90),
    ])
    write_day(tmp_path / "cell_history", "2026-08-17", [
        raw(epoch("2026-08-17"), current=-2, soc=50),
    ])
    report = backfill.run(aggregate, DEFAULTS)
    assert report["files_scanned"] == 2
    assert report["valid_samples"] == 3
    assert {item["day"] for item in aggregate.records.values()} == {"2026-08-16", "2026-08-17"}
    assert {item["phase"] for item in aggregate.records.values()} == {
        "discharge", "low", "charge", "high"
    }


def test_real_upgrade_only_last_day_present_does_not_double_count(tmp_path):
    aggregate, backfill = stores(tmp_path)
    history = tmp_path / "cell_history"
    for offset, day in enumerate(f"2026-08-{value:02d}" for value in range(16, 24)):
        item = raw(epoch(day), delta=offset)
        write_day(history, day, [item])
        if day == "2026-08-23":
            aggregate.add(CellSample(
                item["timestamp"], 1, item["voltages_mv"], item["current_a"],
                item["soc_percent"], item["temperatures_c"], item["balancing"],
                "SN-A", None,
            ), DEFAULTS)
    before_last = next(item["sample_count"] for item in aggregate.records.values()
                       if item["day"] == "2026-08-23")
    report = backfill.run(aggregate, DEFAULTS)
    assert report["files_scanned"] == 8
    assert {item["day"] for item in phase_records(aggregate)} == {
        f"2026-08-{value:02d}" for value in range(16, 24)
    }
    assert all(item["sample_count"] == before_last for item in aggregate.records.values()
               if item["day"] == "2026-08-23")


def test_partial_day_is_rebuilt_from_raw_without_duplicates(tmp_path):
    aggregate, backfill = stores(tmp_path)
    day = "2026-08-20"
    records = [raw(epoch(day, hour), delta=hour) for hour in (10, 11, 12)]
    write_day(tmp_path / "cell_history", day, records)
    last = records[-1]
    aggregate.add(CellSample(last["timestamp"], 1, last["voltages_mv"], -2, 50,
                             last["temperatures_c"], last["balancing"], "SN-A", None), DEFAULTS)
    backfill.run(aggregate, DEFAULTS)
    assert all(item["sample_count"] == 3 for item in phase_records(aggregate))
    backfill.run(aggregate, DEFAULTS)
    assert all(item["sample_count"] == 3 for item in phase_records(aggregate))


def test_idempotent_restart_skips_unchanged_files_without_opening_them(tmp_path, monkeypatch):
    aggregate, backfill = stores(tmp_path)
    path = write_day(tmp_path / "cell_history", "2026-08-16", [raw(epoch("2026-08-16"))])
    backfill.run(aggregate, DEFAULTS)
    reloaded = DiagnosticAggregateStore(aggregate.path, CellDiagnosticStore.phases)
    opened = []
    original = Path.open

    def counted(self, *args, **kwargs):
        if self == path:
            opened.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    report = backfill.run(reloaded, DEFAULTS)
    assert report["files_skipped"] == 1
    assert report["files_scanned"] == 0
    assert opened == []


def test_multiple_modules_and_physical_swap_stay_separate(tmp_path):
    aggregate, backfill = stores(tmp_path)
    write_day(tmp_path / "cell_history", "2026-08-16", [
        raw(epoch("2026-08-16", 10), module=1, serial="SN-X"),
        raw(epoch("2026-08-16", 11), module=2, serial="SN-Y"),
        raw(epoch("2026-08-16", 12), module=1, serial="SN-Y"),
    ])
    backfill.run(aggregate, DEFAULTS)
    assert {item["physical_module_serial"] for item in aggregate.records.values()} == {"SN-X", "SN-Y"}
    assert {item["module_position"] for item in aggregate.for_identity(1, "SN-X")} == {1}
    assert {item["module_position"] for item in aggregate.for_identity(2, "SN-Y")} == {2}


def test_position_history_resolves_missing_serial_and_unknown_fails_closed(tmp_path):
    aggregate, backfill = stores(tmp_path)
    snapshot_id = "PHS-" + str(uuid.uuid4())
    snapshot = {
        "schema_version": 1, "position_history_id": snapshot_id,
        "effective_at": "2026-08-17T00:00:00+00:00",
        "created_at": "2026-08-17T00:00:00+00:00",
        "maintenance_event_id": "MEV-" + str(uuid.uuid4()),
        "positions": {str(value): "SN-X" if value == 1 else None for value in range(1, 7)},
    }
    (tmp_path / "position_history.jsonl").write_text(json.dumps(snapshot) + "\n")
    write_day(tmp_path / "cell_history", "2026-08-16", [raw(epoch("2026-08-16"), serial=None)])
    write_day(tmp_path / "cell_history", "2026-08-18", [raw(epoch("2026-08-18"), serial=None)])
    report = backfill.run(aggregate, DEFAULTS)
    assert report["identity_unknown"] == 1
    assert {item["physical_module_serial"] for item in aggregate.records.values()} == {"SN-X"}
    assert all(item["identity_status"] == "documented" for item in aggregate.records.values())


def test_config_id_invalid_line_raw_immutability_and_live_continuation(tmp_path):
    aggregate, backfill = stores(tmp_path)
    path = write_day(tmp_path / "cell_history", "2026-08-16",
                     [raw(epoch("2026-08-16"), delta=4)], extra_lines=("{broken",))
    original = path.read_bytes()
    report = backfill.run(aggregate, DEFAULTS)
    expected = config_id(diagnostic_parameters(DEFAULTS))
    assert report["invalid_lines"] == 1
    assert {item["config_id"] for item in aggregate.records.values()} == {expected}
    assert path.read_bytes() == original

    live = CellSample(epoch("2026-08-17"), 1, [3300] * 15, -2, 50,
                      [25] * 15, [False] * 15, "SN-A", None)
    assert aggregate.add(live, DEFAULTS)
    assert any(item["day"] == "2026-08-17" for item in aggregate.records.values())


def test_grown_file_is_rescanned_once_and_adds_only_new_raw_state(tmp_path):
    aggregate, backfill = stores(tmp_path)
    path = write_day(tmp_path / "cell_history", "2026-08-16", [raw(epoch("2026-08-16", 10))])
    backfill.run(aggregate, DEFAULTS)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(raw(epoch("2026-08-16", 11))) + "\n")
    report = backfill.run(aggregate, DEFAULTS)
    assert report["files_scanned"] == 1
    assert all(item["sample_count"] == 2 for item in phase_records(aggregate))
    assert backfill.run(aggregate, DEFAULTS)["files_skipped"] == 1


def test_config_change_creates_separate_provenance_without_destroying_old_aggregates(tmp_path):
    aggregate, backfill = stores(tmp_path)
    write_day(tmp_path / "cell_history", "2026-08-16", [raw(epoch("2026-08-16"))])
    backfill.run(aggregate, DEFAULTS)
    first_id = config_id(diagnostic_parameters(DEFAULTS))
    changed = {**DEFAULTS, "cell_diag_low_soc_percent": 25}
    second_id = config_id(diagnostic_parameters(changed))
    report = backfill.run(aggregate, changed)
    assert report["files_scanned"] == 1
    assert {item["config_id"] for item in aggregate.records.values()} == {first_id, second_id}
    assert backfill.run(aggregate, changed)["files_skipped"] == 1


def test_missing_covered_aggregate_forces_source_repair(tmp_path):
    aggregate, backfill = stores(tmp_path)
    write_day(tmp_path / "cell_history", "2026-08-16", [raw(epoch("2026-08-16"))])
    backfill.run(aggregate, DEFAULTS)
    removed = next(iter(aggregate.records))
    del aggregate.records[removed]
    report = backfill.run(aggregate, DEFAULTS)
    assert report["files_scanned"] == 1
    assert removed in aggregate.records


def test_new_position_history_revisits_previously_unknown_identity(tmp_path):
    aggregate, backfill = stores(tmp_path)
    write_day(tmp_path / "cell_history", "2026-08-18", [
        raw(epoch("2026-08-18"), serial=None)
    ])
    assert backfill.run(aggregate, DEFAULTS)["identity_unknown"] == 1
    assert not aggregate.records

    snapshot = {
        "schema_version": 1, "position_history_id": "PHS-" + str(uuid.uuid4()),
        "effective_at": "2026-08-17T00:00:00+00:00",
        "created_at": "2026-08-18T13:00:00+00:00",
        "maintenance_event_id": "MEV-" + str(uuid.uuid4()),
        "positions": {str(value): "SN-LATE" if value == 1 else None for value in range(1, 7)},
    }
    (tmp_path / "position_history.jsonl").write_text(json.dumps(snapshot) + "\n")
    report = backfill.run(aggregate, DEFAULTS)
    assert report["files_scanned"] == 1
    assert {item["physical_module_serial"] for item in aggregate.records.values()} == {"SN-LATE"}
