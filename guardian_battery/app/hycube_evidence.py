"""Isolated read-only Hycube system-evidence acquisition."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from hycube_projection import (DEFAULT_HYCUBE_PROJECTION_DIR,
                               HycubeProjectionStore, parse_projection_record,
                               projection_plan)
from history_block_index import index_signature, selected_ranges


LOG = logging.getLogger("guardian_battery.hycube_evidence")
SCHEMA_VERSION = 1
COLLECTOR_VERSION = "guardian-hycube-read-only-1"
FIELDS = ("BatteryPower", "BatteryCapacity", "GridPower", "HomePower",
          "solarPower", "ExternalPower", "Date2")
MAX_RESPONSE_BODY_BYTES = 1024 * 1024
DEFAULT_HYCUBE_HISTORY_DIR = Path("/share/guardian_battery/hycube_history")
DEFAULT_HYCUBE_POLICY_HISTORY_DIR = Path("/share/guardian_battery/hycube_policy_history")
POLICY_ENDPOINT = "/Bat/getCustomBat/"
POLICY_FIELDS = ("normalMode", "bufferMode", "emergency", "batProtection")
POLICY_SEMANTICS_VERSION = "hycube-custom-battery-zones-1"
DEFAULT_POLICY_POLL_INTERVAL_SECONDS = 300.0


class HycubeHistoryError(RuntimeError):
    pass


class _CapacitySourceError(ValueError):
    def __init__(self, message, counts):
        super().__init__(message)
        self.counts = counts


def _read_capacity_source(path, offset, is_projection, byte_ranges=None):
    items = []; lines = parsed = invalid = byte_count = 0
    with Path(path).open("rb") as handle:
        ranges = byte_ranges if byte_ranges is not None else ((offset, Path(path).stat().st_size),)
        for range_start, range_end in ranges:
            handle.seek(range_start)
            while handle.tell() < range_end:
                raw_line = handle.readline()
                if not raw_line:
                    break
                byte_count += len(raw_line)
                if not raw_line.strip():
                    continue
                lines += 1
                try:
                    record = json.loads(raw_line)
                    parsed += 1
                    if is_projection:
                        item, epoch, capacity = parse_projection_record(record)
                        items.append((item, epoch, capacity))
                        continue
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                    raise _CapacitySourceError(str(exc), {
                        "bytes": byte_count, "lines": lines, "parsed": parsed,
                        "invalid": invalid}) from exc
                expected = "hycube_history_projection" if is_projection else "hycube_system_observation"
                if record.get("record_type") != expected:
                    if is_projection:
                        raise ValueError("projection record type mismatch")
                    invalid += 1
                    continue
                received_at = record.get("received_at")
                capacity = record.get("battery_capacity" if is_projection else "BatteryCapacity")
                if received_at is None or isinstance(capacity, bool) or not isinstance(capacity, (int, float)):
                    if is_projection:
                        raise ValueError("invalid projection record")
                    continue
                epoch = datetime.fromisoformat(received_at).timestamp()
                items.append((record, epoch, float(capacity)))
    return items, {"bytes": byte_count, "lines": lines, "parsed": parsed,
                   "invalid": invalid}


def _source_mode(plans, projection_files, raw_fallback_files, raw_tail_files):
    if not plans:
        return "not_executed"
    if projection_files and not raw_fallback_files and not raw_tail_files:
        return "projection"
    if projection_files and raw_tail_files and not raw_fallback_files:
        return "projection_plus_raw_tail"
    if not projection_files:
        return "raw_fallback"
    return "mixed"


def _fallback_reason(reasons):
    if not reasons:
        return None
    priority = ("projection_read_error", "schema_unsupported", "raw_changed",
                "metadata_invalid", "projection_incomplete", "projection_missing",
                "current_day_tail")
    return next((reason for reason in priority if reason in reasons), "projection_invalid")


class HycubeBatteryCapacitySeries:
    """Read-only, time-windowed projection of observed BatteryCapacity."""

    def __init__(self, directory=DEFAULT_HYCUBE_HISTORY_DIR, cache_size=24,
                 projection_directory=None):
        self.directory = Path(directory)
        self.projection_directory = Path(
            projection_directory if projection_directory is not None
            else (DEFAULT_HYCUBE_PROJECTION_DIR
                  if self.directory == DEFAULT_HYCUBE_HISTORY_DIR
                  else self.directory.parent / "hycube_history_projection"))
        self.cache_size = cache_size
        self._cache = OrderedDict()

    def _paths(self, start, end):
        if not self.directory.exists():
            return []
        first = datetime.fromisoformat(start).astimezone(timezone.utc).date().isoformat()
        last = datetime.fromisoformat(end).astimezone(timezone.utc).date().isoformat()
        return sorted(path for path in self.directory.glob("*.jsonl")
                      if first <= path.stem <= last)

    def query(self, *, timestamp_from, timestamp_to, max_points=850, timing=None):
        from history_series import _ExtremaCollector

        stage = timing.stage if timing else nullcontext
        with stage("hycube_discovery"):
            considered = len(list(self.directory.glob("*.jsonl"))) if self.directory.exists() else 0
            paths = self._paths(timestamp_from, timestamp_to)
        try:
            plans = [projection_plan(path, self.projection_directory) for path in paths]
            signature_items = []
            for plan in plans:
                files = [plan["raw_path"]]
                if plan["mode"] != "raw":
                    files.extend((plan["projection_path"], plan["metadata_path"]))
                    signature_items.append(index_signature(plan["projection_path"]))
                signature_items.append((plan["mode"], plan.get("raw_offset"), tuple(
                    (str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in files)))
            signature = tuple(signature_items)
        except OSError as exc:
            raise HycubeHistoryError("Hycube history is unavailable") from exc
        key = (signature, timestamp_from, timestamp_to, max_points)
        if key in self._cache:
            self._cache.move_to_end(key)
            if timing:
                timing.not_executed("hycube_projection_read_parse_filter",
                                    "hycube_raw_fallback_read_parse_filter",
                                    "hycube_downsampling")
                timing.counts(hycube_source_mode="not_executed")
            return {**self._cache[key], "cache_hit": True}
        started = time.perf_counter()
        start_epoch = datetime.fromisoformat(timestamp_from).timestamp()
        end_epoch = datetime.fromisoformat(timestamp_to).timestamp()
        collector = _ExtremaCollector(max_points, start_epoch, end_epoch)
        raw_records = 0; raw_lines = 0; parsed = 0; bytes_read = 0; opened = 0
        projection_files = projection_attempts = projection_records = 0
        raw_fallback_files = raw_tail_files = 0
        projection_bytes = projection_lines = projection_parsed = 0
        raw_bytes = raw_source_lines = raw_parsed = raw_records_in_window = 0
        projection_wall = projection_cpu = raw_wall = raw_cpu = 0.0
        projection_seek_modes = set(); projection_skipped_bytes = 0
        fallback_reasons = []
        parse_errors = invalid_records = 0
        try:
          with nullcontext():
            for plan in plans:
                sources = []
                if plan["mode"] == "raw":
                    raw_fallback_files += 1
                    fallback_reasons.append(plan.get("fallback_reason", "projection_invalid"))
                    sources.append((plan["raw_path"], 0, False))
                else:
                    projection_files += 1
                    projection_attempts += 1
                    sources.append((plan["projection_path"], 0, True))
                    if plan["mode"] == "projection_tail":
                        raw_tail_files += 1
                        fallback_reasons.append("current_day_tail")
                        sources.append((plan["raw_path"], plan["raw_offset"], False))
                day_items = []
                try:
                    for path, offset, is_projection in sources:
                        source_wall = time.monotonic(); source_cpu = time.thread_time()
                        ranges = None
                        if is_projection:
                            try:
                                ranges, seek = selected_ranges(
                                    path, start_epoch, end_epoch,
                                    timestamp_field="received_at", iso_timestamp=True)
                                projection_seek_modes.add(seek["mode"])
                                projection_skipped_bytes += seek["skipped_bytes"]
                            except Exception:
                                projection_seek_modes.add("full_scan")
                        items, counts = _read_capacity_source(
                            path, offset, is_projection, byte_ranges=ranges)
                        elapsed_wall = time.monotonic() - source_wall
                        elapsed_cpu = time.thread_time() - source_cpu
                        if is_projection:
                            projection_wall += elapsed_wall; projection_cpu += elapsed_cpu
                            projection_bytes += counts["bytes"]
                            projection_lines += counts["lines"]
                            projection_parsed += counts["parsed"]
                        else:
                            raw_wall += elapsed_wall; raw_cpu += elapsed_cpu
                            raw_bytes += counts["bytes"]
                            raw_source_lines += counts["lines"]
                            raw_parsed += counts["parsed"]
                        opened += 1; bytes_read += counts["bytes"]; raw_lines += counts["lines"]
                        parsed += counts["parsed"]; invalid_records += counts["invalid"]
                        day_items.extend(items)
                except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError,
                        ValueError) as projection_error:
                    if plan["mode"] == "raw":
                        if isinstance(projection_error, _CapacitySourceError):
                            counts = projection_error.counts
                            raw_bytes += counts["bytes"]
                            raw_source_lines += counts["lines"]
                            raw_parsed += counts["parsed"]
                        raw_wall += time.monotonic() - source_wall
                        raw_cpu += time.thread_time() - source_cpu
                        parse_errors += 1
                        raise
                    if isinstance(projection_error, _CapacitySourceError):
                        counts = projection_error.counts
                        projection_bytes += counts["bytes"]
                        projection_lines += counts["lines"]
                        projection_parsed += counts["parsed"]
                    projection_wall += time.monotonic() - source_wall
                    projection_cpu += time.thread_time() - source_cpu
                    # A derived projection is disposable: fall back for this day only.
                    raw_fallback_files += 1
                    projection_files -= 1
                    fallback_reasons.append("projection_read_error")
                    source_wall = time.monotonic(); source_cpu = time.thread_time()
                    items, counts = _read_capacity_source(plan["raw_path"], 0, False)
                    raw_wall += time.monotonic() - source_wall
                    raw_cpu += time.thread_time() - source_cpu
                    raw_bytes += counts["bytes"]; raw_source_lines += counts["lines"]
                    raw_parsed += counts["parsed"]
                    opened += 1; bytes_read += counts["bytes"]; raw_lines += counts["lines"]
                    parsed += counts["parsed"]; invalid_records += counts["invalid"]
                    day_items = items
                for record, epoch, capacity in day_items:
                    if not start_epoch <= epoch <= end_epoch:
                        continue
                    collector.add({
                        "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                        "_epoch": epoch, "value": float(capacity),
                        "source": "hycube", "source_field": "BatteryCapacity",
                        "device_timestamp": record.get("device_timestamp"),
                        "timezone_semantics": record.get("timezone_semantics"),
                        "parse_quality": record.get("parse_quality"),
                        "payload_sha256": record.get("payload_sha256"),
                        "configured_interval_seconds": record.get("configured_interval_seconds"),
                        "actual_interval_seconds": record.get("actual_interval_seconds"),
                        "actual_interval_quality": record.get("actual_interval_quality"),
                        "_history_storage": ("projection" if record.get("record_type") ==
                                             "hycube_history_projection" else "raw"),
                    })
                    raw_records += 1
                    if record.get("record_type") == "hycube_history_projection":
                        projection_records += 1
                    else:
                        raw_records_in_window += 1
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            if timing:
                if projection_attempts:
                    timing.record("hycube_projection_read_parse_filter",
                                  projection_wall, projection_cpu)
                else:
                    timing.not_executed("hycube_projection_read_parse_filter")
                if raw_fallback_files or raw_tail_files:
                    timing.record("hycube_raw_fallback_read_parse_filter", raw_wall, raw_cpu)
                else:
                    timing.not_executed("hycube_raw_fallback_read_parse_filter")
                timing.counts(hycube_files_considered=considered,
                    hycube_files_opened=opened, hycube_bytes_read=bytes_read,
                    hycube_raw_lines=raw_lines, hycube_parsed_records=parsed,
                    hycube_parse_errors=parse_errors,
                    hycube_invalid_records=invalid_records,
                    hycube_records_in_window=raw_records,
                    hycube_projection_bytes_read=projection_bytes,
                    projection_seek_mode=(next(iter(projection_seek_modes))
                        if len(projection_seek_modes) == 1 else "mixed"),
                    projection_skipped_bytes=projection_skipped_bytes,
                    hycube_raw_fallback_bytes_read=raw_bytes,
                    hycube_source_mode=_source_mode(
                        plans, projection_files, raw_fallback_files, raw_tail_files),
                    hycube_raw_fallback_reason=_fallback_reason(fallback_reasons))
            raise HycubeHistoryError(f"Hycube history is invalid: {exc}") from exc
        read_seconds = time.perf_counter() - started
        downsample_started = time.perf_counter(); downsample_cpu = time.thread_time()
        points = collector.points()
        projection_points_after = sum(
            point.get("_history_storage") == "projection" for point in points)
        for point in points:
            point.pop("_history_storage", None)
        if timing:
            if projection_attempts:
                timing.record("hycube_projection_read_parse_filter",
                              projection_wall, projection_cpu)
            else:
                timing.not_executed("hycube_projection_read_parse_filter")
            if raw_fallback_files or raw_tail_files:
                timing.record("hycube_raw_fallback_read_parse_filter", raw_wall, raw_cpu)
            else:
                timing.not_executed("hycube_raw_fallback_read_parse_filter")
            timing.record("hycube_downsampling", time.perf_counter() - downsample_started,
                          time.thread_time() - downsample_cpu)
            timing.counts(hycube_files_considered=considered,
                hycube_files_opened=opened, hycube_bytes_read=bytes_read,
                hycube_raw_lines=raw_lines, hycube_parsed_records=parsed,
                hycube_parse_errors=parse_errors,
                hycube_invalid_records=invalid_records,
                hycube_projection_files=projection_files,
                hycube_projection_records=projection_records,
                hycube_raw_fallback_files=raw_fallback_files,
                hycube_raw_tail_files=raw_tail_files,
                hycube_projection_files_considered=sum(
                    plan["mode"] != "raw" for plan in plans),
                hycube_projection_files_opened=projection_attempts,
                hycube_projection_bytes_read=projection_bytes,
                projection_seek_mode=(next(iter(projection_seek_modes))
                    if len(projection_seek_modes) == 1 else "mixed"),
                projection_skipped_bytes=projection_skipped_bytes,
                hycube_projection_raw_lines=projection_lines,
                hycube_projection_parsed_records=projection_parsed,
                hycube_projection_records_in_window=projection_records,
                hycube_projection_points_before_downsampling=projection_records,
                hycube_projection_points_after_downsampling=projection_points_after,
                hycube_raw_fallback_files_considered=sum(
                    plan["mode"] == "raw" or plan["mode"] == "projection_tail"
                    for plan in plans),
                hycube_raw_fallback_files_opened=raw_fallback_files + raw_tail_files,
                hycube_raw_fallback_bytes_read=raw_bytes,
                hycube_raw_fallback_raw_lines=raw_source_lines,
                hycube_raw_fallback_parsed_records=raw_parsed,
                hycube_raw_fallback_records_in_window=raw_records_in_window,
                hycube_source_mode=_source_mode(
                    plans, projection_files, raw_fallback_files, raw_tail_files),
                hycube_raw_fallback_reason=_fallback_reason(fallback_reasons),
                hycube_records_in_window=raw_records,
                hycube_points_before_downsampling=raw_records,
                hycube_points_after_downsampling=len(points))
        result = {"metric": "hycube_battery_capacity", "label": "Hycube Battery Capacity",
                  "unit": "%", "source": "hycube", "source_field": "BatteryCapacity",
                  "timestamp_source": "received_at", "points": points,
                  "raw_points": raw_records, "raw_records": raw_records,
                  "read_seconds": read_seconds,
                  "downsample_seconds": time.perf_counter() - downsample_started,
                  "cache_hit": False}
        self._cache[key] = result
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return result


def policy_observation(raw: bytes, observed_at: float, http_status=200,
                       content_type=None) -> dict:
    """Preserve one policy response and normalize only the verified contract."""
    base = {
        "schema_version": SCHEMA_VERSION,
        "policy_semantics_version": POLICY_SEMANTICS_VERSION,
        "record_type": "hycube_policy_observation",
        "observed_at": _utc_iso(observed_at), "source": "hycube",
        "endpoint": POLICY_ENDPOINT, "http_status": int(http_status),
        "content_type": content_type,
        "raw_response": raw.decode("utf-8", errors="replace"),
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
        "causality": "not_determined",
    }
    if int(http_status) != 200:
        return {**base, "parse_quality": "invalid", "validation_error": "http_status"}
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload_not_object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {**base, "parse_quality": "invalid",
                "validation_error": str(exc) or type(exc).__name__}
    raw_values = {field: payload.get(field) for field in POLICY_FIELDS}
    missing = [field for field in POLICY_FIELDS if field not in payload]
    if missing:
        return {**base, **raw_values, "parse_quality": "invalid",
                "validation_error": "missing_fields", "missing_fields": missing}
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not isfinite(float(value)) for value in raw_values.values()):
        return {**base, **raw_values, "parse_quality": "invalid",
                "validation_error": "fields_must_be_finite_numbers"}
    if any(not 0 <= float(value) <= 100 for value in raw_values.values()):
        return {**base, **raw_values, "parse_quality": "invalid",
                "validation_error": "field_out_of_range"}
    if sum(float(value) for value in raw_values.values()) != 100:
        return {**base, **raw_values, "parse_quality": "invalid",
                "validation_error": "sum_must_equal_100"}
    normal, passive, emergency, protection = (float(raw_values[field])
                                               for field in POLICY_FIELDS)
    return {
        **base, **raw_values,
        "normal_operation_pct": normal, "passive_pct": passive,
        "emergency_pct": emergency, "battery_protection_pct": protection,
        "boundary_normal_passive": 100.0 - normal,
        "boundary_passive_emergency": 100.0 - normal - passive,
        "boundary_emergency_protection": 100.0 - normal - passive - emergency,
        "unit": "%", "parse_quality": "complete",
    }


def _policy_values(record):
    return tuple(record.get(field) for field in
                 ("normal_operation_pct", "passive_pct", "emergency_pct",
                  "battery_protection_pct"))


class HycubePolicyHistory:
    """Append-only policy observations and time-valid read projection."""

    def __init__(self, directory=DEFAULT_HYCUBE_POLICY_HISTORY_DIR):
        self.directory = Path(directory)

    def _latest_valid(self):
        if not self.directory.exists():
            return None
        for path in sorted(self.directory.glob("*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines):
                    record = json.loads(line)
                    if (record.get("record_type") == "hycube_policy_observation"
                            and record.get("parse_quality") == "complete"):
                        return record
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def append(self, record: dict) -> Path:
        item = dict(record)
        previous = self._latest_valid() if item.get("parse_quality") == "complete" else None
        changed = previous is None or _policy_values(previous) != _policy_values(item)
        item["policy_changed"] = changed if item.get("parse_quality") == "complete" else None
        item["effective_at"] = (item["observed_at"] if changed else previous.get(
            "effective_at", previous["observed_at"])) if previous else item.get("observed_at")
        timestamp = datetime.fromisoformat(item["observed_at"])
        path = self.directory / f"{timestamp.astimezone(timezone.utc).date().isoformat()}.jsonl"
        self.directory.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def query(self, *, timestamp_from, timestamp_to, timing=None):
        if not self.directory.exists():
            if timing:
                timing.counts(policy_files=0, policy_records=0)
            return []
        start = datetime.fromisoformat(timestamp_from).timestamp()
        end = datetime.fromisoformat(timestamp_to).timestamp()
        first_day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
        last_day = datetime.fromtimestamp(end, timezone.utc).date().isoformat()
        last_before = None; parsed_records = 0
        changes = []
        try:
            all_paths = sorted(self.directory.glob("*.jsonl"))
            paths = [path for path in all_paths if first_day <= path.stem <= last_day]
            for path in reversed([path for path in all_paths if path.stem < first_day]):
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines):
                    record = json.loads(line)
                    parsed_records += 1
                    if (record.get("record_type") == "hycube_policy_observation"
                            and record.get("parse_quality") == "complete"):
                        last_before = record
                        break
                if last_before:
                    break
            for path in paths:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        parsed_records += 1
                        if (record.get("record_type") != "hycube_policy_observation"
                                or record.get("parse_quality") != "complete"):
                            continue
                        epoch = datetime.fromisoformat(record["observed_at"]).timestamp()
                        if epoch <= start:
                            last_before = record
                        elif epoch <= end and record.get("policy_changed", True):
                            changes.append(record)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HycubeHistoryError(f"Hycube policy history is invalid: {exc}") from exc
        records = ([last_before] if last_before else []) + changes
        if timing:
            timing.counts(policy_files=len(all_paths), policy_records=parsed_records)
        segments = []
        for index, record in enumerate(records):
            segment_from = timestamp_from if index == 0 and last_before else record["observed_at"]
            segment_to = (records[index + 1]["observed_at"] if index + 1 < len(records)
                          else timestamp_to)
            segments.append({
                "from": segment_from, "to": segment_to,
                "observed_at": record["observed_at"],
                "effective_at": record.get("effective_at", record["observed_at"]),
                "normal_operation_pct": record["normal_operation_pct"],
                "passive_pct": record["passive_pct"],
                "emergency_pct": record["emergency_pct"],
                "battery_protection_pct": record["battery_protection_pct"],
                "boundary_normal_passive": record["boundary_normal_passive"],
                "boundary_passive_emergency": record["boundary_passive_emergency"],
                "boundary_emergency_protection": record["boundary_emergency_protection"],
                "source": "hycube", "endpoint": POLICY_ENDPOINT,
                "quality": ("historically_applicable" if record.get(
                    "effective_at", record["observed_at"]) < segment_from
                            else "observed"), "causality": "not_determined",
            })
        return segments


def evidence_enabled(value) -> bool:
    """Require an actual JSON boolean; truthy strings must never enable I/O."""
    return value is True


def data_row_url(base_url: str) -> str:
    """Return the fixed read endpoint after rejecting unsafe target forms."""
    return _fixed_read_url(base_url, "/data_row/")


def policy_url(base_url: str) -> str:
    """Return the fixed parameter-free policy readback endpoint."""
    return _fixed_read_url(base_url, POLICY_ENDPOINT)


def _fixed_read_url(base_url: str, path: str) -> str:
    parsed = urlsplit(str(base_url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("hycube base URL must be HTTP(S)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("credentials, query and fragment are not allowed")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname != "localhost":
            raise ValueError("hycube address must be a local IP or localhost")
    else:
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("hycube address must be local")
    netloc = parsed.hostname
    if ":" in netloc and not netloc.startswith("["):
        netloc = f"[{netloc}]"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def _device_time(value):
    if not isinstance(value, str) or not value.strip():
        return None, "unavailable"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None:
        return value, "unknown"
    return parsed.astimezone(timezone.utc).isoformat(), "explicit"


def observation(raw: bytes, received_at: float, http_status: int = 200) -> dict:
    """Build one reproducible observation without inventing absent values."""
    digest = hashlib.sha256(raw).hexdigest()
    base = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "record_type": "hycube_system_observation",
        "received_at": _utc_iso(received_at),
        "endpoint": "/data_row/", "http_status": int(http_status),
        "raw_payload": raw.decode("utf-8", errors="replace"),
        "payload_sha256": digest, "causality": "not_determined",
        "source_semantics": "hycube_system_response",
        "policy_evidence": "unavailable",
        "policy_evidence_reason": "no_verified_read_only_source",
    }
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {**base, "parse_quality": "invalid", "parse_error": type(exc).__name__,
                **{field: None for field in FIELDS}}
    result = {field: payload.get(field) for field in FIELDS}
    device_timestamp, timezone_semantics = _device_time(result["Date2"])
    offset = None
    if timezone_semantics == "explicit":
        offset = float(received_at) - datetime.fromisoformat(device_timestamp).timestamp()
    return {**base, **result, "device_timestamp": device_timestamp,
            "timezone_semantics": timezone_semantics,
            "device_receive_offset_seconds": offset,
            "parse_quality": "complete" if all(field in payload for field in FIELDS)
            else "partial"}


class HycubeEvidenceWriter:
    """Append successful observations to UTC daily JSONL without rewriting."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def append_with_receipt(self, record: dict):
        timestamp = datetime.fromisoformat(record["received_at"])
        path = self.directory / f"{timestamp.astimezone(timezone.utc).date().isoformat()}.jsonl"
        self.directory.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8")
        with path.open("ab") as handle:
            start_offset = handle.tell()
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
            end_offset = handle.tell()
        return HycubeWriteReceipt(path, start_offset, end_offset)

    def append(self, record: dict) -> Path:
        return self.append_with_receipt(record).path


@dataclass(frozen=True)
class HycubeWriteReceipt:
    path: Path
    start_offset: int
    end_offset: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class HycubeCollector:
    """Single non-overlapping GET loop isolated from Guardian acquisition."""

    def __init__(self, base_url, writer, *, policy_writer=None, projection_store=None,
                 interval_seconds=5.0, policy_interval_seconds=DEFAULT_POLICY_POLL_INTERVAL_SECONDS,
                 timeout_seconds=0.8, clock=time.time, monotonic=time.monotonic,
                 opener=None, max_backoff_seconds=60.0):
        self.url = data_row_url(base_url)
        self.policy_url = policy_url(base_url)
        self.writer = writer
        self.policy_writer = policy_writer
        self.projection_store = projection_store
        self.interval_seconds = float(interval_seconds)
        self.policy_interval_seconds = float(policy_interval_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.monotonic = monotonic
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last_received_timestamp = None
        self._status = {"state": "starting", "last_success_at": None,
                        "last_error": None, "observations": 0, "failures": 0,
                        "policy_state": "disabled" if policy_writer is None else "starting",
                        "policy_last_success_at": None, "policy_last_error": None,
                        "policy_observations": 0, "policy_failures": 0}

    def status(self):
        with self._lock:
            status = dict(self._status)
        if self.projection_store is not None:
            status["projection"] = self.projection_store.status()
        return status

    def _set(self, **values):
        with self._lock:
            self._status.update(values)

    def collect_once(self):
        request = urllib.request.Request(self.url, method="GET",
                                         headers={"Accept": "application/json"})
        with self.opener.open(request, timeout=self.timeout_seconds) as response:
            status = int(response.getcode())
            raw = response.read(MAX_RESPONSE_BODY_BYTES + 1)
        if status != 200:
            raise RuntimeError(f"hycube HTTP status {status}")
        if len(raw) > MAX_RESPONSE_BODY_BYTES:
            raise ValueError("Hycube response body exceeds 1 MiB limit")
        received_timestamp = float(self.clock())
        record = observation(raw, received_timestamp, status)
        record["configured_interval_seconds"] = self.interval_seconds
        actual_interval = (None if self._last_received_timestamp is None
                           else received_timestamp - self._last_received_timestamp)
        record["actual_interval_seconds"] = (
            actual_interval if actual_interval is None or actual_interval >= 0 else None)
        record["actual_interval_quality"] = (
            "first_observation" if actual_interval is None else
            "observed" if actual_interval >= 0 else "clock_regression")
        record["request_timeout_seconds"] = self.timeout_seconds
        receipt = self.writer.append_with_receipt(record)
        if self.projection_store is not None:
            try:
                self.projection_store.append_live(record, receipt)
            except Exception as exc:
                LOG.warning("Hycube projection write failed: %s", type(exc).__name__)
        if record["parse_quality"] == "invalid":
            raise ValueError("invalid Hycube JSON")
        self._last_received_timestamp = received_timestamp
        self._set(state="available", last_success_at=record["received_at"], last_error=None,
                  observations=self.status()["observations"] + 1)
        return record

    def collect_policy_once(self):
        if self.policy_writer is None:
            return None
        request = urllib.request.Request(self.policy_url, method="GET",
                                         headers={"Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.getcode())
                raw = response.read(MAX_RESPONSE_BODY_BYTES + 1)
                content_type = response.headers.get("Content-Type") if response.headers else None
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(MAX_RESPONSE_BODY_BYTES + 1)
            content_type = exc.headers.get("Content-Type") if exc.headers else None
        if len(raw) > MAX_RESPONSE_BODY_BYTES:
            raise ValueError("Hycube policy response body exceeds 1 MiB limit")
        record = policy_observation(raw, float(self.clock()), status, content_type)
        record["configured_interval_seconds"] = self.policy_interval_seconds
        record["request_timeout_seconds"] = self.timeout_seconds
        self.policy_writer.append(record)
        if record["parse_quality"] != "complete":
            raise ValueError(f"invalid Hycube policy: {record['validation_error']}")
        self._set(policy_state="available", policy_last_success_at=record["observed_at"],
                  policy_last_error=None,
                  policy_observations=self.status()["policy_observations"] + 1)
        return record

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guardian-hycube-evidence",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._thread = None
        if self.projection_store is not None:
            try:
                self.projection_store.close()
            except Exception as exc:
                LOG.warning("Hycube projection flush failed: %s", type(exc).__name__)
        self._set(state="disabled")

    def _run(self):
        failures = 0
        next_policy_poll = 0.0
        while not self._stop.is_set():
            started = self.monotonic()
            try:
                self.collect_once()
                failures = 0
            except Exception as exc:
                failures += 1
                self._set(state="error", last_error=f"{type(exc).__name__}: {exc}",
                          failures=self.status()["failures"] + 1)
                LOG.warning("Hycube read-only observation failed: %s", type(exc).__name__)
            if self.policy_writer is not None and self.monotonic() >= next_policy_poll:
                try:
                    self.collect_policy_once()
                except Exception as exc:
                    self._set(policy_state="error",
                              policy_last_error=f"{type(exc).__name__}: {exc}",
                              policy_failures=self.status()["policy_failures"] + 1)
                    LOG.warning("Hycube policy read-only observation failed: %s",
                                type(exc).__name__)
                next_policy_poll = self.monotonic() + self.policy_interval_seconds
            delay = (self.interval_seconds if failures == 0 else
                     min(self.max_backoff_seconds, self.interval_seconds * (2 ** failures)))
            self._stop.wait(max(0.0, delay - (self.monotonic() - started)))


def collector_from_options(options: dict, history_directory, policy_history_directory=None,
                           projection_directory=None):
    """Construct the collector only after an explicit boolean opt-in."""
    if not evidence_enabled(options.get("hycube_evidence_enabled", False)):
        return None
    return HycubeCollector(
        options["hycube_base_url"], HycubeEvidenceWriter(history_directory),
        projection_store=HycubeProjectionStore(
            projection_directory if projection_directory is not None
            else Path(history_directory).parent / "hycube_history_projection"),
        policy_writer=(HycubePolicyHistory(policy_history_directory)
                       if policy_history_directory is not None else None),
        interval_seconds=float(options.get("hycube_interval_seconds", 5)),
        policy_interval_seconds=DEFAULT_POLICY_POLL_INTERVAL_SECONDS,
        timeout_seconds=float(options.get("hycube_timeout_seconds", 0.8)),
    )
