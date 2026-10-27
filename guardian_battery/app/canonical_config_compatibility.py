"""Bounded verification of historical configuration prefixes; no migration."""
from datetime import datetime
import hashlib
import json


def matches_historical_config(path, expected, timestamp_to):
    """Later appends cannot invalidate an unchanged, earlier configuration prefix."""
    try:
        if not expected:
            return not path.exists()
        size = expected["size"]
        if type(size) is not int or not 0 < size <= 65536:
            return False
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if not size <= len(raw) <= 65536:
            return False
        prefix = raw[:size]
        if hashlib.sha256(prefix).hexdigest() != expected.get("sha256"):
            return False
        if len(raw) == size:
            return True
        if not prefix.endswith(b"\n"):
            return False
        end = datetime.fromisoformat(timestamp_to.replace("Z", "+00:00"))
        if end.tzinfo is None:
            return False
        for line in raw[size:].splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            stamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            if (record.get("schema_version") != 1 or
                    not isinstance(record.get("parameters"), dict) or
                    not isinstance(record.get("config_id"), str) or
                    stamp.tzinfo is None or stamp <= end):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
