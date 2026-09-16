"""Disposable block index for append-only RS485 JSONL evidence."""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path

from history_block_index import BlockIndexError, SIGNATURE_BYTES, index_path
SCHEMA_VERSION = 1
INDEX_KIND = "guardian_rs485_history_block_index"
BLOCK_RECORDS = 512
OPEN_SUFFIX_MAX_BYTES = 2 * 1024 * 1024


def _epoch(value) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean timestamp")
    result = (float(value) if isinstance(value, (int, float))
              else datetime.fromisoformat(value).timestamp())
    if not math.isfinite(result):
        raise ValueError("non-finite timestamp")
    return result


def _edge_hash(source: Path, indexed_size: int) -> tuple[str, str]:
    with source.open("rb") as handle:
        head = handle.read(min(SIGNATURE_BYTES, indexed_size))
        handle.seek(max(0, indexed_size - SIGNATURE_BYTES))
        tail = handle.read(indexed_size - max(0, indexed_size - SIGNATURE_BYTES))
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()


def _atomic_write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    os.replace(temporary, path)


def _checkpoint(state: dict[int, dict]) -> dict[str, dict]:
    return {str(adr): {"physical_serial": item["physical_serial"],
                       "decode_source": item["decode_source"]}
            for adr, item in sorted(state.items())}


def _advance_identity(state: dict[int, dict], record: dict) -> None:
    # Local import keeps the evidence writer free to own the index lifecycle.
    from rs485_evidence import decode_identity_record
    identity = decode_identity_record(record)
    if identity is not None:
        state[int(identity["adr"])] = {
            "physical_serial": identity["serial_string"],
            "decode_source": identity["decode_source"],
        }


def _empty(source: Path, block_records: int) -> dict:
    stat = source.stat()
    empty_hash = hashlib.sha256(b"").hexdigest()
    return {"schema_version": SCHEMA_VERSION, "index_kind": INDEX_KIND,
            "source_filename": source.name, "source_device": stat.st_dev,
            "source_inode": stat.st_ino, "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns, "indexed_size": 0,
            "complete": False, "block_records": int(block_records),
            "timestamp_field": "timestamp",
            "timestamp_encoding": "iso8601_or_unix_seconds",
            "prefix_head_sha256": empty_hash, "prefix_tail_sha256": empty_hash,
            "blocks": []}


def load_valid(source: Path) -> dict:
    """Load an index only when its indexed source prefix is still authoritative."""
    source = Path(source)
    try:
        value = json.loads(index_path(source).read_text(encoding="utf-8"))
        stat = source.stat()
        if (value.get("schema_version") != SCHEMA_VERSION
                or value.get("index_kind") != INDEX_KIND
                or value.get("source_filename") != source.name
                or value.get("source_device") != stat.st_dev
                or value.get("source_inode") != stat.st_ino):
            raise BlockIndexError("RS485 index contract mismatch")
        indexed_size = int(value["indexed_size"])
        if int(value.get("block_records", 0)) <= 0:
            raise BlockIndexError("invalid RS485 block size")
        if indexed_size < 0 or indexed_size > stat.st_size:
            raise BlockIndexError("indexed RS485 source was truncated")
        if value.get("complete") and (indexed_size != stat.st_size
                or int(value.get("source_size", -1)) != stat.st_size
                or int(value.get("source_mtime_ns", -1)) != stat.st_mtime_ns):
            raise BlockIndexError("complete RS485 source changed")
        if (not value.get("complete") and stat.st_size == int(
                value.get("source_size", -1))
                and stat.st_mtime_ns != int(value.get("source_mtime_ns", -1))):
            raise BlockIndexError("open RS485 source changed without append")
        head, tail = _edge_hash(source, indexed_size)
        if (head != value.get("prefix_head_sha256")
                or tail != value.get("prefix_tail_sha256")):
            raise BlockIndexError("indexed RS485 prefix changed")
        cursor = 0
        for block in value.get("blocks", []):
            start, end = int(block["byte_start"]), int(block["byte_end"])
            minimum, maximum = (float(block["timestamp_min"]),
                                float(block["timestamp_max"]))
            checkpoint = block.get("identity_checkpoint")
            if (start != cursor or end <= start or end > indexed_size
                    or int(block["record_count"]) <= 0
                    or not math.isfinite(minimum) or not math.isfinite(maximum)
                    or minimum > maximum or not isinstance(checkpoint, dict)):
                raise BlockIndexError("invalid RS485 block")
            for adr, identity in checkpoint.items():
                int(adr)
                if (not isinstance(identity, dict)
                        or not isinstance(identity.get("physical_serial"), str)
                        or not isinstance(identity.get("decode_source"), str)):
                    raise BlockIndexError("invalid RS485 identity checkpoint")
            cursor = end
        if cursor != indexed_size:
            raise BlockIndexError("RS485 index coverage gap")
        return value
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, BlockIndexError):
            raise
        raise BlockIndexError(str(exc)) from exc


def _state_after_last_block(source: Path, value: dict) -> dict[int, dict]:
    if not value["blocks"]:
        return {}
    final = value["blocks"][-1]
    state = {int(adr): dict(identity) for adr, identity in
             final["identity_checkpoint"].items()}
    with source.open("rb") as handle:
        handle.seek(int(final["byte_start"]))
        while handle.tell() < int(final["byte_end"]):
            raw = handle.readline()
            if not raw:
                break
            try:
                _advance_identity(state, json.loads(raw))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    return state


def extend(source: Path, *, block_records: int = BLOCK_RECORDS,
           max_blocks: int = 1, close_source: bool = False) -> dict:
    """Append confirmed complete-line blocks without rebuilding confirmed blocks."""
    source = Path(source)
    try:
        value = load_valid(source)
    except BlockIndexError:
        value = _empty(source, block_records)
    if value.get("complete"):
        return value
    block_records = int(value.get("block_records", block_records))
    blocks = list(value["blocks"])
    state = _state_after_last_block(source, value)
    with source.open("rb") as handle:
        handle.seek(int(value["indexed_size"]))
        for _ in range(max(0, int(max_blocks))):
            start = handle.tell()
            checkpoint = _checkpoint(state)
            epochs, count = [], 0
            while count < int(block_records):
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    handle.seek(line_start)
                    break
                if not raw.strip():
                    continue
                record = json.loads(raw)
                epochs.append(_epoch(record["timestamp"]))
                _advance_identity(state, record)
                count += 1
            end = handle.tell()
            if not count or (count < int(block_records) and not close_source):
                handle.seek(start)
                break
            blocks.append({"byte_start": start, "byte_end": end,
                           "timestamp_min": min(epochs), "timestamp_max": max(epochs),
                           "record_count": count,
                           "identity_checkpoint": checkpoint})
            if count < int(block_records):
                break
    stat = source.stat()
    indexed_size = blocks[-1]["byte_end"] if blocks else 0
    head, tail = _edge_hash(source, indexed_size)
    result = {"schema_version": SCHEMA_VERSION, "index_kind": INDEX_KIND,
        "source_filename": source.name, "source_device": stat.st_dev,
        "source_inode": stat.st_ino, "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns, "indexed_size": indexed_size,
        "complete": bool(close_source and indexed_size == stat.st_size),
        "block_records": int(block_records), "timestamp_field": "timestamp",
        "timestamp_encoding": "iso8601_or_unix_seconds",
        "prefix_head_sha256": head, "prefix_tail_sha256": tail, "blocks": blocks}
    _atomic_write(index_path(source), result)
    return result


def rebuild(source: Path, *, block_records: int = BLOCK_RECORDS,
            complete: bool = True) -> dict:
    """Deterministically rebuild an index; never called by Research reads."""
    try:
        index_path(Path(source)).unlink()
    except FileNotFoundError:
        pass
    return extend(source, block_records=block_records, max_blocks=10_000_000,
                  close_source=complete)


def select_ranges(source: Path, start_epoch: float, end_epoch: float) -> tuple:
    """Select overlapping blocks and the bounded unindexed open suffix."""
    source = Path(source)
    value = load_valid(source)
    selected = [block for block in value["blocks"]
                if float(block["timestamp_max"]) >= start_epoch
                and float(block["timestamp_min"]) <= end_epoch]
    ranges = [{"byte_start": int(block["byte_start"]),
               "byte_end": int(block["byte_end"]),
               "identity_checkpoint": block["identity_checkpoint"],
               "open_suffix": False} for block in selected]
    suffix_start, size = int(value["indexed_size"]), source.stat().st_size
    suffix_bytes = size - suffix_start
    if suffix_bytes > OPEN_SUFFIX_MAX_BYTES:
        raise BlockIndexError("RS485 open suffix exceeds bounded read limit")
    if suffix_bytes:
        ranges.append({"byte_start": suffix_start, "byte_end": size,
                       "identity_checkpoint": _checkpoint(
                           _state_after_last_block(source, value)),
                       "open_suffix": True})
    return ranges, {"index_present": True, "index_valid": True,
        "selected_blocks": len(selected),
        "selected_bytes": sum(item["byte_end"] - item["byte_start"]
                              for item in ranges),
        "open_suffix_bytes": suffix_bytes, "read_mode": "rs485_block_index"}
