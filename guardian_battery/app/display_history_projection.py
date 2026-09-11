"""Rebuildable multi-resolution data for interactive history displays.

This module deliberately has no dependency on the production History API.  Raw
and full-resolution history remain authoritative; every artifact written here
is disposable derived data.
"""
from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


DISPLAY_PROJECTION_SCHEMA_VERSION = 1
AGGREGATION_ALGORITHM_VERSION = "guardian_display_bucket_v1"
RESOLUTIONS = {"1m": 60, "5m": 300, "15m": 900, "60m": 3600}
SOURCE_SCHEMA_VERSION = 1
DEFAULT_DISPLAY_HISTORY_DIR = Path("/share/guardian_battery/display_history")
CHANNEL_VALUE_FIELDS = ("first_value", "first_timestamp", "last_value",
                        "last_timestamp", "min_value", "min_timestamp",
                        "max_value", "max_timestamp", "mean", "sample_count")
TIMESTAMP_FIELDS = frozenset({"first_timestamp", "last_timestamp",
                              "min_timestamp", "max_timestamp"})


class DisplayProjectionError(ValueError):
    pass


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    os.replace(temporary, path)


def _deterministic_gzip_lines(path: Path, records: list[dict]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6,
                           mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False,
                                            separators=(",", ":")) + "\n")
    os.replace(temporary, path)


def _source_signature(path: Path | None, *, content_hash=True) -> dict | None:
    if path is None or not path.is_file():
        return None
    stat = path.stat()
    result = {"filename": path.name, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "source_schema_version": SOURCE_SCHEMA_VERSION}
    if content_hash:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
    else:
        result["sha256"] = None
    return result


def _bucket_start(epoch: float, seconds: int) -> int:
    return int(epoch // seconds) * seconds


def _sample_key(timestamp: str, source_order: int) -> tuple[float, int]:
    return datetime.fromisoformat(timestamp).timestamp(), source_order


def _new_bucket(channel: dict, resolution: str, timestamp: str, value: float,
                source_order: int) -> dict:
    epoch, _ = _sample_key(timestamp, source_order)
    start = _bucket_start(epoch, RESOLUTIONS[resolution])
    return {**channel, "resolution": resolution,
            "bucket_start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "bucket_end": datetime.fromtimestamp(
                start + RESOLUTIONS[resolution], timezone.utc).isoformat(),
            "first_value": value, "first_timestamp": timestamp,
            "last_value": value, "last_timestamp": timestamp,
            "min_value": value, "min_timestamp": timestamp,
            "max_value": value, "max_timestamp": timestamp,
            "mean": value, "sample_count": 1,
            "_sum": value, "_first_order": source_order,
            "_last_order": source_order, "_min_order": source_order,
            "_max_order": source_order}


def _add(bucket: dict, timestamp: str, value: float, source_order: int) -> None:
    key = _sample_key(timestamp, source_order)
    first_key = _sample_key(bucket["first_timestamp"], bucket["_first_order"])
    last_key = _sample_key(bucket["last_timestamp"], bucket["_last_order"])
    if key < first_key:
        bucket.update(first_value=value, first_timestamp=timestamp,
                      _first_order=source_order)
    if key >= last_key:
        bucket.update(last_value=value, last_timestamp=timestamp,
                      _last_order=source_order)
    min_key = _sample_key(bucket["min_timestamp"], bucket["_min_order"])
    max_key = _sample_key(bucket["max_timestamp"], bucket["_max_order"])
    if value < bucket["min_value"] or (value == bucket["min_value"] and key < min_key):
        bucket.update(min_value=value, min_timestamp=timestamp, _min_order=source_order)
    if value > bucket["max_value"] or (value == bucket["max_value"] and key < max_key):
        bucket.update(max_value=value, max_timestamp=timestamp, _max_order=source_order)
    bucket["_sum"] += value
    bucket["sample_count"] += 1
    bucket["mean"] = bucket["_sum"] / bucket["sample_count"]


def _public(bucket: dict) -> dict:
    return {"display_projection_schema_version": DISPLAY_PROJECTION_SCHEMA_VERSION,
            "aggregation_algorithm_version": AGGREGATION_ALGORITHM_VERSION,
            **{key: value for key, value in bucket.items() if not key.startswith("_")},
            "quality": "observed", "derived": True,
            "authoritative": False}


def _pack(records: list[dict]) -> list[dict]:
    """Deduplicate bucket/provenance keys while preserving every channel value."""
    groups = {}
    for item in records:
        key = (item["resolution"], item["bucket_start"], item["bucket_end"],
               item["source"], item.get("module_position"),
               item.get("physical_serial"), item.get("identity_quality"))
        packed = groups.setdefault(key, {
            "display_projection_schema_version": DISPLAY_PROJECTION_SCHEMA_VERSION,
            "aggregation_algorithm_version": AGGREGATION_ALGORITHM_VERSION,
            "resolution": item["resolution"], "bucket_start": item["bucket_start"],
            "bucket_end": item["bucket_end"], "source": item["source"],
            "module_position": item.get("module_position"),
            "physical_serial": item.get("physical_serial"),
            "identity_quality": item.get("identity_quality"),
            "channels": {}, "quality": "observed", "derived": True,
            "authoritative": False})
        name = item["metric"] + (f":{item['cell_number']}"
                                 if item.get("cell_number") else "")
        start_epoch = datetime.fromisoformat(item["bucket_start"]).timestamp()
        packed["channels"][name] = [
            datetime.fromisoformat(item[field]).timestamp() - start_epoch
            if field in TIMESTAMP_FIELDS else item[field]
            for field in CHANNEL_VALUE_FIELDS]
    return sorted(groups.values(), key=lambda item: (
        item["bucket_start"], item["source"], item.get("physical_serial") or "",
        item.get("module_position") or 0))


def _unpack(records: list[dict]) -> list[dict]:
    expanded = []
    for record in records:
        for name, values in record["channels"].items():
            metric, _, cell = name.partition(":")
            start_epoch = datetime.fromisoformat(record["bucket_start"]).timestamp()
            decoded = [datetime.fromtimestamp(start_epoch + value,
                       timezone.utc).isoformat() if field in TIMESTAMP_FIELDS else value
                       for field, value in zip(CHANNEL_VALUE_FIELDS, values)]
            expanded.append({key: value for key, value in record.items()
                             if key != "channels"} | {"metric": metric,
                             **({"cell_number": int(cell)} if cell else {}),
                             **dict(zip(CHANNEL_VALUE_FIELDS, decoded))})
    return expanded


def _channels(record: dict, source: str) -> list[tuple[dict, str, float]]:
    if record.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise DisplayProjectionError("source schema mismatch")
    if source == "cell":
        module = int(record["module"])
        serial = record.get("module_serial")
        base = {"source": "cell_history", "module_position": module,
                "physical_serial": serial,
                "identity_quality": "physical_serial" if serial else "position_only"}
        result = [(base, "soc", float(record["soc_percent"])),
                  (base, "current", float(record["current_a"]))]
        for metric, field in (("cell_voltage", "voltages_mv"),
                              ("cell_temperature", "temperatures_c")):
            values = record[field]
            for cell, value in enumerate(values, 1):
                result.append(({**base, "cell_number": cell}, metric, float(value)))
        return result
    if (record.get("record_type") == "hycube_history_projection"
            and "battery_capacity" in record):
        value = record["battery_capacity"]
        timestamp_field = "received_at"
    elif record.get("record_type") == "hycube_system_observation":
        value = record["BatteryCapacity"]
        timestamp_field = "received_at"
    else:
        raise DisplayProjectionError("unsupported Hycube record")
    return [({"source": "hycube", "timestamp_field": timestamp_field},
             "hycube_soc", float(value))]


def _timestamp(record: dict, source: str) -> str:
    if source == "cell":
        return datetime.fromtimestamp(float(record["timestamp"]), timezone.utc).isoformat()
    return datetime.fromisoformat(record["received_at"]).astimezone(timezone.utc).isoformat()


def _channel_key(channel: dict, metric: str, resolution: str, start: int) -> str:
    identity = channel.get("physical_serial") or f"position:{channel.get('module_position')}"
    return "|".join(map(str, (resolution, start, channel["source"], identity,
                              channel.get("cell_number", 0), metric)))


class DisplayHistoryProjection:
    """One-pass, chunked builder for independent UTC days."""

    def __init__(self, cell_history_directory, hycube_history_directory, output_directory,
                 *, max_records=1000, max_bytes=4 * 1024 * 1024, clock=time.time):
        self.cell_directory = Path(cell_history_directory)
        self.hycube_directory = Path(hycube_history_directory)
        self.output_directory = Path(output_directory)
        self.max_records = max(1, int(max_records))
        self.max_bytes = max(1, int(max_bytes))
        self.clock = clock
        self._lock = threading.Lock()
        self._status = {"enabled": True, "state": "idle", "days": 0,
                        "complete_days": 0, "open_days": 0, "invalid_days": 0,
                        "storage_bytes": 0, "last_success": None,
                        "failure_count": 0}
        self._rebuild = {"status": "idle", "days_total": 0, "days_completed": 0,
                         "current_file": None, "records": 0, "bytes": 0,
                         "buckets": 0, "errors": 0}
        self.refresh_status(validate_contents=False)

    def _state_path(self, day):
        return self.output_directory / ".state" / f"{day}.json"

    def _data_path(self, resolution, day, building=False):
        suffix = ".jsonl.gz.building" if building else ".jsonl.gz"
        return self.output_directory / resolution / f"{day}{suffix}"

    def _meta_path(self, resolution, day, building=False):
        suffix = ".meta.json.building" if building else ".meta.json"
        return self.output_directory / resolution / f"{day}{suffix}"

    def _sources(self, day):
        candidates = (("cell", self.cell_directory / f"{day}.jsonl"),
                      ("hycube", self.hycube_directory / f"{day}.jsonl"))
        return [(kind, path) for kind, path in candidates if path.is_file()]

    def _load_state(self, day):
        fresh = {"display_projection_schema_version": DISPLAY_PROJECTION_SCHEMA_VERSION,
                 "aggregation_algorithm_version": AGGREGATION_ALGORITHM_VERSION,
                 "day": day, "offsets": {}, "orders": {}, "buckets": {},
                 "watermarks": {}, "closed_before": {},
                 "records": 0, "bytes": 0}
        try:
            value = json.loads(self._state_path(day).read_text(encoding="utf-8"))
            if (value.get("display_projection_schema_version") !=
                    DISPLAY_PROJECTION_SCHEMA_VERSION or
                    value.get("aggregation_algorithm_version") !=
                    AGGREGATION_ALGORITHM_VERSION):
                return fresh
            return value
        except FileNotFoundError:
            return fresh

    def process_chunk(self, day: str, *, finalize: bool | None = None) -> dict:
        """Read one bounded chunk. Repeated calls resume from persisted offsets."""
        state = self._load_state(day)
        sources = self._sources(day)
        if not sources:
            raise DisplayProjectionError("no source history for day")
        processed = consumed = 0
        for kind, path in sources:
            offset = int(state["offsets"].get(kind, 0))
            order = int(state["orders"].get(kind, 0))
            with path.open("rb") as handle:
                handle.seek(offset)
                while processed < self.max_records and consumed < self.max_bytes:
                    raw = handle.readline()
                    if not raw:
                        break
                    consumed += len(raw); processed += 1; order += 1
                    try:
                        record = json.loads(raw)
                        timestamp = _timestamp(record, kind)
                        epoch = datetime.fromisoformat(timestamp).timestamp()
                        if datetime.fromtimestamp(epoch, timezone.utc).date().isoformat() != day:
                            raise DisplayProjectionError("record outside UTC day")
                        state["watermarks"][kind] = max(
                            epoch, float(state["watermarks"].get(kind, epoch)))
                        for channel, metric, value in _channels(record, kind):
                            for resolution, seconds in RESOLUTIONS.items():
                                start = _bucket_start(epoch, seconds)
                                closed_before = float(state["closed_before"].get(
                                    f"{kind}:{resolution}", float("-inf")))
                                if start < closed_before:
                                    raise DisplayProjectionError(
                                        "late source record requires day rebuild")
                                key = _channel_key(channel, metric, resolution, start)
                                bucket = state["buckets"].get(key)
                                if bucket is None:
                                    state["buckets"][key] = _new_bucket(
                                        {**channel, "metric": metric}, resolution,
                                        timestamp, value, order)
                                else:
                                    _add(bucket, timestamp, value, order)
                    except DisplayProjectionError:
                        raise
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError,
                            IndexError) as exc:
                        raise DisplayProjectionError(f"invalid {kind} source record") from exc
                state["offsets"][kind] = handle.tell()
                state["orders"][kind] = order
            if processed >= self.max_records or consumed >= self.max_bytes:
                break
        state["records"] += processed; state["bytes"] += consumed
        _atomic_json(self._state_path(day), state)
        caught_up = all(int(state["offsets"].get(kind, 0)) == path.stat().st_size
                        for kind, path in sources)
        current_day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        should_finalize = caught_up and (finalize if finalize is not None else day < current_day)
        if should_finalize:
            self._publish(day, state, sources, complete=True)
        elif caught_up:
            self._publish_open(day, state, sources)
            _atomic_json(self._state_path(day), state)
        with self._lock:
            self._status.update(state="available", last_success=self.clock())
        self.refresh_status(validate_contents=False)
        return {"day": day, "records_processed": processed,
                "bytes_processed": consumed, "caught_up": caught_up,
                "complete": bool(should_finalize),
                "bucket_count": len(state["buckets"])}

    def _publish(self, day, state, sources, *, complete):
        signatures = {kind: _source_signature(path, content_hash=complete)
                      for kind, path in sources}
        by_resolution = {resolution: [] for resolution in RESOLUTIONS}
        for bucket in state["buckets"].values():
            by_resolution[bucket["resolution"]].append(_public(bucket))
        built_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        for resolution, records in by_resolution.items():
            records.sort(key=lambda item: (item["bucket_start"], item["source"],
                item.get("physical_serial") or "", item.get("module_position") or 0,
                item.get("cell_number") or 0, item["metric"]))
            channel_count = len(records); records = _pack(records)
            data = self._data_path(resolution, day, building=True)
            data.parent.mkdir(parents=True, exist_ok=True)
            _deterministic_gzip_lines(data, records)
            metadata = {"display_projection_schema_version":
                        DISPLAY_PROJECTION_SCHEMA_VERSION,
                        "aggregation_algorithm_version": AGGREGATION_ALGORITHM_VERSION,
                        "config_revision": None, "phase_version": None,
                        "status": "complete" if complete else "open", "day": day,
                        "resolution": resolution, "source_signatures": signatures,
                        "bucket_count": len(records),
                        "channel_bucket_count": channel_count,
                        "channel_value_fields": list(CHANNEL_VALUE_FIELDS),
                        "channel_timestamp_encoding": "seconds_from_bucket_start",
                        "first_bucket": records[0]["bucket_start"] if records else None,
                        "last_bucket": records[-1]["bucket_start"] if records else None,
                        "build_timestamp": built_at, "authoritative": False}
            _atomic_json(self._meta_path(resolution, day, building=True), metadata)
            os.replace(data, self._data_path(resolution, day))
            os.replace(self._meta_path(resolution, day, building=True),
                       self._meta_path(resolution, day))
        if complete:
            try: self._state_path(day).unlink()
            except FileNotFoundError: pass

    def _publish_open(self, day, state, sources):
        """Append immutable closed buckets; retain only open buckets in state."""
        signatures = {kind: _source_signature(path, content_hash=False)
                      for kind, path in sources}
        source_kind = {"cell_history": "cell", "hycube": "hycube"}
        closed = {resolution: [] for resolution in RESOLUTIONS}
        retained = {}
        for key, bucket in state["buckets"].items():
            kind = source_kind[bucket["source"]]
            watermark = state["watermarks"].get(kind)
            end = datetime.fromisoformat(bucket["bucket_end"]).timestamp()
            if watermark is not None and end <= watermark:
                closed[bucket["resolution"]].append(_public(bucket))
                marker = f"{kind}:{bucket['resolution']}"
                state["closed_before"][marker] = max(
                    end, float(state["closed_before"].get(marker, end)))
            else:
                retained[key] = bucket
        state["buckets"] = retained
        built_at = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        for resolution, records in closed.items():
            path = self._data_path(resolution, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            channel_count = len(records); records = _pack(records)
            if records:
                with gzip.open(path, "at", encoding="utf-8", compresslevel=6) as handle:
                    for record in records:
                        handle.write(json.dumps(record, ensure_ascii=False,
                                                separators=(",", ":")) + "\n")
            previous = 0
            try:
                previous = json.loads(self._meta_path(
                    resolution, day).read_text())["bucket_count"]
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                pass
            total = previous + len(records)
            metadata = {"display_projection_schema_version":
                        DISPLAY_PROJECTION_SCHEMA_VERSION,
                        "aggregation_algorithm_version": AGGREGATION_ALGORITHM_VERSION,
                        "config_revision": None, "phase_version": None,
                        "status": "open", "day": day, "resolution": resolution,
                        "source_signatures": signatures, "bucket_count": total,
                        "channel_bucket_count": channel_count,
                        "channel_value_fields": list(CHANNEL_VALUE_FIELDS),
                        "channel_timestamp_encoding": "seconds_from_bucket_start",
                        "open_bucket_count": sum(bucket["resolution"] == resolution
                                                 for bucket in retained.values()),
                        "first_bucket": None, "last_bucket": None,
                        "build_timestamp": built_at, "authoritative": False}
            _atomic_json(self._meta_path(resolution, day), metadata)

    def build_day(self, day: str) -> dict:
        """Finish one independent day using bounded resumable chunks."""
        totals = {"records": 0, "bytes": 0, "buckets": 0}
        while True:
            result = self.process_chunk(day, finalize=True)
            totals["records"] += result["records_processed"]
            totals["bytes"] += result["bytes_processed"]
            totals["buckets"] = result["bucket_count"]
            if result["complete"]:
                return totals

    def reset_open_day(self, day: str) -> None:
        """Discard only rebuildable current-day artifacts before crash recovery."""
        for resolution in RESOLUTIONS:
            for path in (self._data_path(resolution, day),
                         self._meta_path(resolution, day)):
                try: path.unlink()
                except FileNotFoundError: pass
        try: self._state_path(day).unlink()
        except FileNotFoundError: pass
        self.refresh_status(validate_contents=False)

    def rebuild(self, *, include_current=False) -> dict:
        """Explicit synchronous maintenance action; no startup invocation."""
        days = sorted({path.stem for directory in (self.cell_directory,
                      self.hycube_directory) if directory.exists()
                       for path in directory.glob("*.jsonl")})
        current = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        if not include_current:
            days = [day for day in days if day < current]
        self._rebuild.update(status="running", days_total=len(days), days_completed=0,
                             current_file=None, records=0, bytes=0, buckets=0,
                             errors=0)
        for day in days:
            self._rebuild["current_file"] = day
            try:
                result = self.build_day(day)
                self._rebuild["days_completed"] += 1
                for key in ("records", "bytes", "buckets"):
                    self._rebuild[key] += result[key]
            except Exception:
                self._rebuild["errors"] += 1
                with self._lock:
                    self._status["failure_count"] += 1
        self._rebuild.update(status="completed", current_file=None)
        self.refresh_status(validate_contents=True)
        return dict(self._rebuild)

    def status(self):
        with self._lock:
            return {**self._status, "rebuild": dict(self._rebuild)}

    def refresh_status(self, *, validate_contents=True):
        days = set(); complete = open_days = invalid = storage = 0
        for resolution in RESOLUTIONS:
            directory = self.output_directory / resolution
            if not directory.exists():
                continue
            for meta_path in directory.glob("*.meta.json"):
                day = meta_path.name.removesuffix(".meta.json")
                path = self._data_path(resolution, day)
                if path.exists(): storage += path.stat().st_size
                days.add(day)
                try:
                    meta = json.loads(meta_path.read_text())
                    valid = (meta["display_projection_schema_version"] ==
                             DISPLAY_PROJECTION_SCHEMA_VERSION and
                             meta["aggregation_algorithm_version"] ==
                             AGGREGATION_ALGORITHM_VERSION)
                    if validate_contents:
                        valid = valid and meta["bucket_count"] == sum(1 for line in
                                (gzip.open(path, "rt", encoding="utf-8")
                                 if path.exists() else []) if line.strip())
                    for kind, signature in meta.get("source_signatures", {}).items():
                        source = (self.cell_directory if kind == "cell"
                                  else self.hycube_directory) / signature["filename"]
                        actual = _source_signature(source, content_hash=(
                            validate_contents and meta.get("status") == "complete"))
                        comparable = (actual if validate_contents else {
                            key: actual.get(key) for key in
                            ("filename", "size", "mtime_ns", "source_schema_version")})
                        expected = (signature if validate_contents else {
                            key: signature.get(key) for key in
                            ("filename", "size", "mtime_ns", "source_schema_version")})
                        if comparable != expected:
                            valid = False
                    if not valid: raise ValueError
                    if resolution == "1m":
                        if meta["status"] == "complete": complete += 1
                        else: open_days += 1
                except (OSError, EOFError, gzip.BadGzipFile, KeyError, TypeError,
                        ValueError, json.JSONDecodeError):
                    if resolution == "1m": invalid += 1
        with self._lock:
            self._status.update(days=len(days), complete_days=complete,
                                open_days=open_days, invalid_days=invalid,
                                storage_bytes=storage,
                                state="invalid" if invalid else self._status["state"])


def read_projection(directory, resolution, day) -> list[dict]:
    """Internal equality/benchmark reader; not wired to the History API."""
    path = Path(directory) / resolution / f"{day}.jsonl.gz"
    packed = ([json.loads(line) for line in gzip.open(
        path, "rt", encoding="utf-8") if line.strip()] if path.exists() else [])
    records = _unpack(packed)
    state_path = Path(directory) / ".state" / f"{day}.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        records.extend(_public(bucket) for bucket in state.get("buckets", {}).values()
                       if bucket["resolution"] == resolution)
    return sorted(records, key=lambda item: (item["bucket_start"], item["source"],
        item.get("physical_serial") or "", item.get("module_position") or 0,
        item.get("cell_number") or 0, item["metric"]))


class DisplayHistoryProjectionWorker:
    """Failure-isolated tailer; it never owns or calls an acquisition source."""

    def __init__(self, projection: DisplayHistoryProjection, *, interval_seconds=30.0):
        self.projection = projection
        self.interval_seconds = max(0.01, float(interval_seconds))
        self._stop = threading.Event(); self._rebuild = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive(): return False
        self._stop.clear(); self._thread = threading.Thread(
            target=self._run, name="guardian-display-history-projection", daemon=True)
        self._thread.start(); return True

    def request_historical_rebuild(self):
        if self._rebuild.is_set(): return False
        self._rebuild.set(); return True

    def status(self):
        return {**self.projection.status(),
                "worker_active": bool(self._thread and self._thread.is_alive()),
                "historical_rebuild_requested": self._rebuild.is_set()}

    def stop(self, timeout=5.0):
        self._stop.set(); self._rebuild.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not (self._thread and self._thread.is_alive())

    def _run(self):
        recovering = True
        while not self._stop.is_set():
            try:
                if self._rebuild.is_set():
                    self._rebuild.clear()
                    self.projection.rebuild(include_current=False)
                day = datetime.fromtimestamp(
                    self.projection.clock(), timezone.utc).date().isoformat()
                if recovering:
                    self.projection.reset_open_day(day)
                    recovering = False
                if self.projection._sources(day):
                    self.projection.process_chunk(day)
            except Exception:
                with self.projection._lock:
                    self.projection._status["failure_count"] += 1
                    self.projection._status["state"] = "error"
            self._stop.wait(self.interval_seconds)
