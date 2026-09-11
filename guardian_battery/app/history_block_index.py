"""Disposable block-range indexes for append-only Guardian JSONL history."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_BLOCK_RECORDS = 256
SIGNATURE_BYTES = 4096


class BlockIndexError(ValueError):
    pass


def index_path(source: Path) -> Path:
    return Path(str(source) + ".idx")


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


def _epoch(record: dict, timestamp_field: str, iso_timestamp: bool) -> float:
    value = record[timestamp_field]
    if iso_timestamp:
        from datetime import datetime
        result = datetime.fromisoformat(value).timestamp()
        if not math.isfinite(result):
            raise ValueError("non-finite timestamp")
        return result
    if isinstance(value, bool):
        raise ValueError("boolean timestamp")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite timestamp")
    return result


def build_index(source: Path, *, timestamp_field: str, iso_timestamp: bool,
                block_records: int = DEFAULT_BLOCK_RECORDS, complete: bool = True,
                validate_record=None) -> dict:
    """Rebuild one index atomically; callers control when this bounded unit runs."""
    source = Path(source)
    blocks = []
    with source.open("rb") as handle:
        while True:
            start = handle.tell(); epochs = []; count = 0
            while count < block_records:
                raw = handle.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                record = json.loads(raw)
                if validate_record is not None:
                    validate_record(record)
                epochs.append(_epoch(record, timestamp_field, iso_timestamp)); count += 1
            end = handle.tell()
            if not count:
                break
            if count < block_records and not complete:
                break
            blocks.append({"start_offset": start, "end_offset": end,
                           "min_timestamp": min(epochs), "max_timestamp": max(epochs),
                           "record_count": count})
            if end == source.stat().st_size:
                break
    indexed_size = blocks[-1]["end_offset"] if blocks else 0
    stat = source.stat()
    head_hash, tail_hash = _edge_hash(source, indexed_size)
    value = {
        "schema_version": SCHEMA_VERSION,
        "source_filename": source.name,
        "source_device": stat.st_dev,
        "source_inode": stat.st_ino,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "indexed_size": indexed_size,
        "complete": bool(complete and indexed_size == stat.st_size),
        "block_records": int(block_records),
        "timestamp_field": timestamp_field,
        "timestamp_encoding": "iso8601" if iso_timestamp else "unix_seconds",
        "prefix_head_sha256": head_hash,
        "prefix_tail_sha256": tail_hash,
        "blocks": blocks,
    }
    _atomic_write(index_path(source), value)
    return value


def extend_open_index(source: Path, *, timestamp_field: str, iso_timestamp: bool,
                      block_records: int = DEFAULT_BLOCK_RECORDS,
                      validate_record=None) -> dict:
    """Index at most one newly completed block and leave the rest as suffix."""
    source = Path(source); stat = source.stat()
    try:
        value = _load_valid(source, timestamp_field=timestamp_field,
                            iso_timestamp=iso_timestamp)
        if value.get("complete"):
            return value
    except BlockIndexError:
        value = {"blocks": [], "indexed_size": 0}
    start = int(value["indexed_size"]); epochs = []; count = 0
    with source.open("rb") as handle:
        handle.seek(start)
        while count < block_records:
            raw = handle.readline()
            if not raw:
                break
            if not raw.strip():
                continue
            record = json.loads(raw)
            if validate_record is not None:
                validate_record(record)
            epochs.append(_epoch(record, timestamp_field, iso_timestamp)); count += 1
        end = handle.tell()
    if count < block_records:
        return value
    blocks = list(value["blocks"])
    blocks.append({"start_offset": start, "end_offset": end,
                   "min_timestamp": min(epochs), "max_timestamp": max(epochs),
                   "record_count": count})
    head_hash, tail_hash = _edge_hash(source, end)
    next_value = {"schema_version": SCHEMA_VERSION,
        "source_filename": source.name, "source_device": stat.st_dev,
        "source_inode": stat.st_ino, "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns, "indexed_size": end,
        "complete": False, "block_records": int(block_records),
        "timestamp_field": timestamp_field,
        "timestamp_encoding": "iso8601" if iso_timestamp else "unix_seconds",
        "prefix_head_sha256": head_hash, "prefix_tail_sha256": tail_hash,
        "blocks": blocks}
    _atomic_write(index_path(source), next_value)
    return next_value


def _load_valid(source: Path, *, timestamp_field: str, iso_timestamp: bool) -> dict:
    source = Path(source); path = index_path(source)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        stat = source.stat()
        expected_encoding = "iso8601" if iso_timestamp else "unix_seconds"
        if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
                or value.get("source_filename") != source.name
                or value.get("source_device") != stat.st_dev
                or value.get("source_inode") != stat.st_ino
                or value.get("timestamp_field") != timestamp_field
                or value.get("timestamp_encoding") != expected_encoding):
            raise BlockIndexError("index contract mismatch")
        indexed_size = int(value["indexed_size"])
        if indexed_size < 0 or indexed_size > stat.st_size:
            raise BlockIndexError("indexed source was truncated")
        if value.get("complete") and (indexed_size != stat.st_size
                or int(value.get("source_size", -1)) != stat.st_size
                or int(value.get("source_mtime_ns", -1)) != stat.st_mtime_ns):
            raise BlockIndexError("complete source changed")
        if (not value.get("complete") and stat.st_size == int(value.get("source_size", -1))
                and stat.st_mtime_ns != int(value.get("source_mtime_ns", -1))):
            raise BlockIndexError("open source changed without append")
        head_hash, tail_hash = _edge_hash(source, indexed_size)
        if (head_hash != value.get("prefix_head_sha256")
                or tail_hash != value.get("prefix_tail_sha256")):
            raise BlockIndexError("indexed prefix changed")
        cursor = 0
        for block in value.get("blocks", []):
            start = int(block["start_offset"]); end = int(block["end_offset"])
            if start != cursor or end <= start or end > indexed_size:
                raise BlockIndexError("invalid block offsets")
            minimum = float(block["min_timestamp"]); maximum = float(block["max_timestamp"])
            if (int(block["record_count"]) <= 0 or not math.isfinite(minimum)
                    or not math.isfinite(maximum) or minimum > maximum):
                raise BlockIndexError("invalid block range")
            cursor = end
        if cursor != indexed_size:
            raise BlockIndexError("index coverage gap")
        return value
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, BlockIndexError):
            raise
        raise BlockIndexError(str(exc)) from exc


def selected_ranges(source: Path, start_epoch: float, end_epoch: float, *,
                    timestamp_field: str, iso_timestamp: bool) -> tuple[list[tuple[int, int]], dict]:
    """Return safe source-order byte ranges, or raise for caller Full Scan fallback."""
    source = Path(source)
    value = _load_valid(source, timestamp_field=timestamp_field,
                        iso_timestamp=iso_timestamp)
    ranges = []
    for block in value["blocks"]:
        if (float(block["max_timestamp"]) >= start_epoch
                and float(block["min_timestamp"]) <= end_epoch):
            ranges.append((int(block["start_offset"]), int(block["end_offset"])))
    size = source.stat().st_size
    indexed_size = int(value["indexed_size"])
    if indexed_size < size:
        ranges.append((indexed_size, size))
    merged = []
    for start, end in ranges:
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    selected = sum(end - start for start, end in merged)
    return merged, {"mode": "block_index", "skipped_bytes": size - selected,
                    "indexed_size": indexed_size}


def index_signature(source: Path):
    path = index_path(source)
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns
    except OSError:
        return None
