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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


LOG = logging.getLogger("guardian_battery.hycube_evidence")
SCHEMA_VERSION = 1
COLLECTOR_VERSION = "guardian-hycube-read-only-1"
FIELDS = ("BatteryPower", "BatteryCapacity", "GridPower", "HomePower",
          "solarPower", "ExternalPower", "Date2")
MAX_RESPONSE_BODY_BYTES = 1024 * 1024


def evidence_enabled(value) -> bool:
    """Require an actual JSON boolean; truthy strings must never enable I/O."""
    return value is True


def data_row_url(base_url: str) -> str:
    """Return the fixed read endpoint after rejecting unsafe target forms."""
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
    return urlunsplit((parsed.scheme, netloc, "/data_row/", "", ""))


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

    def __init__(self, base_url, writer, *, interval_seconds=5.0, timeout_seconds=0.8,
                 clock=time.time, opener=None, max_backoff_seconds=60.0):
        self.url = data_row_url(base_url)
        self.writer = writer
        self.interval_seconds = float(interval_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.max_backoff_seconds = float(max_backoff_seconds)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last_received_timestamp = None
        self._status = {"state": "starting", "last_success_at": None,
                        "last_error": None, "observations": 0, "failures": 0}

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
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.collect_once()
                failures = 0
            except Exception as exc:
                failures += 1
                self._set(state="error", last_error=f"{type(exc).__name__}: {exc}",
                          failures=self.status()["failures"] + 1)
                LOG.warning("Hycube read-only observation failed: %s", type(exc).__name__)
            delay = (self.interval_seconds if failures == 0 else
                     min(self.max_backoff_seconds, self.interval_seconds * (2 ** failures)))
            self._stop.wait(max(0.0, delay - (time.monotonic() - started)))


def collector_from_options(options: dict, history_directory):
    """Construct the collector only after an explicit boolean opt-in."""
    if not evidence_enabled(options.get("hycube_evidence_enabled", False)):
        return None
    return HycubeCollector(
        options["hycube_base_url"], HycubeEvidenceWriter(history_directory),
        interval_seconds=float(options.get("hycube_interval_seconds", 5)),
        timeout_seconds=float(options.get("hycube_timeout_seconds", 0.8)),
    )
