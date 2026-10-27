"""Recoverable publication of derived results and their processing frontier.

One writer owns each intent. Recovery finishes the saved unit without rereading
raw history. The frontier is always the last operation. No raw files belong here.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

MAX_INTENT = 64 * 1024 * 1024

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)

def atomic(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".resume-tmp")
    with tmp.open("wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path); sync_dir(path.parent)

def replace(path, data):
    return {"kind": "replace", "path": str(Path(path).absolute()),
            "data": base64.b64encode(data).decode()}

def append(path, data):
    item = replace(path, data); item["kind"] = "append"
    item["offset"] = Path(path).stat().st_size if Path(path).exists() else 0
    return item

def recover(intent, *, fault=lambda point: None):
    intent = Path(intent)
    if not intent.exists(): return False
    with intent.open("rb") as handle: raw = handle.read(MAX_INTENT + 1)
    if len(raw) > MAX_INTENT: raise ValueError("resume publication exceeds budget")
    envelope = json.loads(raw); operations = envelope["operations"]
    if hashlib.sha256(encoded(operations)).hexdigest() != envelope["sha256"]:
        raise ValueError("resume publication checksum mismatch")
    for index, item in enumerate(operations):
        path = Path(item["path"])
        if not path.is_absolute() or not path.is_relative_to(intent.parent.parent):
            raise ValueError("resume publication target outside derived store")
        if item["kind"] == "display_window":
            from display_window_store import publish
            publish(path, item["records"], item["revision"])
        elif item["kind"] == "canonical_window":
            from canonical_window_store import publish
            publish(path, item["artifact"])
        else:
            data = base64.b64decode(item["data"], validate=True)
            if item["kind"] == "replace": atomic(path, data)
            elif item["kind"] == "append":
                path.parent.mkdir(parents=True, exist_ok=True)
                start = item["offset"]
                with path.open("r+b" if path.exists() else "w+b") as handle:
                    handle.seek(0, 2); end = handle.tell()
                    if not start <= end <= start + len(data):
                        raise ValueError("derived append frontier changed")
                    handle.seek(start)
                    if handle.read(end-start) != data[:end-start]:
                        raise ValueError("derived append prefix changed")
                    handle.seek(end); handle.write(data[end-start:])
                    handle.flush(); os.fsync(handle.fileno())
                sync_dir(path.parent)
            else: raise ValueError("unknown resume operation")
        fault("after_operation_" + str(index))
    intent.unlink(); sync_dir(intent.parent)
    return True

def publish(intent, operations, *, fault=lambda point: None):
    intent = Path(intent)
    if intent.exists(): raise ValueError("unfinished resume publication requires recovery")
    raw = encoded({"operations": operations,
                   "sha256": hashlib.sha256(encoded(operations)).hexdigest()})
    if len(raw) > MAX_INTENT: raise ValueError("resume publication exceeds budget")
    atomic(intent, raw); fault("after_intent")
    recover(intent, fault=fault)
