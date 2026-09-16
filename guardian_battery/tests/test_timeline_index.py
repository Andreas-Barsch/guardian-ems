import json
import threading
import time
from datetime import datetime, timezone

import pytest

from timeline import TechnicalEventSource
from timeline_index import (OPEN_SUFFIX_MAX_BYTES, SMALL_SOURCE_MAX_BYTES,
                            TimelineIndexError, TimelineIndexWorker, extend,
                            index_path, load_valid, rebuild, select_ranges)


def event(timestamp, number, *, kind="alarm_started", module=1):
    if kind == "alarm_started":
        return {"timestamp": timestamp, "type": kind, "status": "warning",
                "alarm": {"code": f"{module}:A{number}", "message": f"alarm {number}",
                          "module": module, "level": "warning"}}
    if kind == "alarm_cleared":
        return {"timestamp": timestamp, "type": kind, "code": f"{module}:A{number}"}
    return {"timestamp": timestamp, "type": "status_changed",
            "from": "ok", "to": "warning"}


def write(path, rows, suffix=b""):
    path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows) + suffix)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_index_contract_range_selection_and_reference_equality(tmp_path):
    source = tmp_path / "events.jsonl"
    rows = [event(1_000 + index * 60, index, kind=(
        "status_changed" if index % 3 == 0 else
        "alarm_cleared" if index % 3 == 1 else "alarm_started"))
        for index in range(12)]
    write(source, rows)
    raw = source.read_bytes()
    value = rebuild(source, block_records=4,
                    validate_record=TechnicalEventSource.validate_index_record)

    assert value["index_kind"] == "guardian_timeline_block_index_v1"
    assert len(value["blocks"]) == 3
    assert value["blocks"][1]["line_start"] == 5
    ranges, profile = select_ranges(source, 1_000 + 4 * 60, 1_000 + 7 * 60)
    assert len(ranges) == 1
    assert profile == {"index_present": True, "index_valid": True,
        "selected_blocks": 1,
        "selected_bytes": ranges[0]["byte_end"] - ranges[0]["byte_start"],
        "open_suffix_bytes": 0, "read_mode": "timeline_block_index"}

    legacy = [item for item in TechnicalEventSource(source).read()
              if iso(1_000 + 4 * 60) <= item.timestamp <= iso(1_000 + 7 * 60)]
    observed, available = TechnicalEventSource(source).read_range(
        iso(1_000 + 4 * 60), iso(1_000 + 7 * 60), profile={})
    assert available is True
    assert observed == legacy
    assert source.read_bytes() == raw


def test_out_of_order_blocks_and_multiple_matches_are_selected(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100, 1), event(900, 2), event(200, 3), event(800, 4)])
    rebuild(source, block_records=2,
            validate_record=TechnicalEventSource.validate_index_record)
    ranges, profile = select_ranges(source, 150, 250)
    assert len(ranges) == 2
    assert profile["selected_blocks"] == 2
    result, available = TechnicalEventSource(source).read_range(iso(150), iso(250))
    assert available and [item.summary for item in result] == ["alarm 3"]


def test_append_resume_partial_line_and_confirmed_blocks_are_stable(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100, 1), event(200, 2)], b'{"partial"')
    raw = source.read_bytes()
    first = extend(source, block_records=2,
                   validate_record=TechnicalEventSource.validate_index_record)
    confirmed = dict(first["blocks"][0])
    assert first["indexed_size"] < len(raw)
    _, profile = select_ranges(source, 0, 1_000)
    assert profile["open_suffix_bytes"] == len(raw) - first["indexed_size"]
    result, available = TechnicalEventSource(source).read_range(iso(0), iso(1_000))
    assert available and len(result) == 2

    source.write_bytes(raw[:-len(b'{"partial"')] +
                       (json.dumps(event(300, 3)) + "\n" +
                        json.dumps(event(400, 4)) + "\n").encode())
    second = extend(source, block_records=2,
                    validate_record=TechnicalEventSource.validate_index_record)
    assert second["blocks"][0] == confirmed
    assert len(second["blocks"]) == 2


@pytest.mark.parametrize("mutation", ["replace", "truncate", "same_size", "corrupt"])
def test_replaced_truncated_or_corrupt_source_is_rejected(tmp_path, mutation):
    source = tmp_path / "events.jsonl"
    write(source, [event(100, 1), event(200, 2)])
    rebuild(source, block_records=1)
    if mutation == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(source.read_bytes())
        source.unlink(); replacement.rename(source)
    elif mutation == "truncate":
        source.write_bytes(source.read_bytes()[:10])
    elif mutation == "same_size":
        raw = bytearray(source.read_bytes())
        raw[0] = ord(" ") if raw[0] != ord(" ") else ord("{")
        source.write_bytes(raw)
    else:
        index_path(source).write_text("{broken", encoding="utf-8")
    with pytest.raises(TimelineIndexError):
        load_valid(source)


def test_large_missing_invalid_or_unbounded_suffix_is_unavailable(tmp_path):
    source = tmp_path / "events.jsonl"
    row = json.dumps(event(100, 1)).encode() + b"\n"
    source.write_bytes(row * (SMALL_SOURCE_MAX_BYTES // len(row) + 2))
    raw = source.read_bytes()
    result, available = TechnicalEventSource(source).read_range(iso(0), iso(1_000))
    assert result == [] and available is False
    assert not index_path(source).exists()

    rebuild(source, block_records=512)
    index_path(source).write_text("{}", encoding="utf-8")
    result, available = TechnicalEventSource(source).read_range(iso(0), iso(1_000))
    assert result == [] and available is False
    assert source.read_bytes() == raw


def test_small_missing_index_has_bounded_full_read_and_empty_means_complete(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100, 1)])
    profile = {}
    result, available = TechnicalEventSource(source).read_range(
        iso(200), iso(300), profile=profile)
    assert available is True and result == []
    assert profile["read_mode"] == "bounded_small_source"
    assert profile["index_present"] is False
    assert not index_path(source).exists()


def test_indexed_range_without_overlap_is_complete_empty_evidence(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100 + index, index) for index in range(1_024)])
    rebuild(source, block_records=512)
    profile = {}
    result, available = TechnicalEventSource(source).read_range(
        iso(5_000), iso(6_000), profile=profile)
    assert result == [] and available is True
    assert profile["read_mode"] == "timeline_block_index"
    assert profile["selected_blocks"] == 0
    assert profile["raw_bytes_read"] == profile["records_inspected"] == 0


def test_worker_autonomously_backfills_existing_source_and_stops(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100 + index, index) for index in range(1_100)])
    worker = TimelineIndexWorker(
        source, validate_record=TechnicalEventSource.validate_index_record,
        interval_seconds=0.01)
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if index_path(source).exists() and len(load_valid(source)["blocks"]) >= 2:
            break
        time.sleep(0.01)
    assert len(load_valid(source)["blocks"]) >= 2
    assert worker.stop(timeout=1)
    assert not worker.is_alive()


def test_worker_slow_block_is_background_and_shutdown_is_bounded(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100 + index, index) for index in range(512)])
    entered = threading.Event()

    def slow_validation(raw):
        entered.set()
        time.sleep(0.001)
        TechnicalEventSource.validate_index_record(raw)

    worker = TimelineIndexWorker(source, validate_record=slow_validation,
                                 interval_seconds=0.01)
    started = time.monotonic()
    worker.start()
    assert time.monotonic() - started < 0.1
    assert entered.wait(1)
    stopped = time.monotonic()
    assert worker.stop(timeout=1)
    assert time.monotonic() - stopped < 1


def test_worker_failure_isolated_and_thread_remains_alive(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text('{"invalid":true}\n', encoding="utf-8")
    worker = TimelineIndexWorker(
        source, validate_record=TechnicalEventSource.validate_index_record,
        interval_seconds=0.01)
    worker.start()
    deadline = time.monotonic() + 1
    while worker.last_error is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.last_error is not None
    assert worker.is_alive()
    assert worker.stop(timeout=1)


def test_open_suffix_limit_is_hard(tmp_path):
    source = tmp_path / "events.jsonl"
    write(source, [event(100, 1), event(200, 2)])
    extend(source, block_records=2)
    with source.open("ab") as handle:
        handle.write(b" " * (OPEN_SUFFIX_MAX_BYTES + 1))
    with pytest.raises(TimelineIndexError):
        select_ranges(source, 0, 1_000)
    result, available = TechnicalEventSource(source).read_range(iso(0), iso(1_000))
    assert result == [] and available is False
