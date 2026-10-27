"""Compact, versioned daily aggregates for long-term cell diagnostics.

Raw JSONL history remains append-only and untouched.  This separate store uses
an atomic replace for its own derived data and keeps at most a configured number
of UTC days.  Duplicate sample timestamps are ignored per aggregate key.
"""

from __future__ import annotations

import copy
import json
import threading
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config_history import config_id, diagnostic_parameters
from version import DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION


AGGREGATE_SCHEMA_VERSION = 1
PHASES = ("discharge", "low", "charge", "high")


def _ranks(voltages):
    ordered = sorted(set(voltages), reverse=True)
    mapping = {}
    position = 1
    for voltage in ordered:
        count = voltages.count(voltage)
        mapping[voltage] = (position + position + count - 1) / 2
        position += count
    return [mapping[value] for value in voltages]


def _histogram_median(histogram):
    total = sum(histogram.values())
    if not total:
        return None
    targets = ((total - 1) // 2, total // 2)
    values = []
    cumulative = 0
    for raw, count in sorted(histogram.items(), key=lambda item: float(item[0])):
        previous = cumulative
        cumulative += count
        for target in targets:
            if previous <= target < cumulative:
                values.append(float(raw))
    return sum(values) / len(values)



def _state_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return locked


class _PersistencePayload(dict):
    """In-process receipt; generation metadata is never serialized to disk."""

    def __init__(self, values, owner, generation):
        super().__init__(values)
        self.owner = owner
        self.generation = generation


class DiagnosticAggregateStore:
    """Persist bounded daily/phase/cell aggregates without touching raw data."""

    def __init__(self, path: Path, phase_classifier, retention_days: int = 730):
        self.path = Path(path)
        self.phase_classifier = phase_classifier
        self.retention_days = max(30, int(retention_days))
        self.records = {}
        self.backfill_sources = {}
        self._init_generation()
        self._load()

    @classmethod
    def in_memory(cls, phase_classifier, retention_days: int = 730):
        instance = cls.__new__(cls)
        instance.path = None
        instance.phase_classifier = phase_classifier
        instance.retention_days = max(30, int(retention_days))
        instance.records = {}
        instance.backfill_sources = {}
        instance._init_generation()
        return instance

    def _init_generation(self):
        self._state_lock = threading.RLock()
        self._generation = 0
        self._acknowledged_generation = 0
        self._receipt_owner = object()

    @property
    def _dirty(self):
        with self._state_lock:
            return self._generation > self._acknowledged_generation

    @_dirty.setter
    def _dirty(self, changed):
        # Existing startup/backfill assignments remain explicit mutations.
        with self._state_lock:
            if changed:
                self._generation += 1
            else:
                self._acknowledged_generation = self._generation

    @_state_locked
    def acknowledge_persistence(self, payload):
        """Acknowledge only this store's captured, successfully written state.

        Call after persistence, never on enqueue. An older completion cannot
        clear mutations made while its write was in progress.
        """
        if (not isinstance(payload, _PersistencePayload)
                or payload.owner is not self._receipt_owner
                or not 0 <= payload.generation <= self._generation):
            return False
        self._acknowledged_generation = max(
            self._acknowledged_generation, payload.generation)
        return True

    def _load(self):
        try:
            if not self.path.exists():
                return
            from diagnostic_startup_io import load
            value = load(self.path)
            if value.get("schema_version") != AGGREGATE_SCHEMA_VERSION:
                return
            records = value.get("records", {})
            if isinstance(records, dict):
                self.records = records
            sources = value.get("backfill_sources", {})
            if isinstance(sources, dict):
                self.backfill_sources = sources
        except (OSError, ValueError, TypeError):
            self.records = {}

    @staticmethod
    def _identity(sample):
        serial = getattr(sample, "module_serial", None)
        return serial or f"UNKNOWN-POSITION-{int(sample.module)}"

    @staticmethod
    def _key(identity, cell, day, phase, current_config_id):
        return "|".join((identity, str(cell), day, phase, current_config_id))

    @_state_locked
    def add(self, sample, options):
        if len(sample.voltages_mv) != 15:
            return False
        timestamp = float(sample.timestamp)
        day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        identity = self._identity(sample)
        current_config_id = config_id(diagnostic_parameters(options))
        median = sorted(sample.voltages_mv)[7]
        ranks = _ranks(sample.voltages_mv)
        lowest, highest = min(sample.voltages_mv), max(sample.voltages_mv)
        changed = False
        for phase in self.phase_classifier(sample.__dict__, options):
            if phase not in PHASES:
                continue
            for index, voltage in enumerate(sample.voltages_mv):
                key = self._key(identity, index + 1, day, phase, current_config_id)
                record = self.records.get(key)
                if record is None:
                    record = {
                        "schema_version": AGGREGATE_SCHEMA_VERSION,
                        "physical_module_serial": getattr(sample, "module_serial", None),
                        "identity_status": "documented" if getattr(sample, "module_serial", None) else "unknown",
                        "module_position": int(sample.module), "cell": index + 1,
                        "day": day, "phase": phase, "config_id": current_config_id,
                        "guardian_version": GUARDIAN_VERSION,
                        "diagnostic_engine_version": DIAGNOSTIC_ENGINE_VERSION,
                        "sample_count": 0, "rank_sum": 0.0, "lowest_count": 0,
                        "highest_count": 0, "deviation_histogram": {},
                        "first_timestamp": timestamp, "last_timestamp": -1,
                    }
                    self.records[key] = record
                if timestamp <= float(record.get("last_timestamp", -1)):
                    continue
                # Published records are immutable to readers. Copy only the
                # changed cell/phase and its histogram, never the whole history.
                record = dict(record)
                record["deviation_histogram"] = dict(record["deviation_histogram"])
                self.records[key] = record
                deviation = round(float(voltage - median), 1)
                histogram = record["deviation_histogram"]
                histogram[str(deviation)] = int(histogram.get(str(deviation), 0)) + 1
                record["sample_count"] += 1
                record["rank_sum"] += ranks[index]
                record["lowest_count"] += voltage == lowest
                record["highest_count"] += voltage == highest
                record["first_timestamp"] = min(float(record["first_timestamp"]), timestamp)
                record["last_timestamp"] = timestamp
                changed = True
        if changed:
            self._dirty = True
            self._prune(day)
        return changed

    def _prune(self, latest_day):
        incremental = getattr(self.records, "prune_records", None)
        if incremental is not None:
            incremental(latest_day, self.retention_days)
            return
        cutoff = datetime.fromisoformat(latest_day).date() - timedelta(days=self.retention_days - 1)
        self.records = {key: value for key, value in self.records.items()
                        if datetime.fromisoformat(value["day"]).date() >= cutoff}

    @_state_locked
    def prune_through(self, latest_day: str) -> None:
        before = len(self.records)
        self._prune(latest_day)
        if len(self.records) != before:
            self._dirty = True

    def save(self):
        from diagnostic_startup_io import active, checkpoint
        if active():
            checkpoint()
            return False  # Keep original caches until a complete handover.
        if not self._dirty:
            return False
        payload = self.persistence_payload()
        if not self.persist_payload(payload):
            return False
        self.acknowledge_persistence(payload)
        return True

    @_state_locked
    def persistence_payload(self):
        if not self._dirty:
            return None
        return _PersistencePayload({
            "schema_version": AGGREGATE_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "retention_days": self.retention_days,
            # Membership is frozen; add/merge/mark replace entries rather
            # than mutating any record already handed to a worker.
            "backfill_sources": dict(self.backfill_sources),
            "records": dict(self.records),
        }, self._receipt_owner, self._generation)

    def persist_payload(self, payload):
        if payload is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        return True

    @staticmethod
    def source_signature(path: Path):
        stat = Path(path).stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    @staticmethod
    def _source_key(path: Path, current_config_id: str):
        return f"{Path(path).name}|{current_config_id}"

    def source_complete(self, path: Path, signature: dict, current_config_id: str) -> bool:
        """Return true only when the unchanged source's recorded coverage still exists."""
        state = self.backfill_sources.get(self._source_key(path, current_config_id))
        if not isinstance(state, dict) or state.get("signature") != signature:
            return False
        for key, expected in state.get("aggregate_coverage", {}).items():
            actual = self.records.get(key)
            if (not actual or int(actual.get("sample_count", 0)) < int(expected["sample_count"])
                    or float(actual.get("last_timestamp", -1)) < float(expected["last_timestamp"])):
                return False
        return True

    @_state_locked
    def mark_source_complete(self, path: Path, signature: dict, current_config_id: str,
                             record_keys: set[str], statistics: dict) -> None:
        coverage = {
            key: {"sample_count": int(value["sample_count"]),
                  "last_timestamp": float(value["last_timestamp"])}
            for key, value in self.records.items()
            if key in record_keys
        }
        self.backfill_sources[self._source_key(path, current_config_id)] = {
            "signature": copy.deepcopy(signature),
            "config_id": current_config_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "days": sorted({self.records[key]["day"] for key in record_keys
                            if key in self.records}),
            "aggregate_coverage": coverage,
            "statistics": dict(statistics),
        }
        self._dirty = True

    @_state_locked
    def merge_backfill_records(self, records: dict) -> int:
        """Use one raw day as canonical input without double-counting partial state."""
        changed = 0
        for key, value in records.items():
            if self.records.get(key) != value:
                self.records[key] = copy.deepcopy(value)
                changed += 1
        if changed:
            self._dirty = True
        return changed

    def records_for_identity(self, module, serial):
        """Freeze membership of immutable records; no histogram evaluation."""
        indexed = getattr(self.records, "records_for_identity", None)
        if indexed is not None:
            return tuple(indexed(module, serial))
        identity = serial or f"UNKNOWN-POSITION-{int(module)}"
        return tuple(record for record in self.records.values()
                     if (record.get("physical_module_serial") or
                         f"UNKNOWN-POSITION-{record.get('module_position')}") == identity)

    def for_identity(self, module, serial):
        return self.project_records(self.records_for_identity(module, serial))

    @staticmethod
    def project_records(records):
        """Existing diagnostic projection, also used off-thread on a snapshot."""
        result = []
        for record in records:
            count = int(record["sample_count"])
            result.append({
                **{key: value for key, value in record.items()
                   if key not in {"rank_sum", "deviation_histogram", "lowest_count", "highest_count"}},
                "mean_rank": round(float(record["rank_sum"]) / count, 4) if count else None,
                "median_deviation_mv": _histogram_median(record["deviation_histogram"]),
                "lowest_percent": round(100 * int(record["lowest_count"]) / count, 2) if count else 0,
                "highest_percent": round(100 * int(record["highest_count"]) / count, 2) if count else 0,
            })
        return sorted(result, key=lambda item: (item["day"], item["phase"], item["cell"], item["config_id"]))
