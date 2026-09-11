import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import history_block_index
from cell_history import CellHistoryWriter
from history_block_index import (BlockIndexError, build_index, extend_open_index,
                                 index_path, selected_ranges)
from history_series import CellHistorySeries
from history_timing import HistoryRequestTimingState
from hycube_evidence import HycubeBatteryCapacitySeries, HycubeEvidenceWriter
from hycube_projection import HycubeProjectionBackfill, HycubeProjectionStore


def projection_record(epoch, value, token):
    stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    return {"schema_version": 1, "record_type": "hycube_history_projection",
            "received_at": stamp, "battery_capacity": value,
            "device_timestamp": stamp, "timezone_semantics": "explicit",
            "parse_quality": "complete", "payload_sha256": token,
            "configured_interval_seconds": 5, "actual_interval_seconds": 5,
            "actual_interval_quality": "observed", "source_raw_end_offset": token}


def raw_record(epoch, value):
    stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    return {"schema_version": 1, "record_type": "hycube_system_observation",
            "received_at": stamp, "BatteryCapacity": value,
            "device_timestamp": stamp, "timezone_semantics": "explicit",
            "parse_quality": "complete", "payload_sha256": str(value),
            "configured_interval_seconds": 5, "actual_interval_seconds": 5,
            "actual_interval_quality": "observed"}


def cell_record(epoch, module=1, value=50):
    return {"schema_version": 1, "timestamp": epoch, "module": module,
            "voltages_mv": [3300 + value] * 15, "current_a": value / 10,
            "soc_percent": value, "temperatures_c": [25] * 15,
            "balancing": [False] * 15, "physical_groups": {},
            "module_serial": f"SN-{module}"}


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, separators=(",", ":")) + "\n"
                            for item in records), encoding="utf-8")


def test_block_ranges_preserve_duplicates_regressions_order_and_provenance(tmp_path):
    path = tmp_path / "2026-09-01.jsonl"
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    epochs = [base + index * 60 for index in range(800)]
    epochs[520] = epochs[300]                 # unbounded-looking regression
    epochs[521] = epochs[520]                 # duplicate timestamp and value
    records = [projection_record(epoch, index % 101, index) for index, epoch in enumerate(epochs)]
    records[521]["battery_capacity"] = records[520]["battery_capacity"]
    records[521]["payload_sha256"] = records[520]["payload_sha256"]
    write_jsonl(path, records)
    start, end = epochs[280], epochs[540]
    build_index(path, timestamp_field="received_at", iso_timestamp=True, block_records=64)
    ranges, info = selected_ranges(path, start, end,
                                   timestamp_field="received_at", iso_timestamp=True)
    indexed = []
    with path.open("rb") as handle:
        for lo, hi in ranges:
            handle.seek(lo)
            while handle.tell() < hi:
                indexed.append(json.loads(handle.readline()))
    indexed = [item for item in indexed
               if start <= datetime.fromisoformat(item["received_at"]).timestamp() <= end]
    expected = [item for item in records
                if start <= datetime.fromisoformat(item["received_at"]).timestamp() <= end]
    assert indexed == expected
    assert info["skipped_bytes"] > 0


@pytest.mark.parametrize("damage", ["missing", "corrupt", "schema", "truncate", "changed"])
def test_invalid_index_is_detected_for_full_scan_fallback(tmp_path, damage):
    path = tmp_path / "day.jsonl"
    write_jsonl(path, [cell_record(1000 + index) for index in range(300)])
    build_index(path, timestamp_field="timestamp", iso_timestamp=False)
    idx = index_path(path)
    if damage == "missing": idx.unlink()
    elif damage == "corrupt": idx.write_text("{")
    elif damage == "schema":
        value = json.loads(idx.read_text()); value["schema_version"] = 99
        idx.write_text(json.dumps(value))
    elif damage == "truncate": path.write_bytes(path.read_bytes()[:100])
    else:
        raw = bytearray(path.read_bytes()); raw[0] = ord(" "); path.write_bytes(raw)
    with pytest.raises(BlockIndexError):
        selected_ranges(path, 1100, 1200, timestamp_field="timestamp", iso_timestamp=False)


def test_open_index_reads_unindexed_growth_and_detects_prefix_change(tmp_path):
    path = tmp_path / "day.jsonl"
    write_jsonl(path, [cell_record(1000 + index) for index in range(256)])
    extend_open_index(path, timestamp_field="timestamp", iso_timestamp=False)
    with path.open("a") as handle:
        for index in range(256, 270):
            handle.write(json.dumps(cell_record(1000 + index)) + "\n")
    ranges, info = selected_ranges(path, 1258, 1269,
                                   timestamp_field="timestamp", iso_timestamp=False)
    assert ranges[-1][1] == path.stat().st_size
    assert info["indexed_size"] < path.stat().st_size
    raw = bytearray(path.read_bytes()); raw[0] = ord(" "); path.write_bytes(raw)
    with pytest.raises(BlockIndexError):
        selected_ranges(path, 1258, 1269,
                        timestamp_field="timestamp", iso_timestamp=False)


def test_interrupted_index_publication_leaves_source_and_previous_index(tmp_path, monkeypatch):
    path = tmp_path / "day.jsonl"; write_jsonl(path, [cell_record(index) for index in range(300)])
    build_index(path, timestamp_field="timestamp", iso_timestamp=False)
    before_source = path.read_bytes(); before_index = index_path(path).read_bytes()
    monkeypatch.setattr(history_block_index, "_atomic_write",
                        lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        build_index(path, timestamp_field="timestamp", iso_timestamp=False)
    assert path.read_bytes() == before_source
    assert index_path(path).read_bytes() == before_index


def test_hycube_full_scan_and_index_are_pointwise_equal_cross_day(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw)
    base = datetime(2026, 9, 1, 20, tzinfo=timezone.utc).timestamp()
    for index in range(900):
        epoch = base + index * 60
        if index == 600: epoch = base + 300 * 60
        writer.append(raw_record(epoch, index % 101))
    HycubeProjectionBackfill(raw, projection, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 3, tzinfo=timezone.utc).timestamp()).run_once()
    args = dict(timestamp_from="2026-09-01T23:00:00+00:00",
                timestamp_to="2026-09-02T08:00:00+00:00", max_points=180)
    indexed = HycubeBatteryCapacitySeries(raw, projection_directory=projection).query(**args)
    for idx in projection.glob("*.idx"): idx.rename(idx.with_suffix(".disabled"))
    scanned = HycubeBatteryCapacitySeries(raw, projection_directory=projection).query(**args)
    assert indexed["points"] == scanned["points"]
    assert indexed["raw_records"] == scanned["raw_records"]


def test_current_day_projection_suffix_and_raw_tail_remain_equal_to_raw(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    day = datetime.now(timezone.utc).date().isoformat()
    start = datetime.fromisoformat(day + "T00:00:00+00:00").timestamp()
    for index in range(300):
        record = raw_record(start + index * 5, index % 101)
        receipt = writer.append_with_receipt(record); assert store.append_live(record, receipt)
    writer.append(raw_record(start + 1501, 77))
    args = dict(timestamp_from=day + "T00:20:00+00:00",
                timestamp_to=day + "T00:30:00+00:00", max_points=850)
    indexed = HycubeBatteryCapacitySeries(raw, projection_directory=projection).query(**args)
    scanned = HycubeBatteryCapacitySeries(raw, projection_directory=tmp_path / "none").query(**args)
    assert indexed["points"] == scanned["points"]


def test_cell_full_scan_and_index_are_pointwise_equal_with_module_filter(tmp_path):
    directory = tmp_path / "cell"; path = directory / "2026-09-01.jsonl"
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    records = [cell_record(base + index * 10, index % 6 + 1, index % 100)
               for index in range(1800)]
    records[1200]["timestamp"] = records[600]["timestamp"]
    records[1201] = dict(records[1200])
    write_jsonl(path, records)
    args = dict(metric="soc", timestamp_from="2026-09-01T02:00:00+00:00",
                timestamp_to="2026-09-01T03:00:00+00:00", module_number=1,
                max_points=100)
    scanned = CellHistorySeries(directory).query_bundle(**args)
    build_index(path, timestamp_field="timestamp", iso_timestamp=False)
    indexed = CellHistorySeries(directory).query_bundle(**args)
    assert indexed["points"] == scanned["points"]
    assert indexed["samples"] == scanned["samples"]
    assert indexed["raw_records"] == scanned["raw_records"]


def test_cell_writer_index_failure_never_affects_history(tmp_path, monkeypatch):
    monkeypatch.setattr("cell_history.extend_open_index",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index")))
    writer = CellHistoryWriter(tmp_path)
    writer.append(cell_record(1000))
    assert len(next(tmp_path.glob("*.jsonl")).read_text().splitlines()) == 1


def test_cell_live_index_keeps_open_suffix_and_reader_falls_back_if_corrupt(tmp_path):
    writer = CellHistoryWriter(tmp_path)
    for index in range(300):
        writer.append(cell_record(index, index % 6 + 1, index % 100))
    path = next(tmp_path.glob("*.jsonl")); idx = index_path(path)
    assert idx.exists()
    value = json.loads(idx.read_text())
    assert value["indexed_size"] < path.stat().st_size and value["complete"] is False
    args = dict(metric="soc", timestamp_from="1970-01-01T00:04:20+00:00",
                timestamp_to="1970-01-01T00:05:00+00:00", module_number=1)
    expected = CellHistorySeries(tmp_path).query_bundle(**args)
    value["schema_version"] = 99; idx.write_text(json.dumps(value))
    fallback = CellHistorySeries(tmp_path).query_bundle(**args)
    assert fallback["points"] == expected["points"]


def test_existing_complete_projection_gets_index_from_explicit_backfill(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw)
    writer.append(raw_record(datetime(2026, 9, 1, 10, tzinfo=timezone.utc).timestamp(), 42))
    worker = HycubeProjectionBackfill(raw, projection, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    worker.run_once()
    idx = index_path(projection / "2026-09-01.jsonl")
    assert idx.exists(); idx.unlink()
    worker.run_once()
    assert idx.exists()


def test_explicit_backfill_rebuilds_existing_historical_cell_index(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    cell = tmp_path / "cell"; path = cell / "2026-09-01.jsonl"
    write_jsonl(path, [cell_record(1000 + index) for index in range(300)])
    worker = HycubeProjectionBackfill(
        raw, projection, cell_history_directory=cell, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    worker.run_once(include_current=False, include_historical=True)
    assert index_path(path).exists()


def test_seek_observability_and_parallel_requests(tmp_path):
    directory = tmp_path / "cell"; path = directory / "1970-01-01.jsonl"
    write_jsonl(path, [cell_record(index, index % 6 + 1) for index in range(1024)])
    build_index(path, timestamp_field="timestamp", iso_timestamp=False)
    args = dict(requests=({"metric": "soc"},),
                timestamp_from="1970-01-01T00:08:20+00:00",
                timestamp_to="1970-01-01T00:10:00+00:00", module_number=1)
    def query(_):
        timing = HistoryRequestTimingState(); timing.begin({"view": "test"})
        result = CellHistorySeries(directory).query_bundles(**args, timing=timing)
        return result, timing.context()["counts"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(query, range(8)))
    assert all(item[0]["series"] == results[0][0]["series"] for item in results)
    assert all(item[1]["cell_seek_mode"] == "block_index" for item in results)
    assert all(item[1]["cell_skipped_bytes"] > 0 for item in results)


def test_production_shape_indexes_reduce_hycube_and_cell_reads(tmp_path):
    base = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    projection = tmp_path / "projection.jsonl"
    records = [projection_record(base + index * 5, index % 101, index)
               for index in range(25325)]
    write_jsonl(projection, records)
    build_index(projection, timestamp_field="received_at", iso_timestamp=True)
    ranges, info = selected_ranges(
        projection, base + 4800 * 5, base + (4800 + 15664) * 5,
        timestamp_field="received_at", iso_timestamp=True)
    read_bytes = sum(hi - lo for lo, hi in ranges)
    assert info["skipped_bytes"] > 0 and read_bytes < projection.stat().st_size

    cell = tmp_path / "cell.jsonl"
    write_jsonl(cell, [cell_record(base + index * 10, index % 6 + 1)
                       for index in range(12947)])
    build_index(cell, timestamp_field="timestamp", iso_timestamp=False)
    ranges, info = selected_ranges(cell, base + 5000 * 10, base + 6404 * 10,
                                   timestamp_field="timestamp", iso_timestamp=False)
    assert info["skipped_bytes"] > 0
    assert sum(hi - lo for lo, hi in ranges) < cell.stat().st_size
