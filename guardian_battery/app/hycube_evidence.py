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
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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


class HycubeBatteryCapacitySeries:
    """Read-only, time-windowed projection of observed BatteryCapacity."""

    def __init__(self, directory=DEFAULT_HYCUBE_HISTORY_DIR, cache_size=24):
        self.directory = Path(directory)
        self.cache_size = cache_size
        self._cache = OrderedDict()

    def _paths(self, start, end):
        if not self.directory.exists():
            return []
        first = datetime.fromisoformat(start).astimezone(timezone.utc).date().isoformat()
        last = datetime.fromisoformat(end).astimezone(timezone.utc).date().isoformat()
        return sorted(path for path in self.directory.glob("*.jsonl")
                      if first <= path.stem <= last)

    def query(self, *, timestamp_from, timestamp_to, max_points=850):
        from history_series import _ExtremaCollector

        paths = self._paths(timestamp_from, timestamp_to)
        try:
            signature = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns)
                              for path in paths)
        except OSError as exc:
            raise HycubeHistoryError("Hycube history is unavailable") from exc
        key = (signature, timestamp_from, timestamp_to, max_points)
        if key in self._cache:
            self._cache.move_to_end(key)
            return {**self._cache[key], "cache_hit": True}
        started = time.perf_counter()
        start_epoch = datetime.fromisoformat(timestamp_from).timestamp()
        end_epoch = datetime.fromisoformat(timestamp_to).timestamp()
        collector = _ExtremaCollector(max_points, start_epoch, end_epoch)
        raw_records = 0
        try:
            for path in paths:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        if record.get("record_type") != "hycube_system_observation":
                            continue
                        received_at = record.get("received_at")
                        capacity = record.get("BatteryCapacity")
                        if received_at is None or isinstance(capacity, bool) or not isinstance(capacity, (int, float)):
                            continue
                        epoch = datetime.fromisoformat(received_at).timestamp()
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
                        })
                        raw_records += 1
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HycubeHistoryError(f"Hycube history is invalid: {exc}") from exc
        read_seconds = time.perf_counter() - started
        downsample_started = time.perf_counter()
        points = collector.points()
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

    def query(self, *, timestamp_from, timestamp_to):
        if not self.directory.exists():
            return []
        start = datetime.fromisoformat(timestamp_from).timestamp()
        end = datetime.fromisoformat(timestamp_to).timestamp()
        first_day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
        last_day = datetime.fromtimestamp(end, timezone.utc).date().isoformat()
        last_before = None
        changes = []
        try:
            all_paths = sorted(self.directory.glob("*.jsonl"))
            paths = [path for path in all_paths if first_day <= path.stem <= last_day]
            for path in reversed([path for path in all_paths if path.stem < first_day]):
                lines = path.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines):
                    record = json.loads(line)
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

    def append(self, record: dict) -> Path:
        timestamp = datetime.fromisoformat(record["received_at"])
        path = self.directory / f"{timestamp.astimezone(timezone.utc).date().isoformat()}.jsonl"
        self.directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class HycubeCollector:
    """Single non-overlapping GET loop isolated from Guardian acquisition."""

    def __init__(self, base_url, writer, *, policy_writer=None,
                 interval_seconds=5.0, policy_interval_seconds=DEFAULT_POLICY_POLL_INTERVAL_SECONDS,
                 timeout_seconds=0.8, clock=time.time, monotonic=time.monotonic,
                 opener=None, max_backoff_seconds=60.0):
        self.url = data_row_url(base_url)
        self.policy_url = policy_url(base_url)
        self.writer = writer
        self.policy_writer = policy_writer
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
            return dict(self._status)

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
        self.writer.append(record)
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


def collector_from_options(options: dict, history_directory, policy_history_directory=None):
    """Construct the collector only after an explicit boolean opt-in."""
    if not evidence_enabled(options.get("hycube_evidence_enabled", False)):
        return None
    return HycubeCollector(
        options["hycube_base_url"], HycubeEvidenceWriter(history_directory),
        policy_writer=(HycubePolicyHistory(policy_history_directory)
                       if policy_history_directory is not None else None),
        interval_seconds=float(options.get("hycube_interval_seconds", 5)),
        policy_interval_seconds=DEFAULT_POLICY_POLL_INTERVAL_SECONDS,
        timeout_seconds=float(options.get("hycube_timeout_seconds", 0.8)),
    )
