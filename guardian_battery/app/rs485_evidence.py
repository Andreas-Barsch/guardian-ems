"""Append-only passive RS485 evidence and read-only history projection."""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rs485_sniffer import Correlation, ParsedFrame, decode_0x92


DEFAULT_RS485_HISTORY_DIR = Path("/share/guardian_battery/rs485_history")
RS485_SCHEMA_VERSION = 1
RS485_SERIES_METRICS = frozenset({
    "rs485_ccl", "rs485_dcl", "rs485_cvl", "rs485_dvl",
    "rs485_charge_enable", "rs485_discharge_enable",
})
_METRIC_FIELDS = {
    "rs485_ccl": "charge_current_limit_a",
    "rs485_dcl": "discharge_current_limit_a",
    "rs485_cvl": "charge_voltage_limit_v",
    "rs485_dvl": "discharge_voltage_limit_v",
    "rs485_charge_enable": "charge_enable",
    "rs485_discharge_enable": "discharge_enable",
}
_CHANGE_FIELDS = tuple(dict.fromkeys(_METRIC_FIELDS.values())) + (
    "charge_immediately_1", "charge_immediately_2", "full_charge_request",
)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class Rs485EvidenceWriter:
    """Bounded asynchronous JSONL writer; overload never blocks acquisition."""

    def __init__(self, directory=DEFAULT_RS485_HISTORY_DIR, *, queue_size=4096,
                 batch_size=64, flush_interval_seconds=1.0):
        self.directory = Path(directory)
        self.batch_size = int(batch_size)
        self.flush_interval_seconds = float(flush_interval_seconds)
        self._queue = queue.Queue(maxsize=int(queue_size))
        self._stop = threading.Event()
        self._thread = None
        self.records_written = 0
        self.bytes_written = 0
        self.dropped_records = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guardian-rs485-evidence", daemon=True)
        self._thread.start()
        return True

    def append(self, record: dict) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped_records += 1
            return False

    def status(self):
        return {"queue_depth": self._queue.qsize(), "queue_capacity": self._queue.maxsize,
                "records_written": self.records_written, "bytes_written": self.bytes_written,
                "dropped_records": self.dropped_records}

    def stop(self, timeout=5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self._thread = None

    def _write_batch(self, batch):
        groups = {}
        for record in batch:
            day = record["timestamp"][:10]
            groups.setdefault(day, []).append(record)
        for day, records in groups.items():
            path = self.directory / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                for record in records:
                    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                    handle.write(line)
                    self.records_written += 1
                    self.bytes_written += len(line.encode("utf-8"))
                handle.flush()

    def _run(self):
        batch = []
        deadline = time.monotonic() + self.flush_interval_seconds
        while not self._stop.is_set() or not self._queue.empty():
            timeout = max(0.0, deadline - time.monotonic())
            try:
                batch.append(self._queue.get(timeout=min(timeout, 0.25)))
            except queue.Empty:
                pass
            if batch and (len(batch) >= self.batch_size or time.monotonic() >= deadline
                          or (self._stop.is_set() and self._queue.empty())):
                self._write_batch(batch)
                batch.clear()
                deadline = time.monotonic() + self.flush_interval_seconds


class Rs485EvidencePipeline:
    """Convert accepted passive frames into evidence without identity guesses."""

    def __init__(self, writer: Rs485EvidenceWriter, *, wall_clock=time.time,
                 fast_frame_interval_seconds=300.0):
        self.writer = writer
        self.wall_clock = wall_clock
        self.fast_frame_interval_seconds = float(fast_frame_interval_seconds)
        self._last_fast = {}
        self._unknown_exemplars = set()
        self._latest_management = {}

    def __call__(self, frame: ParsedFrame, correlation: Correlation):
        now = float(self.wall_clock())
        command = correlation.paired_command
        if frame.is_request:
            command = frame.command
        key = (frame.adr, "request" if frame.is_request else "response")
        persist = command in {0x92, 0x44}
        if command == 0x42:
            last = self._last_fast.get(key)
            persist = last is None or now - last >= self.fast_frame_interval_seconds
            if persist:
                self._last_fast[key] = now
        if command not in {0x42, 0x44, 0x92}:
            exemplar = (frame.adr, frame.cid2_or_rtn, command)
            persist = exemplar not in self._unknown_exemplars
            self._unknown_exemplars.add(exemplar)
        if not persist:
            return
        decoded = None
        if command == 0x92 and not frame.is_request:
            decoded = decode_0x92(correlation)
        reference = hashlib.sha256(frame.raw_frame).hexdigest()
        record = {
            "schema_version": RS485_SCHEMA_VERSION, "record_type": "frame",
            "timestamp": _iso(now), "source": "rs485_passive",
            "protocol_reference": frame.protocol_reference,
            "decoder_version": frame.decoder_version,
            "raw_frame": frame.raw_frame.hex().upper(), "raw_ascii": frame.raw_ascii,
            "source_frame_reference": reference,
            "checksum_valid": frame.checksum_valid, "frame_complete": frame.frame_complete,
            "ver": frame.ver, "adr": frame.adr, "cid1": frame.cid1,
            "cid2_or_rtn": frame.cid2_or_rtn, "direction": "request" if frame.is_request else "response",
            "paired_command": command, "request_matched": correlation.request_matched,
            "info_raw": frame.info_hex, "decoder_supported": bool(decoded and decoded.get("decoder_supported")),
            "identity_resolved": False, "physical_serial": None, "position": None,
            "decoded": decoded, "quality": {"evidence_level": "observation",
                "causality": "not_determined", "identity_source": "unresolved"},
        }
        self.writer.append(record)
        if decoded and decoded.get("decoder_supported"):
            previous = self._latest_management.get(frame.adr)
            if previous is not None:
                for field in _CHANGE_FIELDS:
                    if previous.get(field) != decoded.get(field):
                        self.writer.append({
                            "schema_version": RS485_SCHEMA_VERSION, "record_type": "state_change",
                            "timestamp": _iso(now), "source": "rs485_passive", "adr": frame.adr,
                            "identity_resolved": False, "physical_serial": None, "position": None,
                            "field": field, "old_value": previous.get(field),
                            "new_value": decoded.get(field), "source_frame_reference": reference,
                            "evidence_level": "observation", "causality": "not_determined",
                        })
            self._latest_management[frame.adr] = decoded


class Rs485HistorySeries:
    """Read projected management values from rotated evidence JSONL."""

    def __init__(self, directory=DEFAULT_RS485_HISTORY_DIR, max_points=6000):
        self.directory = Path(directory)
        self.max_points = int(max_points)

    def query_bundles(self, requests, *, timestamp_from, timestamp_to, adr):
        start = datetime.fromisoformat(timestamp_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(timestamp_to.replace("Z", "+00:00"))
        points = {item["metric"]: [] for item in requests}
        day = start.date()
        while day <= end.date():
            path = self.directory / f"{day.isoformat()}.jsonl"
            if path.exists():
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # includes a crash-truncated final line
                        if record.get("record_type") != "frame" or record.get("adr") != adr:
                            continue
                        timestamp = record.get("timestamp")
                        if not timestamp or timestamp < timestamp_from or timestamp > timestamp_to:
                            continue
                        decoded = record.get("decoded") or {}
                        for request in requests:
                            field = _METRIC_FIELDS[request["metric"]]
                            if field in decoded:
                                value = decoded[field]
                                points[request["metric"]].append({"timestamp": timestamp,
                                    "value": int(value) if isinstance(value, bool) else value,
                                    "adr": adr, "identity_resolved": bool(record.get("identity_resolved"))})
            day += timedelta(days=1)
        return [{"metric": request["metric"], "adr": adr,
                 "state_semantics": {"0": "STOP REQUEST", "1": "ENABLED"}
                 if request["metric"].endswith("_enable") else None,
                 "points": self._downsample(
                     points[request["metric"]], request["metric"].endswith("_enable"))}
                for request in requests]

    def _downsample(self, points, preserve_transitions=False):
        if preserve_transitions and points:
            changes = [points[0]]
            for point in points[1:]:
                if point["value"] != changes[-1]["value"]:
                    changes.append(point)
            if changes[-1] is not points[-1]:
                changes.append(points[-1])
            return changes
        if len(points) <= self.max_points:
            return points
        stride = max(1, len(points) // (self.max_points - 2))
        selected = [points[0], *points[1:-1:stride], points[-1]]
        return selected[:self.max_points - 1] + [points[-1]] if selected[-1] is not points[-1] else selected
