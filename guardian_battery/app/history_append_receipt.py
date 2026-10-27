"""Disposable append provenance, never a substitute for authoritative samples.

A writer checks the previous receipt before appending. Any external modification,
rotation or truncation starts a new generation; readers reject stale projections.
No content scan or historical work runs on the acquisition path.
"""
import json
import os
from pathlib import Path
import uuid


def _state(path):
    stat = Path(path).stat()
    return {"device": stat.st_dev, "inode": stat.st_ino,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def current_receipt(path):
    try:
        with Path(str(path)+".append.json").open() as handle:
            raw=handle.read(65537)
        if len(raw)>65536: return None
        value = json.loads(raw)
        if value.get("schema_version") != 1 or not value.get("generation"):
            return None
        return value if all(value.get(k)==v for k,v in _state(path).items()) else None
    except (OSError, ValueError, TypeError):
        return None


def before_append(path):
    receipt = current_receipt(path)
    return receipt["generation"] if receipt else str(uuid.uuid4())


def after_append(path, generation):
    """Best effort: failure only removes projection reuse, never raw persistence."""
    try:
        target = Path(str(path)+".append.json")
        temporary = target.with_suffix(target.suffix+".tmp")
        temporary.write_text(json.dumps({"schema_version":1, "generation":generation,
                                         **_state(path)}, separators=(",", ":")))
        os.replace(temporary, target)
    except OSError:
        pass


def signature(path):
    result = _state(path)
    receipt = current_receipt(path)
    if receipt:
        result["append_generation"] = receipt["generation"]
    return result


def matches_prefix(path, expected):
    """Accept exact source or an append chain proven by its writer receipts."""
    try:
        actual = _state(path)
        if expected.get("inode") is not None and any(actual[k] != expected.get(k) for k in ("device", "inode")):
            return False
        if actual["size"]==expected.get("size") and actual["mtime_ns"]==expected.get("mtime_ns"):
            return True
        receipt = current_receipt(path)
        return bool(receipt and expected.get("append_generation") == receipt["generation"]
                    and actual["size"] >= expected.get("size", float("inf")))
    except (OSError, TypeError):
        return False
