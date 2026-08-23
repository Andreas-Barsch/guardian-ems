"""Compact, versioned daily aggregates for long-term cell diagnostics.

Raw JSONL history remains append-only and untouched.  This separate store uses
an atomic replace for its own derived data and keeps at most a configured number
of UTC days.  Duplicate sample timestamps are ignored per aggregate key.
"""

from __future__ import annotations

import json
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


class DiagnosticAggregateStore:
    """Persist bounded daily/phase/cell aggregates without touching raw data."""

    def __init__(self, path: Path, phase_classifier, retention_days: int = 730):
        self.path = Path(path)
        self.phase_classifier = phase_classifier
        self.retention_days = max(30, int(retention_days))
        self.records = {}
        self.backfill_sources = {}
        self._dirty = False
        self._load()

    @classmethod
    def in_memory(cls, phase_classifier, retention_days: int = 730):
        instance = cls.__new__(cls)
        instance.path = None
        instance.phase_classifier = phase_classifier
        instance.retention_days = max(30, int(retention_days))
        instance.records = {}
        instance.backfill_sources = {}
        instance._dirty = False
        return instance

    def _load(self):
        try:
            if not self.path.exists():
                return
            value = json.loads(self.path.read_text(encoding="utf-8"))
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
        cutoff = datetime.fromisoformat(latest_day).date() - timedelta(days=self.retention_days - 1)
        self.records = {key: value for key, value in self.records.items()
                        if datetime.fromisoformat(value["day"]).date() >= cutoff}

    def prune_through(self, latest_day: str) -> None:
        before = len(self.records)
        self._prune(latest_day)
        if len(self.records) != before:
            self._dirty = True

    def save(self):
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "schema_version": AGGREGATE_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "retention_days": self.retention_days,
            "backfill_sources": self.backfill_sources,
            "records": self.records,
        }, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self._dirty = False
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

    def mark_source_complete(self, path: Path, signature: dict, current_config_id: str,
                             record_keys: set[str], statistics: dict) -> None:
        coverage = {
            key: {"sample_count": int(value["sample_count"]),
                  "last_timestamp": float(value["last_timestamp"])}
            for key, value in self.records.items()
            if key in record_keys
        }
        self.backfill_sources[self._source_key(path, current_config_id)] = {
            "signature": signature,
            "config_id": current_config_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "days": sorted({self.records[key]["day"] for key in record_keys
                            if key in self.records}),
            "aggregate_coverage": coverage,
            "statistics": dict(statistics),
        }
        self._dirty = True

    def merge_backfill_records(self, records: dict) -> int:
        """Use one raw day as canonical input without double-counting partial state."""
        changed = 0
        for key, value in records.items():
            if self.records.get(key) != value:
                self.records[key] = value
                changed += 1
        if changed:
            self._dirty = True
        return changed

    def for_identity(self, module, serial):
        identity = serial or f"UNKNOWN-POSITION-{int(module)}"
        result = []
        for record in self.records.values():
            expected = record.get("physical_module_serial") or f"UNKNOWN-POSITION-{record.get('module_position')}"
            if expected != identity:
                continue
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
