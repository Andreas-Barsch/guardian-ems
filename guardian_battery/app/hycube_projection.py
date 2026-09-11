"""Derived, rebuildable Hycube history projection with raw provenance."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from history_block_index import (DEFAULT_BLOCK_RECORDS, BlockIndexError, build_index,
                                 extend_open_index, selected_ranges)


SCHEMA_VERSION = 1
RECORD_TYPE = "hycube_history_projection"
DEFAULT_HYCUBE_PROJECTION_DIR = Path(
    "/share/guardian_battery/hycube_history_projection")
FLUSH_RECORDS = 12
FLUSH_SECONDS = 30.0
BACKFILL_MAX_RECORDS = 1000
BACKFILL_MAX_BYTES = 4 * 1024 * 1024
DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


def _day(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone.utc).date().isoformat()


def projection_record(raw_record: dict, source_raw_end_offset: int) -> dict | None:
    """Project only the existing Battery Capacity history contract."""
    received_at = raw_record.get("received_at")
    capacity = raw_record.get("BatteryCapacity")
    if (raw_record.get("record_type") != "hycube_system_observation"
            or received_at is None or isinstance(capacity, bool)
            or not isinstance(capacity, (int, float))):
        return None
    datetime.fromisoformat(received_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": RECORD_TYPE,
        "received_at": raw_record.get("received_at"),
        "battery_capacity": raw_record.get("BatteryCapacity"),
        "device_timestamp": raw_record.get("device_timestamp"),
        "timezone_semantics": raw_record.get("timezone_semantics"),
        "parse_quality": raw_record.get("parse_quality"),
        "payload_sha256": raw_record.get("payload_sha256"),
        "configured_interval_seconds": raw_record.get("configured_interval_seconds"),
        "actual_interval_seconds": raw_record.get("actual_interval_seconds"),
        "actual_interval_quality": raw_record.get("actual_interval_quality"),
        "source_raw_end_offset": int(source_raw_end_offset),
    }


def parse_projection_record(record):
    """Validate the single V1 projection contract used by every reader."""
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("projection schema mismatch")
    if record.get("record_type") != RECORD_TYPE:
        raise ValueError("projection record type mismatch")
    received_at = record.get("received_at")
    capacity = record.get("battery_capacity")
    offset = record.get("source_raw_end_offset")
    if (not isinstance(received_at, str) or isinstance(capacity, bool)
            or not isinstance(capacity, (int, float)) or isinstance(offset, bool)
            or not isinstance(offset, int) or offset < 0):
        raise ValueError("invalid projection record")
    return record, datetime.fromisoformat(received_at).timestamp(), float(capacity)


def _validate_index_record(record):
    parse_projection_record(record)


def _validate_cell_index_record(record):
    if record.get("schema_version") != 1:
        raise ValueError("cell history schema mismatch")
    value = record.get("timestamp")
    if isinstance(value, bool):
        raise ValueError("invalid cell timestamp")
    float(value)


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                         encoding="utf-8")
    os.replace(temporary, path)


def _last_projection_offset(path):
    size = path.stat().st_size
    if size == 0:
        return None
    with path.open("rb") as handle:
        handle.seek(max(0, size - 8192))
        lines = [line for line in handle.read().splitlines() if line.strip()]
    if not lines:
        return None
    record, _epoch, _capacity = parse_projection_record(json.loads(lines[-1]))
    return record["source_raw_end_offset"]


class HycubeProjectionStore:
    """Bounded buffered live writer; Raw Evidence is always written first."""

    def __init__(self, directory=DEFAULT_HYCUBE_PROJECTION_DIR, *,
                 flush_records=FLUSH_RECORDS, flush_seconds=FLUSH_SECONDS,
                 monotonic=time.monotonic):
        self.directory = Path(directory)
        self.flush_records = max(1, int(flush_records))
        self.flush_seconds = max(0.0, float(flush_seconds))
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._pending = []
        self._pending_day = None
        self._last_flush = monotonic()
        self._status = {"enabled": True, "state": "idle", "current_file": None,
                        "records_written": 0, "last_success": None,
                        "write_failures": 0, "gap_skips": 0,
                        "last_error": None, "last_flush_at": None}
        self._storage = self._inventory()

    def _inventory(self):
        result = {"projection_days": 0, "projection_bytes": 0,
                  "complete_days": 0, "open_days": 0, "invalid_days": 0}
        if not self.directory.exists():
            return result
        for path in self.directory.glob("*.jsonl"):
            result["projection_days"] += 1
            try:
                result["projection_bytes"] += path.stat().st_size
                metadata = load_projection_metadata(self.directory, path.stem)
                key = "complete_days" if metadata and metadata.get("status") == "complete" \
                    else "open_days" if metadata and metadata.get("status") == "open" \
                    else "invalid_days"
                result[key] += 1
            except OSError:
                result["invalid_days"] += 1
        return result

    def projection_path(self, day):
        return self.directory / f"{day}.jsonl"

    def metadata_path(self, day):
        return self.directory / f"{day}.meta.json"

    def status(self):
        with self._lock:
            return {**self._status,
                    "failure_count": self._status["write_failures"],
                    "buffered_records": len(self._pending),
                    "last_flush": self._status["last_flush_at"],
                    **self._storage}

    def append_live(self, raw_record, receipt) -> bool:
        """Never wait behind backfill and never affect successful Raw Evidence."""
        if not self._lock.acquire(blocking=False):
            self._status["gap_skips"] += 1
            return False
        try:
            day = _day(raw_record["received_at"])
            if self._pending_day is not None and self._pending_day != day:
                self._flush_locked()
            metadata = load_projection_metadata(self.directory, day)
            confirmed = int(metadata.get("confirmed_raw_end_offset", 0)) if metadata else 0
            if self._pending and self._pending_day == day:
                confirmed = self._pending[-1][1].end_offset
            if receipt.start_offset != confirmed:
                self._status["gap_skips"] += 1
                return False
            self._pending_day = day
            self._status["current_file"] = receipt.path.name
            item = projection_record(raw_record, receipt.end_offset)
            self._pending.append((item, receipt))
            elapsed = self.monotonic() - self._last_flush
            if len(self._pending) >= self.flush_records or elapsed >= self.flush_seconds:
                self._flush_locked()
            return True
        except Exception as exc:
            self._status.update(state="error", last_error=f"{type(exc).__name__}: {exc}",
                                write_failures=self._status["write_failures"] + 1)
            # Derived records are rebuildable; never let a failed flush grow RAM.
            if len(self._pending) >= self.flush_records:
                self._pending.clear()
                self._pending_day = None
            return False
        finally:
            self._lock.release()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def close(self):
        self.flush()

    def coordinated(self, operation, *args):
        """Flush live state and serialize one bounded backfill operation."""
        with self._lock:
            self._flush_locked()
            result = operation(*args)
            self._storage = self._inventory()
            return result

    def refresh_storage(self):
        with self._lock:
            self._storage = self._inventory()

    def _flush_locked(self):
        if not self._pending:
            self._last_flush = self.monotonic()
            return
        day = self._pending_day
        path = self.projection_path(day); path.parent.mkdir(parents=True, exist_ok=True)
        metadata = load_projection_metadata(self.directory, day) or {}
        old_size = path.stat().st_size if path.exists() else 0
        old_status = metadata.get("status")
        expected_offset = int(metadata.get("projection_end_offset", 0))
        if path.exists() and path.stat().st_size != expected_offset:
            raise ValueError("projection size does not match confirmed metadata")
        projected = [item for item, _receipt in self._pending if item is not None]
        lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                 for item in projected]
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()  # Derived data: deliberately no per-flush fsync.
        first = metadata.get("first_timestamp")
        if first is None and projected:
            first = projected[0]["received_at"]
        old_count = int(metadata.get("projection_record_count", 0))
        count = old_count + len(projected)
        _last_item, last_receipt = self._pending[-1]
        last_projection_offset = metadata.get("last_projection_source_raw_end_offset")
        trailing_skipped = int(metadata.get("trailing_skipped_records", 0))
        for item, item_receipt in self._pending:
            if item is None:
                trailing_skipped += 1
            else:
                last_projection_offset = item_receipt.end_offset
                trailing_skipped = 0
        raw_stat = last_receipt.path.stat()
        next_metadata = {
            "schema_version": SCHEMA_VERSION, "status": "open",
            "raw_filename": last_receipt.path.name,
            "raw_size": raw_stat.st_size, "raw_mtime_ns": raw_stat.st_mtime_ns,
            "confirmed_raw_end_offset": last_receipt.end_offset,
            "projection_end_offset": path.stat().st_size,
            "projection_record_count": count,
            "last_projection_source_raw_end_offset": last_projection_offset,
            "trailing_skipped_records": trailing_skipped,
            "first_timestamp": first,
            "last_timestamp": (projected[-1]["received_at"] if projected
                               else metadata.get("last_timestamp")),
        }
        _write_json_atomic(self.metadata_path(day), next_metadata)
        if count // DEFAULT_BLOCK_RECORDS > old_count // DEFAULT_BLOCK_RECORDS:
            try:
                extend_open_index(path, timestamp_field="received_at", iso_timestamp=True,
                                  validate_record=_validate_index_record)
            except Exception:
                # The index is disposable and must never affect Projection or Raw writes.
                pass
        self._status.update(state="available",
                            records_written=self._status["records_written"] + len(projected),
                            last_error=None, last_flush_at=time.time(),
                            last_success=time.time())
        new_size = path.stat().st_size
        if old_size == 0 and old_status is None:
            self._storage["projection_days"] += 1
        self._storage["projection_bytes"] += new_size - old_size
        if old_status in {"open", "complete"}:
            self._storage[f"{old_status}_days"] = max(
                0, self._storage[f"{old_status}_days"] - 1)
        self._storage["open_days"] += 1
        self._pending.clear(); self._pending_day = None
        self._last_flush = self.monotonic()


def load_projection_metadata(directory, day):
    path = Path(directory) / f"{day}.meta.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_metadata_path(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def projection_plan(raw_path: Path, projection_directory, *, current_day=None):
    """Return a validated per-day source plan without scanning either data file."""
    day = raw_path.stem
    projection_path = Path(projection_directory) / f"{day}.jsonl"
    metadata = load_projection_metadata(projection_directory, day)
    if not projection_path.is_file():
        return {"mode": "raw", "raw_path": raw_path,
                "fallback_reason": "projection_missing"}
    if not metadata:
        return {"mode": "raw", "raw_path": raw_path,
                "fallback_reason": "metadata_invalid"}
    try:
        raw_stat = raw_path.stat(); projection_stat = projection_path.stat()
        confirmed = int(metadata["confirmed_raw_end_offset"])
        today = (current_day or datetime.now(timezone.utc).date().isoformat())
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("schema_unsupported")
        if day < today and metadata.get("status") != "complete":
            raise RuntimeError("projection_incomplete")
        valid = (metadata.get("raw_filename") == raw_path.name
                 and metadata.get("status") in {"open", "complete"}
                 and 0 <= confirmed <= raw_stat.st_size
                 and int(metadata["projection_end_offset"]) == projection_stat.st_size)
        if metadata.get("status") == "complete":
            if not (confirmed == raw_stat.st_size
                    and int(metadata.get("raw_size", -1)) == raw_stat.st_size
                    and int(metadata.get("raw_mtime_ns", -1)) == raw_stat.st_mtime_ns):
                raise RuntimeError("raw_changed")
        record_count = int(metadata.get("projection_record_count", 0))
        trailing_skipped = int(metadata.get("trailing_skipped_records", 0))
        if record_count:
            last_offset = _last_projection_offset(projection_path)
            if last_offset != int(metadata.get("last_projection_source_raw_end_offset", -1)):
                raise RuntimeError("metadata_invalid")
            if trailing_skipped == 0 and last_offset != confirmed:
                raise RuntimeError("metadata_invalid")
        if not valid:
            raise RuntimeError("metadata_invalid")
    except RuntimeError as exc:
        return {"mode": "raw", "raw_path": raw_path,
                "fallback_reason": str(exc)}
    except (OSError, KeyError, TypeError, ValueError):
        return {"mode": "raw", "raw_path": raw_path,
                "fallback_reason": "metadata_invalid"}
    return {"mode": "projection" if confirmed == raw_stat.st_size else "projection_tail",
            "raw_path": raw_path, "projection_path": projection_path,
            "metadata_path": Path(projection_directory) / f"{day}.meta.json",
            "raw_offset": confirmed, "metadata": metadata}


class HycubeProjectionBackfill:
    """One-file-at-a-time, resumable projection builder with bounded chunks."""

    def __init__(self, raw_directory, projection_directory, *,
                 max_records=BACKFILL_MAX_RECORDS, max_bytes=BACKFILL_MAX_BYTES,
                 pause_seconds=1.0, scan_interval_seconds=60.0,
                 clock=time.time, live_store=None, cell_history_directory=None):
        self.raw_directory = Path(raw_directory)
        self.projection_directory = Path(projection_directory)
        self.max_records = max(1, int(max_records)); self.max_bytes = max(1, int(max_bytes))
        self.pause_seconds = max(0.0, float(pause_seconds)); self.clock = clock
        self.scan_interval_seconds = max(1.0, float(scan_interval_seconds))
        self.live_store = live_store
        self.cell_history_directory = (Path(cell_history_directory)
                                       if cell_history_directory is not None else None)
        self._stop = threading.Event(); self._thread = None; self._lock = threading.Lock()
        self._historical_requested = threading.Event()
        self._historical_active = False
        self._status = {"status": "idle", "current_file": None, "files_total": 0,
                        "files_completed": 0, "bytes_processed": 0,
                        "records_processed": 0, "records_written": 0,
                        "errors": 0, "started_at": None, "updated_at": None,
                        "completed_at": None, "last_error": None}

    def status(self):
        with self._lock: return dict(self._status)

    def _set(self, **values):
        with self._lock: self._status.update(values)

    def start(self):
        if self._thread and self._thread.is_alive(): return False
        self._stop.clear(); self._thread = threading.Thread(
            target=self._run, name="guardian-hycube-projection-backfill", daemon=True)
        self._thread.start(); return True

    def request_historical_backfill(self):
        """Idempotently request the explicit non-destructive maintenance migration."""
        if not (self._thread and self._thread.is_alive()):
            self.start()
        with self._lock:
            if self._historical_requested.is_set() or self._historical_active:
                return False
            self._historical_requested.set()
            return True

    def stop(self, timeout=5.0):
        self._stop.set()
        self._historical_requested.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not (self._thread and self._thread.is_alive())

    def run_once(self, *, include_current=True, include_historical=True):
        paths = (sorted(path for path in self.raw_directory.glob("*.jsonl")
                        if DAY_FILE_RE.fullmatch(path.name))
                 if self.raw_directory.exists() else [])
        current_day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        paths = [path for path in paths
                 if ((include_current and path.stem == current_day)
                     or (include_historical and path.stem < current_day))]
        # Explicit migration accelerates recent History first; current catchup is separate.
        paths.sort(key=lambda path: path.stem, reverse=True)
        self._set(status="running", files_total=len(paths), started_at=self.clock(),
                  files_completed=0, bytes_processed=0, records_processed=0,
                  records_written=0, errors=0, completed_at=None, last_error=None)
        complete = 0
        for path in paths:
            if self._stop.is_set(): break
            self._set(current_file=path.name, updated_at=self.clock())
            try:
                done = False
                current_day = datetime.fromtimestamp(
                    self.clock(), timezone.utc).date().isoformat()
                while not done and not self._stop.is_set():
                    done = (self.live_store.coordinated(self._process_chunk, path)
                            if self.live_store is not None and path.stem == current_day
                            else self._process_chunk(path))
                    if done and self.live_store is not None and path.stem != current_day:
                        self.live_store.refresh_storage()
                    if path.stem >= current_day:
                        break
                    if not done and self.pause_seconds and self._stop.wait(self.pause_seconds):
                        break
                if done: complete += 1
            except Exception as exc:
                self._set(errors=self.status()["errors"] + 1,
                          last_error=f"{type(exc).__name__}: {str(exc)[:120]}")
            self._set(files_completed=complete, updated_at=self.clock())
            if self.pause_seconds and self._stop.wait(self.pause_seconds): break
        if include_historical and not self._stop.is_set():
            self._rebuild_cell_indexes(current_day)
        stopped = self._stop.is_set()
        self._set(status="stopped" if stopped else "completed", current_file=None,
                  completed_at=self.clock() if not stopped else None, updated_at=self.clock())
        return self.status()

    def _rebuild_cell_indexes(self, current_day):
        """Reuse the explicit maintenance migration for existing Cell History."""
        if self.cell_history_directory is None or not self.cell_history_directory.exists():
            return
        for path in sorted(self.cell_history_directory.glob("*.jsonl")):
            if self._stop.is_set() or path.stem >= current_day:
                break
            try:
                selected_ranges(path, float("-inf"), float("inf"),
                                timestamp_field="timestamp", iso_timestamp=False)
            except BlockIndexError:
                try:
                    build_index(path, timestamp_field="timestamp", iso_timestamp=False,
                                complete=True, validate_record=_validate_cell_index_record)
                except Exception:
                    pass
            except Exception:
                pass
            if self.pause_seconds and self._stop.wait(self.pause_seconds):
                break

    def _run(self):
        # Startup recovery is deliberately limited to the active UTC day.
        self.run_once(include_current=True, include_historical=False)
        while not self._stop.is_set():
            if self._historical_requested.wait(self.scan_interval_seconds):
                self._historical_requested.clear()
                if self._stop.is_set():
                    break
                with self._lock:
                    self._historical_active = True
                try:
                    self.run_once(include_current=False, include_historical=True)
                finally:
                    with self._lock:
                        self._historical_active = False
            else:
                self.run_once(include_current=True, include_historical=False)

    def _process_chunk(self, raw_path):
        day = raw_path.stem
        current_day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        historical = day < current_day
        final_projection_path = self.projection_directory / f"{day}.jsonl"
        final_metadata_path = self.projection_directory / f"{day}.meta.json"
        if historical and projection_plan(raw_path, self.projection_directory)["mode"] == "projection":
            try:
                selected_ranges(final_projection_path, float("-inf"), float("inf"),
                                timestamp_field="received_at", iso_timestamp=True)
            except BlockIndexError:
                try:
                    build_index(final_projection_path, timestamp_field="received_at",
                                iso_timestamp=True, complete=True,
                                validate_record=_validate_index_record)
                except Exception:
                    pass
            except Exception:
                pass
            return True
        projection_path = (self.projection_directory / f"{day}.jsonl.building"
                           if historical else final_projection_path)
        metadata_path = (self.projection_directory / f"{day}.meta.json.building"
                         if historical else final_metadata_path)
        metadata = _load_metadata_path(metadata_path)
        offset = int(metadata.get("confirmed_raw_end_offset", 0)) if metadata else 0
        projection_offset = int(metadata.get("projection_end_offset", 0)) if metadata else 0
        record_count = int(metadata.get("projection_record_count", 0)) if metadata else 0
        last_projection_offset = (metadata.get("last_projection_source_raw_end_offset")
                                  if metadata else None)
        trailing_skipped = int(metadata.get("trailing_skipped_records", 0)) if metadata else 0
        first = metadata.get("first_timestamp") if metadata else None
        last = metadata.get("last_timestamp") if metadata else None
        raw_stat = raw_path.stat()
        self.projection_directory.mkdir(parents=True, exist_ok=True)
        if projection_path.exists() and projection_path.stat().st_size != projection_offset:
            with projection_path.open("r+b") as handle: handle.truncate(projection_offset)
        processed = written = bytes_processed = 0; lines = []; last_offset = offset
        with raw_path.open("rb") as handle:
            handle.seek(offset)
            while processed < self.max_records and bytes_processed < self.max_bytes:
                line = handle.readline()
                if not line: break
                last_offset = handle.tell(); bytes_processed += len(line); processed += 1
                try: record = json.loads(line)
                except json.JSONDecodeError as exc: raise ValueError("invalid raw JSON") from exc
                item = projection_record(record, last_offset)
                if item is None:
                    trailing_skipped += 1
                    continue
                received = item["received_at"]
                last_projection_offset = last_offset; trailing_skipped = 0
                lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                first = first or received; last = received; written += 1
        if lines:
            with projection_path.open("a", encoding="utf-8") as handle:
                handle.writelines(lines); handle.flush()
        final_raw_stat = raw_path.stat()
        caught_up = last_offset == final_raw_stat.st_size
        status = "complete" if caught_up and day < current_day else "open"
        next_metadata = {"schema_version": SCHEMA_VERSION, "status": status,
            "raw_filename": raw_path.name, "raw_size": final_raw_stat.st_size,
            "raw_mtime_ns": final_raw_stat.st_mtime_ns,
            "confirmed_raw_end_offset": last_offset,
            "projection_end_offset": projection_path.stat().st_size if projection_path.exists() else 0,
            "projection_record_count": record_count + written,
            "last_projection_source_raw_end_offset": last_projection_offset,
            "trailing_skipped_records": trailing_skipped,
            "first_timestamp": first, "last_timestamp": last,
            "raw_records_considered": processed, "projection_records_written": written,
            "parse_errors": 0, "skipped_records": processed - written}
        _write_json_atomic(metadata_path, next_metadata)
        if status == "complete" and historical:
            os.replace(projection_path, final_projection_path)
            os.replace(metadata_path, final_metadata_path)
            try:
                build_index(final_projection_path, timestamp_field="received_at",
                            iso_timestamp=True, complete=True,
                            validate_record=_validate_index_record)
            except Exception:
                pass
        elif projection_path.exists():
            try:
                for _ in range(1 + written // DEFAULT_BLOCK_RECORDS):
                    extend_open_index(
                        projection_path, timestamp_field="received_at",
                        iso_timestamp=True, validate_record=_validate_index_record)
            except Exception:
                pass
        snapshot = self.status()
        self._set(bytes_processed=snapshot["bytes_processed"] + bytes_processed,
                  records_processed=snapshot["records_processed"] + processed,
                  records_written=snapshot["records_written"] + written)
        return status == "complete"
