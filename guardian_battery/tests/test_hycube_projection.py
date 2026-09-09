import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from hycube_evidence import (HycubeBatteryCapacitySeries, HycubeCollector,
                             HycubeEvidenceWriter)
from hycube_projection import (HycubeProjectionBackfill, HycubeProjectionStore,
                               projection_plan)
from maintenance_ui import render_maintenance_html
from history_timing import HistoryRequestTimingState


def raw_record(timestamp, capacity, **extra):
    return {"schema_version": 1, "record_type": "hycube_system_observation",
            "received_at": timestamp, "BatteryCapacity": capacity,
            "device_timestamp": extra.pop("device_timestamp", timestamp),
            "timezone_semantics": "explicit", "parse_quality": "complete",
            "payload_sha256": "a" * 64, "configured_interval_seconds": 5,
            "actual_interval_seconds": extra.pop("actual_interval_seconds", 5),
            "actual_interval_quality": extra.pop("actual_interval_quality", "observed"),
            **extra}


def append(writer, store, record):
    receipt = writer.append_with_receipt(record)
    assert receipt.end_offset == receipt.path.stat().st_size
    assert store.append_live(record, receipt)
    return receipt


def query(raw, projection, start="2026-09-01T00:00:00+00:00",
          end="2026-09-03T00:00:00+00:00"):
    return HycubeBatteryCapacitySeries(raw, projection_directory=projection).query(
        timestamp_from=start, timestamp_to=end, max_points=850)


def test_live_projection_is_raw_first_exact_and_preserves_multiplicity(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw)
    store = HycubeProjectionStore(projection, flush_records=99)
    timestamp = "2026-09-02T10:00:00+00:00"
    first = append(writer, store, raw_record(timestamp, 42))
    second = append(writer, store, raw_record(timestamp, 42))
    assert not list(projection.glob("*.jsonl"))
    store.close()
    rows = [json.loads(line) for line in (projection / "2026-09-02.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0] == {"schema_version": 1, "record_type": "hycube_history_projection",
        "received_at": timestamp, "battery_capacity": 42,
        "device_timestamp": timestamp, "timezone_semantics": "explicit",
        "parse_quality": "complete", "payload_sha256": "a" * 64,
        "configured_interval_seconds": 5, "actual_interval_seconds": 5,
        "actual_interval_quality": "observed", "source_raw_end_offset": first.end_offset}
    assert rows[1]["source_raw_end_offset"] == second.end_offset
    assert len(query(raw, projection)["points"]) == 2


def test_current_day_projection_plus_strict_raw_tail_matches_raw(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    day = datetime.now(timezone.utc).date().isoformat()
    first = raw_record(f"{day}T10:00:00+00:00", 41)
    append(writer, store, first)
    writer.append(raw_record(f"{day}T10:00:05+00:00", 42))
    plan = projection_plan(raw / f"{day}.jsonl", projection)
    assert plan["mode"] == "projection_tail"
    start = f"{day}T00:00:00+00:00"; end = f"{day}T23:59:59+00:00"
    projected = query(raw, projection, start, end)
    raw_only = query(raw, tmp_path / "missing", start, end)
    assert projected["points"] == raw_only["points"]


@pytest.mark.parametrize("damage", ["missing", "corrupt", "offset"])
def test_invalid_or_missing_projection_metadata_falls_back_to_raw(tmp_path, damage):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    append(writer, store, raw_record("2026-09-02T10:00:00+00:00", 42))
    metadata = projection / "2026-09-02.meta.json"
    if damage == "missing": metadata.unlink()
    elif damage == "corrupt": metadata.write_text("{")
    else:
        value = json.loads(metadata.read_text()); value["confirmed_raw_end_offset"] += 100
        metadata.write_text(json.dumps(value))
    assert projection_plan(raw / "2026-09-02.jsonl", projection)["mode"] == "raw"
    assert query(raw, projection)["points"][0]["value"] == 42


def test_corrupt_projection_body_falls_back_for_only_that_day(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    append(writer, store, raw_record("2026-09-02T10:00:00+00:00", 42))
    path = projection / "2026-09-02.jsonl"
    path.write_text("{" + " " * (path.stat().st_size - 1))
    metadata = json.loads((projection / "2026-09-02.meta.json").read_text())
    metadata["projection_end_offset"] = path.stat().st_size
    metadata["status"] = "complete"
    (projection / "2026-09-02.meta.json").write_text(json.dumps(metadata))
    assert query(raw, projection)["points"][0]["value"] == 42


def test_backfill_historical_day_is_bounded_resumable_and_atomically_promoted(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw)
    for second in range(3):
        writer.append(raw_record(f"2026-09-01T10:00:0{second}+00:00", 40 + second))
    backfill = HycubeProjectionBackfill(raw, projection, max_records=1,
        pause_seconds=0, clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    status = backfill.run_once()
    assert status["status"] == "completed" and status["files_completed"] == 1
    assert not list(projection.glob("*.building"))
    metadata = json.loads((projection / "2026-09-01.meta.json").read_text())
    assert metadata["status"] == "complete"
    assert metadata["projection_record_count"] == 3
    assert [point["value"] for point in query(raw, projection)["points"]] == [40, 41, 42]
    before = (projection / "2026-09-01.jsonl").read_bytes()
    backfill.run_once()
    assert (projection / "2026-09-01.jsonl").read_bytes() == before


def test_backfill_invalid_json_never_marks_day_complete(tmp_path):
    raw = tmp_path / "raw"; raw.mkdir()
    (raw / "2026-09-01.jsonl").write_text("{broken\n")
    backfill = HycubeProjectionBackfill(raw, tmp_path / "projection", pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    status = backfill.run_once()
    assert status["errors"] == 1 and status["files_completed"] == 0
    assert not (tmp_path / "projection" / "2026-09-01.meta.json").exists()


def test_skipped_wrong_type_advances_offset_without_inventing_point(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    append(writer, store, raw_record("2026-09-02T10:00:00+00:00", "42"))
    metadata = json.loads((projection / "2026-09-02.meta.json").read_text())
    assert metadata["confirmed_raw_end_offset"] == next(raw.glob("*.jsonl")).stat().st_size
    assert metadata["projection_record_count"] == 0
    assert query(raw, projection)["points"] == []


def test_projection_failure_cannot_remove_durable_raw_or_repeat_get(tmp_path):
    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def getcode(self): return 200
        def read(self, _): return b'{"BatteryCapacity":42,"Date2":"2026-09-02T10:00:00+00:00"}'
    class Opener:
        calls = 0
        def open(self, *_args, **_kwargs): self.calls += 1; return Response()
    class BrokenProjection:
        def append_live(self, *_): raise RuntimeError("projection unavailable")
        def close(self): pass
        def status(self): return {"state": "error"}
    opener = Opener()
    collector = HycubeCollector("http://localhost", HycubeEvidenceWriter(tmp_path / "raw"),
        projection_store=BrokenProjection(), opener=opener,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    assert collector.collect_once()["BatteryCapacity"] == 42
    assert opener.calls == 1
    assert len(next((tmp_path / "raw").glob("*.jsonl")).read_text().splitlines()) == 1


def test_final_epoch_order_retains_clock_regression(tmp_path):
    raw = tmp_path / "raw"; writer = HycubeEvidenceWriter(raw)
    writer.append(raw_record("2026-09-02T10:00:05+00:00", 45,
                             actual_interval_seconds=None,
                             actual_interval_quality="clock_regression"))
    writer.append(raw_record("2026-09-02T10:00:00+00:00", 40))
    points = query(raw, tmp_path / "projection")["points"]
    assert [point["value"] for point in points] == [40, 45]
    assert points[1]["actual_interval_quality"] == "clock_regression"


def test_cache_signature_invalidates_when_raw_tail_grows(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    day = datetime.now(timezone.utc).date().isoformat()
    append(writer, store, raw_record(f"{day}T10:00:00+00:00", 40))
    series = HycubeBatteryCapacitySeries(raw, projection_directory=projection)
    args = {"timestamp_from": f"{day}T00:00:00+00:00",
            "timestamp_to": f"{day}T23:59:59+00:00", "max_points": 850}
    assert series.query(**args)["cache_hit"] is False
    assert series.query(**args)["cache_hit"] is True
    writer.append(raw_record(f"{day}T10:00:05+00:00", 41))
    refreshed = series.query(**args)
    assert refreshed["cache_hit"] is False
    assert [point["value"] for point in refreshed["points"]] == [40, 41]


def test_maintenance_ui_exposes_projection_and_backfill_as_technical_status():
    html = render_maintenance_html(configuration_path="configuration")
    assert "Hycube History Projection" in html
    assert 'id="projection-status"' in html
    assert "Kein Batterie-, Alarm- oder Cell-Risk-Status" in html


def test_history_timing_separates_projection_and_raw_fallback(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw)
    writer.append(raw_record("2026-09-01T10:00:00+00:00", 40))
    HycubeProjectionBackfill(raw, projection, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()).run_once()
    state = HistoryRequestTimingState(); state.begin({"view": "test"})
    HycubeBatteryCapacitySeries(raw, projection_directory=projection).query(
        timestamp_from="2026-09-01T00:00:00+00:00",
        timestamp_to="2026-09-01T23:59:59+00:00", timing=state)
    context = state.context()
    assert context["components"]["hycube_projection_read_parse_filter"]["status"] == "executed"
    assert context["components"]["hycube_raw_fallback_read_parse_filter"]["status"] == "not_executed"
    assert context["counts"]["hycube_source_mode"] == "projection"
    assert context["counts"]["hycube_projection_files_opened"] == 1
    assert context["counts"]["hycube_projection_bytes_read"] > 0


def test_startup_worker_only_catches_current_day_until_explicit_request(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"; writer = HycubeEvidenceWriter(raw)
    today = datetime.now(timezone.utc).date().isoformat()
    writer.append(raw_record("2026-09-01T10:00:00+00:00", 40))
    writer.append(raw_record(f"{today}T10:00:00+00:00", 41))
    worker = HycubeProjectionBackfill(raw, projection, pause_seconds=0,
                                      scan_interval_seconds=60)
    worker.start()
    deadline = time.monotonic() + 2
    while not (projection / f"{today}.meta.json").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (projection / f"{today}.meta.json").exists()
    assert not (projection / "2026-09-01.jsonl").exists()
    assert worker.request_historical_backfill() is True
    assert worker.request_historical_backfill() is False
    deadline = time.monotonic() + 2
    while not (projection / "2026-09-01.jsonl").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (projection / "2026-09-01.jsonl").exists()
    assert worker.stop(timeout=2)


@pytest.mark.parametrize("failure", [OSError("ENOSPC"), PermissionError("denied")])
def test_projection_flush_failure_is_bounded_and_raw_remains_complete(
        tmp_path, monkeypatch, failure):
    import hycube_projection
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=2)
    monkeypatch.setattr(hycube_projection, "_write_json_atomic",
                        lambda *_: (_ for _ in ()).throw(failure))
    for second in range(5):
        receipt = writer.append_with_receipt(
            raw_record(f"2026-09-02T10:00:0{second}+00:00", 40 + second))
        store.append_live(raw_record(f"2026-09-02T10:00:0{second}+00:00", 40 + second), receipt)
        assert store.status()["buffered_records"] < 2
    assert len(next(raw.glob("*.jsonl")).read_text().splitlines()) == 5
    assert store.status()["failure_count"] >= 1


def test_golden_zero_extremes_duplicates_and_downsampling_equal_raw(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw)
    values = [95, 95, 0, 95, 100, -1, 100.5, 42.25]
    for index, value in enumerate(values):
        timestamp = "2026-09-01T10:00:00+00:00" if index < 2 else \
            f"2026-09-01T10:00:{index:02d}+00:00"
        writer.append(raw_record(timestamp, value, payload_sha256="same"))
    HycubeProjectionBackfill(raw, projection, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()).run_once()
    args = ("2026-09-01T00:00:00+00:00", "2026-09-01T23:59:59+00:00")
    projected = query(raw, projection, *args)
    raw_only = query(raw, tmp_path / "none", *args)
    assert projected["points"] == raw_only["points"]
    assert [point["value"] for point in projected["points"]] == values
    projected_small = HycubeBatteryCapacitySeries(raw, projection_directory=projection).query(
        timestamp_from=args[0], timestamp_to=args[1], max_points=4)
    raw_small = HycubeBatteryCapacitySeries(raw, projection_directory=tmp_path / "none").query(
        timestamp_from=args[0], timestamp_to=args[1], max_points=4)
    assert projected_small["points"] == raw_small["points"]


def test_sidecar_ahead_of_last_projected_source_offset_is_invalid(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"
    writer = HycubeEvidenceWriter(raw); store = HycubeProjectionStore(projection, flush_records=1)
    first = append(writer, store, raw_record("2026-09-02T10:00:00+00:00", 40))
    second = writer.append_with_receipt(raw_record("2026-09-02T10:00:05+00:00", 41))
    metadata_path = projection / "2026-09-02.meta.json"
    metadata = json.loads(metadata_path.read_text())
    assert metadata["last_projection_source_raw_end_offset"] == first.end_offset
    metadata.update(status="complete", confirmed_raw_end_offset=second.end_offset,
                    raw_size=second.end_offset,
                    raw_mtime_ns=second.path.stat().st_mtime_ns)
    metadata_path.write_text(json.dumps(metadata))
    plan = projection_plan(second.path, projection,
                           current_day="2026-09-03")
    assert plan["mode"] == "raw" and plan["fallback_reason"] == "metadata_invalid"


def test_projection_hook_does_not_change_raw_utf8_jsonl_bytes(tmp_path):
    record = raw_record("2026-09-02T10:00:00+00:00", 42,
                        raw_payload='{"note":"Größe \\"quoted\\""}')
    plain = HycubeEvidenceWriter(tmp_path / "plain")
    hooked = HycubeEvidenceWriter(tmp_path / "hooked")
    plain.append(record)
    store = HycubeProjectionStore(tmp_path / "projection", flush_records=1)
    receipt = hooked.append_with_receipt(record); store.append_live(record, receipt)
    assert next((tmp_path / "plain").glob("*.jsonl")).read_bytes() == \
        next((tmp_path / "hooked").glob("*.jsonl")).read_bytes()


def test_backfill_recovers_after_bad_raw_day_is_repaired(tmp_path):
    raw = tmp_path / "raw"; projection = tmp_path / "projection"; raw.mkdir()
    path = raw / "2026-09-01.jsonl"; path.write_text("{bad\n")
    worker = HycubeProjectionBackfill(raw, projection, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp())
    assert worker.run_once()["errors"] == 1
    HycubeEvidenceWriter(raw).append(raw_record("2026-09-01T10:00:00+00:00", 42))
    path.write_text(path.read_text().splitlines()[-1] + "\n")
    recovered = worker.run_once()
    assert recovered["files_completed"] == 1 and recovered["errors"] == 0
    assert query(raw, projection)["points"][0]["value"] == 42


def test_parallel_projection_history_reads_do_not_mix_state(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    raw = tmp_path / "raw"; projection = tmp_path / "projection"; writer = HycubeEvidenceWriter(raw)
    for second in range(20):
        writer.append(raw_record(f"2026-09-01T10:00:{second:02d}+00:00", second))
    HycubeProjectionBackfill(raw, projection, pause_seconds=0,
        clock=lambda: datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()).run_once()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: query(raw, projection), range(8)))
    assert all(result["points"] == results[0]["points"] for result in results)
