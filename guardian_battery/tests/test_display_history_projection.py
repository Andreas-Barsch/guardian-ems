import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from display_history_projection import (
    AGGREGATION_ALGORITHM_VERSION, DISPLAY_PROJECTION_SCHEMA_VERSION,
    DisplayHistoryProjection, DisplayProjectionError, RESOLUTIONS,
    DisplayHistoryProjectionWorker, read_projection,
)


DAY = "2026-09-01"
NOW = datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()


def cell_record(timestamp, module=1, serial="SERIAL-A", soc=50, current=1,
                voltages=None, temperatures=None):
    return {"schema_version": 1, "timestamp": timestamp, "module": module,
            "module_serial": serial, "soc_percent": soc, "current_a": current,
            "voltages_mv": voltages or list(range(3300, 3315)),
            "temperatures_c": temperatures or list(range(20, 35)),
            "balancing": [False] * 15, "physical_groups": {}}


def hycube_record(timestamp, value):
    return {"schema_version": 1, "record_type": "hycube_history_projection",
            "received_at": timestamp, "battery_capacity": value,
            "source_raw_end_offset": 1}


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def builder(tmp_path, cell_rows=(), hycube_rows=(), **kwargs):
    cell = tmp_path / "cell"; hycube = tmp_path / "hycube"; output = tmp_path / "display"
    if cell_rows: write_rows(cell / f"{DAY}.jsonl", cell_rows)
    if hycube_rows: write_rows(hycube / f"{DAY}.jsonl", hycube_rows)
    return DisplayHistoryProjection(cell, hycube, output, clock=lambda: NOW,
                                    **kwargs), output


def by(records, metric, cell=None, serial=None):
    return [item for item in records if item["metric"] == metric
            and (cell is None or item.get("cell_number") == cell)
            and (serial is None or item.get("physical_serial") == serial)]


def test_exact_bucket_contract_and_weighted_values(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    rows = [cell_record(base + offset, soc=value, current=-value)
            for offset, value in ((40, 4), (10, 1), (10, 2), (55, 9))]
    core, output = builder(tmp_path, rows, [
        hycube_record("2026-09-01T00:00:05+00:00", 80),
        hycube_record("2026-09-01T00:00:35+00:00", 70)])
    result = core.build_day(DAY)
    assert result["records"] == 6
    bucket = by(read_projection(output, "1m", DAY), "soc")[0]
    assert bucket["bucket_start"] == "2026-09-01T00:00:00+00:00"
    assert bucket["bucket_end"] == "2026-09-01T00:01:00+00:00"
    assert bucket["first_value"] == 1 and bucket["first_timestamp"].endswith("00:00:10+00:00")
    assert bucket["last_value"] == 9 and bucket["last_timestamp"].endswith("00:00:55+00:00")
    assert bucket["min_value"] == 1 and bucket["min_timestamp"] == bucket["first_timestamp"]
    assert bucket["max_value"] == 9 and bucket["sample_count"] == 4
    assert bucket["mean"] == 4
    assert bucket["display_projection_schema_version"] == DISPLAY_PROJECTION_SCHEMA_VERSION
    assert bucket["aggregation_algorithm_version"] == AGGREGATION_ALGORITHM_VERSION
    assert bucket["derived"] and not bucket["authoritative"]
    assert by(read_projection(output, "1m", DAY), "hycube_soc")[0]["mean"] == 75


def test_all_resolutions_equal_direct_full_resolution_aggregation(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    rows = [cell_record(base + second, soc=(second * 7) % 101,
                        current=(second % 17) - 8,
                        voltages=[3300 + second % 13 + cell for cell in range(15)],
                        temperatures=[20 + second % 5 + cell / 10 for cell in range(15)])
            for second in range(0, 3700, 5)]
    core, output = builder(tmp_path, rows, max_records=73, max_bytes=100000)
    core.build_day(DAY)
    for resolution, seconds in RESOLUTIONS.items():
        records = read_projection(output, resolution, DAY)
        for item in records:
            if item["source"] != "cell_history": continue
            cell = item.get("cell_number")
            values = []
            for order, row in enumerate(rows, 1):
                timestamp = datetime.fromtimestamp(row["timestamp"], timezone.utc).isoformat()
                if not item["bucket_start"] <= timestamp < item["bucket_end"]: continue
                if item["metric"] == "soc": value = row["soc_percent"]
                elif item["metric"] == "current": value = row["current_a"]
                elif item["metric"] == "cell_voltage": value = row["voltages_mv"][cell - 1]
                else: value = row["temperatures_c"][cell - 1]
                values.append((timestamp, order, float(value)))
            ordered = sorted(values, key=lambda value: (value[0], value[1]))
            minimum = min(values, key=lambda value: (value[2], value[0], value[1]))
            maximum = min(values, key=lambda value: (-value[2], value[0], value[1]))
            assert (item["first_timestamp"], item["first_value"]) == (ordered[0][0], ordered[0][2])
            assert (item["last_timestamp"], item["last_value"]) == (ordered[-1][0], ordered[-1][2])
            assert (item["min_timestamp"], item["min_value"]) == (minimum[0], minimum[2])
            assert (item["max_timestamp"], item["max_value"]) == (maximum[0], maximum[2])
            assert item["mean"] == pytest.approx(
                sum(value[2] for value in values) / len(values), rel=0, abs=1e-12)
            assert item["sample_count"] == len(values)


def test_chunk_resume_is_bounded_idempotent_and_atomic(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    rows = [cell_record(base + index * 5, soc=index) for index in range(20)]
    core, output = builder(tmp_path, rows, max_records=3, max_bytes=100000)
    first = core.process_chunk(DAY, finalize=True)
    assert first["records_processed"] == 3 and not first["complete"]
    assert not list(output.glob("*/*.jsonl.gz.building"))
    core = DisplayHistoryProjection(tmp_path / "cell", tmp_path / "hycube", output,
                                    max_records=3, clock=lambda: NOW)
    result = core.build_day(DAY)
    assert result["records"] == 17
    before = {path: path.read_bytes() for path in output.glob("*/*.jsonl.gz")}
    core.build_day(DAY)
    assert {path: path.read_bytes() for path in output.glob("*/*.jsonl.gz")} == before
    assert not list(output.rglob("*.tmp")) and not list(output.rglob("*.building"))


def test_current_day_incremental_update_and_restart_recovery(tmp_path):
    today = "2026-09-02"; base = NOW
    cell = tmp_path / "cell"; output = tmp_path / "display"; path = cell / f"{today}.jsonl"
    write_rows(path, [cell_record(base + 5, soc=10)])
    core = DisplayHistoryProjection(cell, tmp_path / "hycube", output,
                                    max_records=1, clock=lambda: NOW)
    assert core.process_chunk(today)["caught_up"]
    with path.open("a") as handle:
        handle.write(json.dumps(cell_record(base + 10, soc=20)) + "\n")
    restarted = DisplayHistoryProjection(cell, tmp_path / "hycube", output,
                                         max_records=1, clock=lambda: NOW)
    result = restarted.process_chunk(today)
    assert result["records_processed"] == 1 and result["caught_up"]
    bucket = by(read_projection(output, "1m", today), "soc")[0]
    assert bucket["sample_count"] == 2 and bucket["mean"] == 15
    assert restarted.status()["open_days"] == 1


@pytest.mark.parametrize("case", ["single", "duplicate_timestamp", "duplicate_value",
                                   "clock_regression", "out_of_order"])
def test_ordering_edge_cases_are_deterministic(tmp_path, case):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    pairs = {"single": [(10, 4)], "duplicate_timestamp": [(10, 4), (10, 8)],
             "duplicate_value": [(10, 4), (20, 4)],
             "clock_regression": [(40, 4), (10, 8)],
             "out_of_order": [(40, 4), (10, 8), (30, 2)]}[case]
    core, output = builder(tmp_path, [cell_record(base + offset, soc=value)
                                      for offset, value in pairs])
    core.build_day(DAY); first = read_projection(output, "1m", DAY)
    core.build_day(DAY); assert read_projection(output, "1m", DAY) == first
    bucket = by(first, "soc")[0]
    chronological = sorted(enumerate(pairs, 1), key=lambda pair: (pair[1][0], pair[0]))
    assert bucket["first_value"] == chronological[0][1][1]
    assert bucket["last_value"] == chronological[-1][1][1]
    assert bucket["sample_count"] == len(pairs)


def test_day_identity_and_missing_data_boundaries(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    rows = [cell_record(base + 1, module=1, serial="OLD"),
            cell_record(base + 2, module=1, serial="NEW"),
            cell_record(base + 3, module=3, serial=None,
                        voltages=[3300] * 14, temperatures=[20] * 14)]
    core, output = builder(tmp_path, rows)
    core.build_day(DAY)
    records = read_projection(output, "1m", DAY)
    assert by(records, "soc", serial="OLD") and by(records, "soc", serial="NEW")
    unresolved = [item for item in records if item.get("module_position") == 3]
    assert unresolved and all(item["physical_serial"] is None and
                              item["identity_quality"] == "position_only" for item in unresolved)
    assert len(by(unresolved, "cell_voltage")) == 14


def test_day_change_invalid_record_corruption_and_source_invalidation(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    core, output = builder(tmp_path, [cell_record(base + 1)])
    core.build_day(DAY); assert core.status()["complete_days"] == 1
    path = tmp_path / "cell" / f"{DAY}.jsonl"
    with path.open("a") as handle: handle.write(json.dumps(cell_record(base + 2)) + "\n")
    core.refresh_status(); assert core.status()["invalid_days"] == 1
    meta = output / "1m" / f"{DAY}.meta.json"; meta.write_text("{")
    core.refresh_status(); assert core.status()["invalid_days"] == 1
    next_day = cell_record(datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    other, _ = builder(tmp_path / "other", [next_day])
    with pytest.raises(DisplayProjectionError): other.process_chunk(DAY)
    broken, _ = builder(tmp_path / "broken", [{"schema_version": 1, "timestamp": base}])
    with pytest.raises(DisplayProjectionError): broken.process_chunk(DAY)


def test_failure_isolation_status_and_explicit_rebuild(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    core, output = builder(tmp_path, [cell_record(base + 1)])
    assert core.rebuild() == {"status": "completed", "days_total": 1,
        "days_completed": 1, "current_file": None, "records": 1,
        "bytes": (tmp_path / "cell" / f"{DAY}.jsonl").stat().st_size,
        "buckets": 128, "errors": 0}
    status = core.status()
    assert status["enabled"] and status["state"] == "available"
    assert status["days"] == status["complete_days"] == 1
    assert status["storage_bytes"] > 0 and status["failure_count"] == 0


def test_live_update_cost_is_small_and_all_resolutions_are_updated(tmp_path):
    today = "2026-09-02"; cell = tmp_path / "cell"; path = cell / f"{today}.jsonl"
    path.parent.mkdir(); path.touch()
    core = DisplayHistoryProjection(cell, tmp_path / "hycube", tmp_path / "display",
                                    max_records=1, clock=lambda: NOW)
    durations = []
    for index in range(40):
        with path.open("a") as handle:
            handle.write(json.dumps(cell_record(NOW + index * 5, soc=index)) + "\n")
        started = time.perf_counter(); core.process_chunk(today); durations.append(
            time.perf_counter() - started)
    assert all(read_projection(tmp_path / "display", resolution, today)
               for resolution in RESOLUTIONS)
    ordered = sorted(durations)
    assert statistics.median(durations) < .1
    assert ordered[int(len(ordered) * .95) - 1] < .2


def test_worker_is_failure_isolated_and_rebuild_is_explicit(tmp_path):
    core = DisplayHistoryProjection(tmp_path / "cell", tmp_path / "hycube",
                                    tmp_path / "display", clock=lambda: NOW)
    worker = DisplayHistoryProjectionWorker(core, interval_seconds=.01)
    assert worker.start() and not worker.start()
    assert worker.request_historical_rebuild()
    deadline = time.time() + 1
    while worker.status()["historical_rebuild_requested"] and time.time() < deadline:
        time.sleep(.01)
    assert worker.stop(timeout=1)
    assert worker.status()["worker_active"] is False


def test_late_current_day_record_never_rewrites_a_closed_bucket(tmp_path):
    today = "2026-09-02"; cell = tmp_path / "cell"; path = cell / f"{today}.jsonl"
    write_rows(path, [cell_record(NOW + 5), cell_record(NOW + 65)])
    core = DisplayHistoryProjection(cell, tmp_path / "hycube", tmp_path / "display",
                                    clock=lambda: NOW)
    core.process_chunk(today)
    with path.open("a") as handle:
        handle.write(json.dumps(cell_record(NOW + 10)) + "\n")
    with pytest.raises(DisplayProjectionError, match="requires day rebuild"):
        core.process_chunk(today)


def test_worker_crash_recovery_rebuilds_only_open_day(tmp_path):
    today = "2026-09-02"; cell = tmp_path / "cell"; output = tmp_path / "display"
    write_rows(cell / f"{today}.jsonl", [cell_record(NOW + 5, soc=10),
                                         cell_record(NOW + 65, soc=20)])
    core = DisplayHistoryProjection(cell, tmp_path / "hycube", output,
                                    clock=lambda: NOW)
    core.process_chunk(today)
    data = output / "1m" / f"{today}.jsonl.gz"
    data.write_bytes(data.read_bytes() + b"crash-tail")
    worker = DisplayHistoryProjectionWorker(core, interval_seconds=.01)
    assert worker.start()
    deadline = time.time() + 1
    while time.time() < deadline:
        try:
            if len(by(read_projection(output, "1m", today), "soc")) == 2: break
        except (OSError, EOFError):
            pass
        time.sleep(.01)
    assert worker.stop(timeout=1)
    records = by(read_projection(output, "1m", today), "soc")
    assert len(records) == 2
    assert sum(item["sample_count"] for item in records) == 2
