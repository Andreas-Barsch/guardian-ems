"""Rebuildable block index for stateless append-only Guardian timelines."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from pathlib import Path


SCHEMA_VERSION = 1
INDEX_KIND = "guardian_timeline_block_index_v1"
BLOCK_RECORDS = 512
OPEN_SUFFIX_MAX_BYTES = 512 * 1024
SMALL_SOURCE_MAX_BYTES = 512 * 1024
SIGNATURE_BYTES = 4096


class TimelineIndexError(ValueError):
    pass


def index_path(source: Path | str) -> Path:
    return Path(str(source) + ".timeline-v1.idx")


def _edge_hash(source: Path, indexed_size: int) -> tuple[str, str]:
    with source.open("rb") as handle:
        head = handle.read(min(SIGNATURE_BYTES, indexed_size))
        handle.seek(max(0, indexed_size - SIGNATURE_BYTES))
        tail = handle.read(indexed_size - max(0, indexed_size - SIGNATURE_BYTES))
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    os.replace(temporary, path)


def _empty(source: Path, block_records: int) -> dict:
    stat = source.stat()
    empty_hash = hashlib.sha256(b"").hexdigest()
    return {"schema_version": SCHEMA_VERSION, "index_kind": INDEX_KIND,
            "source_filename": source.name, "source_device": stat.st_dev,
            "source_inode": stat.st_ino, "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns, "indexed_size": 0,
            "block_records": int(block_records), "timestamp_field": "timestamp",
            "timestamp_encoding": "unix_seconds", "prefix_head_sha256": empty_hash,
            "prefix_tail_sha256": empty_hash, "blocks": []}


def load_valid(source: Path | str) -> dict:
    """Validate that the sidecar still describes the authoritative raw prefix."""
    source = Path(source)
    try:
        value = json.loads(index_path(source).read_text(encoding="utf-8"))
        stat = source.stat()
        if (not isinstance(value, dict)
                or value.get("schema_version") != SCHEMA_VERSION
                or value.get("index_kind") != INDEX_KIND
                or value.get("source_filename") != source.name
                or value.get("source_device") != stat.st_dev
                or value.get("source_inode") != stat.st_ino
                or value.get("timestamp_field") != "timestamp"
                or value.get("timestamp_encoding") != "unix_seconds"
                or int(value.get("block_records", 0)) <= 0):
            raise TimelineIndexError("timeline index contract mismatch")
        indexed_size = int(value["indexed_size"])
        if indexed_size < 0 or indexed_size > stat.st_size:
            raise TimelineIndexError("indexed timeline source was truncated")
        if (stat.st_size == int(value.get("source_size", -1))
                and stat.st_mtime_ns != int(value.get("source_mtime_ns", -1))):
            raise TimelineIndexError("timeline source changed without append")
        head, tail = _edge_hash(source, indexed_size)
        if (head != value.get("prefix_head_sha256")
                or tail != value.get("prefix_tail_sha256")):
            raise TimelineIndexError("indexed timeline prefix changed")
        cursor = 0
        next_line = 1
        for block in value.get("blocks", ()):
            start, end = int(block["byte_start"]), int(block["byte_end"])
            minimum = float(block["timestamp_min"])
            maximum = float(block["timestamp_max"])
            if (start != cursor or end <= start or end > indexed_size
                    or int(block["record_count"]) <= 0
                    or int(block["line_start"]) != next_line
                    or int(block["line_end"]) < int(block["line_start"])
                    or not math.isfinite(minimum) or not math.isfinite(maximum)
                    or minimum > maximum):
                raise TimelineIndexError("invalid timeline block")
            cursor = end
            next_line = int(block["line_end"]) + 1
        if cursor != indexed_size:
            raise TimelineIndexError("timeline index coverage gap")
        return value
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, TimelineIndexError):
            raise
        raise TimelineIndexError(str(exc)) from exc


def extend(source: Path | str, *, block_records: int = BLOCK_RECORDS,
           max_blocks: int = 1, validate_record=None) -> dict:
    """Append at most ``max_blocks`` complete blocks; never mutate raw history."""
    source = Path(source)
    try:
        value = load_valid(source)
    except TimelineIndexError:
        value = _empty(source, block_records)
    block_records = int(value.get("block_records", block_records))
    blocks = list(value["blocks"])
    line_number = (int(blocks[-1]["line_end"]) + 1) if blocks else 1
    with source.open("rb") as handle:
        handle.seek(int(value["indexed_size"]))
        for _ in range(max(0, int(max_blocks))):
            start = handle.tell()
            line_start = line_number
            epochs, count = [], 0
            while count < block_records:
                record_offset = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(record_offset)
                    break
                line_number += 1
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if validate_record is not None:
                    validate_record(record)
                timestamp = record.get("timestamp")
                if isinstance(timestamp, bool):
                    raise TimelineIndexError("boolean timeline timestamp")
                epoch = float(timestamp)
                if not math.isfinite(epoch):
                    raise TimelineIndexError("non-finite timeline timestamp")
                epochs.append(epoch)
                count += 1
            end = handle.tell()
            if count < block_records:
                handle.seek(start)
                break
            blocks.append({"byte_start": start, "byte_end": end,
                           "timestamp_min": min(epochs), "timestamp_max": max(epochs),
                           "record_count": count, "line_start": line_start,
                           "line_end": line_number - 1})
    stat = source.stat()
    indexed_size = blocks[-1]["byte_end"] if blocks else 0
    head, tail = _edge_hash(source, indexed_size)
    result = {"schema_version": SCHEMA_VERSION, "index_kind": INDEX_KIND,
        "source_filename": source.name, "source_device": stat.st_dev,
        "source_inode": stat.st_ino, "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns, "indexed_size": indexed_size,
        "block_records": block_records, "timestamp_field": "timestamp",
        "timestamp_encoding": "unix_seconds", "prefix_head_sha256": head,
        "prefix_tail_sha256": tail, "blocks": blocks}
    _atomic_write(index_path(source), result)
    return result


def rebuild(source: Path | str, *, block_records: int = BLOCK_RECORDS,
            validate_record=None) -> dict:
    try:
        index_path(source).unlink()
    except FileNotFoundError:
        pass
    return extend(source, block_records=block_records, max_blocks=10_000_000,
                  validate_record=validate_record)


def select_ranges(source: Path | str, start_epoch: float, end_epoch: float) -> tuple:
    """Select overlapping confirmed blocks plus a hard-bounded open suffix."""
    source = Path(source)
    value = load_valid(source)
    selected = [block for block in value["blocks"]
                if float(block["timestamp_max"]) >= start_epoch
                and float(block["timestamp_min"]) <= end_epoch]
    ranges = [{"byte_start": int(block["byte_start"]),
               "byte_end": int(block["byte_end"]),
               "line_start": int(block["line_start"]), "open_suffix": False}
              for block in selected]
    suffix_start, size = int(value["indexed_size"]), source.stat().st_size
    suffix_bytes = size - suffix_start
    if suffix_bytes > OPEN_SUFFIX_MAX_BYTES:
        raise TimelineIndexError("timeline open suffix exceeds bounded read limit")
    if suffix_bytes:
        ranges.append({"byte_start": suffix_start, "byte_end": size,
                       "line_start": ((int(value["blocks"][-1]["line_end"]) + 1)
                                      if value["blocks"] else 1),
                       "open_suffix": True})
    return ranges, {"index_present": True, "index_valid": True,
        "selected_blocks": len(selected),
        "selected_bytes": sum(item["byte_end"] - item["byte_start"]
                              for item in ranges),
        "open_suffix_bytes": suffix_bytes, "read_mode": "timeline_block_index"}


class TimelineIndexWorker(threading.Thread):
    """Low-priority, bounded background maintenance for one timeline source."""

    def __init__(self, source, *, validate_record=None, interval_seconds=1.0,
                 logger=None):
        super().__init__(name="guardian-timeline-index", daemon=True)
        self.source = Path(source)
        self.validate_record = validate_record
        self.interval_seconds = float(interval_seconds)
        self.stop_event = threading.Event()
        self.logger = logger or logging.getLogger("guardian_battery.timeline_index")
        self.last_error = None
        self.last_result = None

    def run(self):
        while not self.stop_event.is_set():
            progressed = False
            try:
                if self.source.is_file():
                    before = 0
                    try:
                        before = int(load_valid(self.source)["indexed_size"])
                    except TimelineIndexError:
                        pass
                    value = extend(self.source, max_blocks=1,
                                   validate_record=self.validate_record)
                    after = int(value["indexed_size"])
                    progressed = after > before
                    self.last_result = {"indexed_size": after,
                                        "blocks": len(value["blocks"])}
                    self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.logger.warning("Timeline index maintenance failed: %s", exc)
            self.stop_event.wait(0.01 if progressed else self.interval_seconds)

    def stop(self, timeout=2.0):
        self.stop_event.set()
        self.join(timeout)
        return not self.is_alive()
