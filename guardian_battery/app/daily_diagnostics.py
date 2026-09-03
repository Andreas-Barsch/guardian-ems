"""Deterministic, manually invoked daily diagnostics over persisted evidence.

Importing this module performs no I/O.  Callers explicitly supply every input
and output path; this module never opens Guardian transports or runtime APIs.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from bms_management_evidence import (BmsManagementEvidenceAnalyzer,
                                     BmsManagementEvidenceStore,
                                     EvidenceParameters,
                                     SCHEMA_VERSION as BMS_SCHEMA_VERSION)
from cell_risk_v2 import (CELL_RISK_ALGORITHM_VERSION,
                          analyze_cell_risk)
from position_history import PositionSnapshot


DAILY_SCHEMA_VERSION = 1
CORE_VERSION = "1"
DEFAULT_TIMEZONE = "Europe/Berlin"


class DailyDiagnosticError(RuntimeError):
    """Base failure for a manual daily run."""


class DailyDiagnosticBusyError(DailyDiagnosticError):
    """Another run owns the output-root lock."""


class SourceChangedError(DailyDiagnosticError):
    """An input file changed while its snapshot was being read."""


@dataclass(frozen=True)
class DailyDiagnosticSources:
    cell_history_root: Path | str | None = None
    rs485_history_root: Path | str | None = None
    position_history_path: Path | str | None = None
    config_history_path: Path | str | None = None
    maintenance_history_path: Path | str | None = None


@dataclass(frozen=True)
class GuardianDay:
    diagnostic_date: str
    timezone: str
    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        return self.end.timestamp() - self.start.timestamp()


@dataclass(frozen=True)
class SourceSlice:
    name: str
    records: tuple[Mapping[str, Any], ...]
    provenance: tuple[Mapping[str, Any], ...]
    records_total: int
    records_valid: int
    records_invalid: int
    records_ignored_outside_day: int
    missing: bool

    @property
    def quality(self) -> str:
        if self.missing:
            return "missing"
        if self.records_invalid:
            return "partial"
        return "complete"


@dataclass(frozen=True)
class DailyInputProbe:
    day: GuardianDay
    input_fingerprint: str
    sources: Mapping[str, SourceSlice]
    position_projection: tuple[Mapping[str, Any], ...]
    config_projection: tuple[Mapping[str, Any], ...]
    component_manifest: Mapping[str, Any]
    risk_cell_records: tuple[Mapping[str, Any], ...] = ()


def guardian_day(diagnostic_date: date | str, timezone_name: str = DEFAULT_TIMEZONE) -> GuardianDay:
    parsed = (diagnostic_date if isinstance(diagnostic_date, date)
              else date.fromisoformat(str(diagnostic_date)))
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(parsed, datetime_time(), zone)
    end = datetime.combine(parsed + timedelta(days=1), datetime_time(), zone)
    return GuardianDay(parsed.isoformat(), timezone_name, start, end)


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return parsed.timestamp()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.timestamp()
    raise ValueError("unsupported timestamp")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def _stat(path: Path) -> tuple[int, int, int | None]:
    value = path.stat()
    return value.st_size, value.st_mtime_ns, getattr(value, "st_ino", None)


def _source_files(path: Path | str | None, day: GuardianDay | None = None,
                  discovered: Sequence[Path] | None = None) -> list[Path]:
    if path is None:
        return []
    root = Path(path)
    if discovered is None:
        if root.is_file():
            return [root]
        if not root.exists():
            return []
        files = sorted(item for item in root.rglob("*.jsonl") if item.is_file())
    else:
        files = list(discovered)
    if day is None:
        return files
    # Daily writers use either local or UTC YYYY-MM-DD filenames. Include both
    # possible UTC boundary dates and retain non-date names for compatibility.
    utc_start = day.start.astimezone(timezone.utc).date()
    utc_end = (day.end.astimezone(timezone.utc) - timedelta(microseconds=1)).date()
    relevant_dates = {day.diagnostic_date, utc_start.isoformat(), utc_end.isoformat()}
    selected = []
    for item in files:
        try:
            file_date = date.fromisoformat(item.stem[:10]).isoformat()
        except ValueError:
            selected.append(item)
        else:
            if file_date in relevant_dates:
                selected.append(item)
    return selected


def _slice_jsonl(name: str, path: Path | str | None, day: GuardianDay,
                 *, timestamp_field: str = "timestamp",
                 include_history_before_day: bool = False,
                 discovered: Sequence[Path] | None = None) -> SourceSlice:
    files = _source_files(path, day, discovered)
    missing = path is None or not Path(path).exists()
    used: list[Mapping[str, Any]] = []
    provenance = []
    totals = {"total": 0, "valid": 0, "invalid": 0, "outside": 0}
    start, end = day.start.timestamp(), day.end.timestamp()
    for source in files:
        before = _stat(source)
        raw = source.read_bytes()
        after = _stat(source)
        if before != after:
            raise SourceChangedError(f"source changed during read: {source}")
        file_counts = {"total": 0, "valid": 0, "invalid": 0,
                       "outside": 0, "used": 0}
        file_used_timestamps = []
        for raw_line in raw.splitlines():
            if not raw_line.strip():
                continue
            file_counts["total"] += 1
            try:
                record = json.loads(raw_line)
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
                timestamp = _timestamp(record[timestamp_field])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError):
                file_counts["invalid"] += 1
                continue
            file_counts["valid"] += 1
            if start <= timestamp < end or (include_history_before_day and timestamp < start):
                used.append(record)
                file_counts["used"] += 1
                file_used_timestamps.append(timestamp)
            else:
                file_counts["outside"] += 1
        for key in ("total", "valid", "invalid", "outside"):
            totals[key] += file_counts[key]
        provenance.append({
            "path": str(source), "logical_name": source.name,
            "size": before[0], "mtime_ns": before[1], "inode": before[2],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "records_total": file_counts["total"],
            "records_valid": file_counts["valid"],
            "records_invalid": file_counts["invalid"],
            "records_used": file_counts["used"],
            "records_ignored_outside_day": file_counts["outside"],
            "first_used_timestamp": _utc_iso(min(file_used_timestamps))
            if file_used_timestamps else None,
            "last_used_timestamp": _utc_iso(max(file_used_timestamps))
            if file_used_timestamps else None,
        })
    used.sort(key=lambda item: (_timestamp(item[timestamp_field]),
                                hashlib.sha256(_canonical(item)).hexdigest()))
    return SourceSlice(name, tuple(used), tuple(provenance), totals["total"],
                       totals["valid"], totals["invalid"], totals["outside"], missing)


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _history_projection(records: Sequence[Mapping[str, Any]], day: GuardianDay,
                        timestamp_field: str) -> list[Mapping[str, Any]]:
    """Keep the last effective record before the day plus changes in the day."""
    start, end = day.start.timestamp(), day.end.timestamp()
    ordered = sorted(records, key=lambda item: (_timestamp(item[timestamp_field]),
                                                hashlib.sha256(_canonical(item)).hexdigest()))
    before = [item for item in ordered if _timestamp(item[timestamp_field]) < start]
    relevant = [item for item in ordered if start <= _timestamp(item[timestamp_field]) < end]
    return ([before[-1]] if before else []) + relevant


def _position_resolver(records: Sequence[Mapping[str, Any]]) -> Callable[[str, str], Any] | None:
    snapshots = []
    for record in records:
        try:
            snapshots.append(PositionSnapshot.from_dict(dict(record)))
        except (TypeError, ValueError):
            continue
    snapshots.sort(key=lambda item: (item.effective_at, item.created_at,
                                     item.position_history_id))
    if not snapshots:
        return None

    def resolve(serial: str, timestamp: str):
        target = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
        candidates = [item for item in snapshots
                      if datetime.fromisoformat(item.effective_at).astimezone(timezone.utc) <= target]
        if not candidates:
            return None, None
        snapshot = candidates[-1]
        position = next((int(key) for key, value in snapshot.positions.items()
                         if value == serial), None)
        return position, snapshot.position_history_id

    return resolve


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _output_lock(output_root: Path, timeout_seconds: float):
    lock_path = output_root / "locks" / "daily_job.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DailyDiagnosticBusyError("daily diagnostic output root is busy")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _source_summary(source: SourceSlice) -> dict[str, Any]:
    return {
        "quality": source.quality, "missing": source.missing,
        "records_total": source.records_total, "records_valid": source.records_valid,
        "records_invalid": source.records_invalid, "records_used": len(source.records),
        "records_ignored_outside_day": source.records_ignored_outside_day,
        "files": list(source.provenance),
    }


def _component_manifest(parameters: EvidenceParameters) -> dict[str, Any]:
    return {"bms_management": {"component_version": "1",
                               "schema_version": BMS_SCHEMA_VERSION,
                               "parameters": asdict(parameters)},
            "cell_risk": {"component_version": "2",
                          "cell_risk_algorithm_version": CELL_RISK_ALGORITHM_VERSION,
                          "lookback_days": 14}}


def _risk_history_records(path: Path | str | None, day: GuardianDay) -> tuple[Mapping[str, Any], ...]:
    """Read the current and preceding 13 Guardian days for the daily job only."""
    records = []
    for offset in range(13, -1, -1):
        target = date.fromisoformat(day.diagnostic_date) - timedelta(days=offset)
        records.extend(_slice_jsonl("cell", path, guardian_day(target, day.timezone)).records)
    records.sort(key=lambda item: (_timestamp(item["timestamp"]),
                                   hashlib.sha256(_canonical(item)).hexdigest()))
    return tuple(records)


def build_source_catalog(sources: DailyDiagnosticSources) -> dict[str, tuple[Path, ...]]:
    """Discover source filenames once without reading record contents."""
    paths = {
        "rs485": sources.rs485_history_root,
        "cell": sources.cell_history_root,
        "position_history": sources.position_history_path,
        "config_history": sources.config_history_path,
        "maintenance_history": sources.maintenance_history_path,
    }
    return {name: tuple(_source_files(path)) for name, path in paths.items()}


def probe_daily_inputs(diagnostic_date: date | str, sources: DailyDiagnosticSources,
                       *, timezone_name: str = DEFAULT_TIMEZONE,
                       bms_parameters: EvidenceParameters | None = None,
                       source_catalog: Mapping[str, Sequence[Path]] | None = None
                       ) -> DailyInputProbe:
    """Slice and fingerprint one day without executing any diagnostic component."""
    day = guardian_day(diagnostic_date, timezone_name)
    parameters = bms_parameters or EvidenceParameters(daily_timezone=timezone_name)
    if parameters.daily_timezone != timezone_name:
        raise ValueError("BMS daily timezone must match the Guardian day timezone")
    catalog = source_catalog or {}
    rs485 = _slice_jsonl("rs485", sources.rs485_history_root, day,
                         discovered=catalog.get("rs485"))
    cells = _slice_jsonl("cell", sources.cell_history_root, day,
                         discovered=catalog.get("cell"))
    positions = _slice_jsonl("position_history", sources.position_history_path, day,
                             timestamp_field="effective_at",
                             include_history_before_day=True,
                             discovered=catalog.get("position_history"))
    configs = _slice_jsonl("config_history", sources.config_history_path, day,
                           include_history_before_day=True,
                           discovered=catalog.get("config_history"))
    maintenance = _slice_jsonl("maintenance_history", sources.maintenance_history_path, day,
                               timestamp_field="occurred_at",
                               discovered=catalog.get("maintenance_history"))
    position_projection = tuple(_history_projection(
        positions.records, day, "effective_at"))
    config_projection = tuple(_history_projection(configs.records, day, "timestamp"))
    manifest = _component_manifest(parameters)
    risk_records = _risk_history_records(sources.cell_history_root, day)
    semantic = {
        "timezone": timezone_name, "diagnostic_date": day.diagnostic_date,
        "daily_schema_version": DAILY_SCHEMA_VERSION,
        "core_version": CORE_VERSION, "component_manifest": manifest,
        "rs485_records": list(rs485.records), "cell_records": list(cells.records),
        "cell_risk_records": list(risk_records),
        "position_projection": list(position_projection),
        "config_projection": list(config_projection),
        "maintenance_records": list(maintenance.records),
    }
    fingerprint = hashlib.sha256(_canonical(semantic)).hexdigest()
    return DailyInputProbe(
        day, fingerprint,
        {item.name: item for item in (rs485, cells, positions, configs, maintenance)},
        position_projection, config_projection, manifest, risk_records)


def _component_result(result: Mapping[str, Any], rs485: SourceSlice,
                      cells: SourceSlice, position: SourceSlice,
                      diagnostic_date: str) -> dict[str, Any]:
    aggregates = [item for item in result["daily_aggregates"]
                  if item.get("day") == diagnostic_date]
    limitations = []
    if rs485.missing or not rs485.records:
        limitations.append("rs485_source_missing_or_empty")
    if cells.missing or not cells.records:
        limitations.append("cell_source_missing_or_empty")
    if position.missing or not position.records:
        limitations.append("position_history_missing_or_empty")
    if not aggregates:
        limitations.append("no_bms_management_evidence")
    corrupt = [item.name for item in (rs485, cells, position) if item.records_invalid]
    if corrupt:
        limitations.append("invalid_records:" + ",".join(corrupt))
    status = "partial" if limitations else "complete"
    return {
        "component_name": "bms_management", "component_version": "1",
        "schema_version": BMS_SCHEMA_VERSION, "status": status,
        "coverage": {"rs485_records": len(rs485.records),
                     "cell_records": len(cells.records),
                     "physical_serials": sorted({item["physical_serial"] for item in aggregates})},
        "metrics": {"aggregate_count": len(aggregates),
                    "relative_limit_observation_count": len(result["relative_limits"])},
        "events": {"count": len(result["events"]),
                   "store": f"events/bms_management/{diagnostic_date}.jsonl"},
        "quality": "limited" if limitations else "complete",
        "warnings": limitations, "errors": [],
        "provenance": {"analyzer": "BmsManagementEvidenceAnalyzer",
                       "causality": result["causality"],
                       "identity": "physical_serial",
                       "position": "position_at_sample_time",
                       "historical_guardian_config_required": False},
    }


def run_daily_diagnostic(diagnostic_date: date | str, sources: DailyDiagnosticSources,
                         output_root: Path | str, *, timezone_name: str = DEFAULT_TIMEZONE,
                         bms_parameters: EvidenceParameters | None = None,
                         lock_timeout_seconds: float = 0.0,
                         clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Analyze one explicit Guardian day and atomically publish its revision."""
    day = guardian_day(diagnostic_date, timezone_name)
    output = Path(output_root)
    parameters = bms_parameters or EvidenceParameters(daily_timezone=timezone_name)
    if parameters.daily_timezone != timezone_name:
        raise ValueError("BMS daily timezone must match the Guardian day timezone")
    now = clock or (lambda: datetime.now(timezone.utc))
    attempt_id = str(uuid.uuid4())
    started = now().astimezone(timezone.utc).isoformat()
    with _output_lock(output, lock_timeout_seconds):
        probe = probe_daily_inputs(day.diagnostic_date, sources,
                                   timezone_name=timezone_name,
                                   bms_parameters=parameters)
        rs485, cells = probe.sources["rs485"], probe.sources["cell"]
        positions_all = probe.sources["position_history"]
        configs_all = probe.sources["config_history"]
        maintenance = probe.sources["maintenance_history"]
        positions, configs = probe.position_projection, probe.config_projection
        manifest, fingerprint = probe.component_manifest, probe.input_fingerprint
        result_hash = hashlib.sha256(_canonical({
            "diagnostic_date": day.diagnostic_date, "timezone": timezone_name,
            "schema_version": DAILY_SCHEMA_VERSION, "component_manifest": manifest,
            "input_fingerprint": fingerprint,
        })).hexdigest()
        daily_result_id = f"DDR-{day.diagnostic_date}-{result_hash[:16]}"

        analyzer_result = None
        risk_result = None
        component_errors = []
        try:
            analyzer_result = BmsManagementEvidenceAnalyzer(parameters).analyze(
                rs485.records, cells.records,
                position_resolver=_position_resolver(positions_all.records))
        except Exception as exc:  # component isolation boundary
            component_errors.append({"type": type(exc).__name__, "message": str(exc)})
        if analyzer_result is None or (not rs485.records and not cells.records):
            return {
                "schema_version": DAILY_SCHEMA_VERSION,
                "diagnostic_date": day.diagnostic_date, "timezone": timezone_name,
                "daily_result_id": daily_result_id, "input_fingerprint": fingerprint,
                "attempt_id": attempt_id, "overall_status": "failed",
                "errors": component_errors or [{"type": "NoUsableInput",
                                                  "message": "no usable component input"}],
                "persisted": False,
            }

        component = _component_result(analyzer_result, rs485, cells, positions_all,
                                      day.diagnostic_date)
        risk_errors = []
        try:
            risk_result = analyze_cell_risk(
                probe.risk_cell_records,
                diagnostic_date=day.diagnostic_date,
                maintenance_records=maintenance.records,
                position_resolver=_position_resolver(positions_all.records),
                timezone_name=timezone_name)
        except Exception as exc:  # independent predictive analytics boundary
            risk_errors.append({"type": type(exc).__name__, "message": str(exc)})
        risk_component = {
            "component_name": "cell_risk", "component_version": "2",
            "cell_risk_algorithm_version": CELL_RISK_ALGORITHM_VERSION,
            "status": "complete" if risk_result is not None else "failed",
            "metrics": {"cell_count": len(risk_result["cells"]) if risk_result else 0},
            "quality": "complete" if risk_result and risk_result["cells"] else "limited",
            "warnings": (["no_qualifying_cell_risk_samples"]
                         if risk_result is not None and not risk_result["cells"] else []),
            "errors": risk_errors,
            "provenance": {"identity": "physical_serial+cell_number",
                           "causality": "not_determined",
                           "score_semantics": "engineering_priority_not_soh"},
        }
        source_map = {item.name: _source_summary(item) for item in
                      (rs485, cells, positions_all, configs_all, maintenance)}
        overall = "partial" if component["status"] == "partial" else "complete"
        aggregate_path = output / "aggregates" / "bms_management" / f"{day.diagnostic_date}.json"
        event_path = output / "events" / "bms_management" / f"{day.diagnostic_date}.jsonl"
        store = BmsManagementEvidenceStore(event_path, aggregate_path)
        appended = store.append_events(analyzer_result["events"])
        aggregates = [item for item in analyzer_result["daily_aggregates"]
                      if item.get("day") == day.diagnostic_date]
        store.save_daily_aggregates(aggregates)
        risk_path = output / "aggregates" / "cell_risk" / f"{day.diagnostic_date}.json"
        if risk_result is not None:
            _atomic_json(risk_path, risk_result)
        component["events"]["appended"] = appended
        component["metrics"]["aggregate_store"] = str(
            aggregate_path.relative_to(output))
        completed = now().astimezone(timezone.utc).isoformat()
        payload = {
            "schema_version": DAILY_SCHEMA_VERSION,
            "diagnostic_date": day.diagnostic_date, "timezone": timezone_name,
            "day_start": day.start.isoformat(), "day_end": day.end.isoformat(),
            "day_duration_seconds": day.duration_seconds,
            "daily_result_id": daily_result_id, "input_fingerprint": fingerprint,
            "overall_status": overall, "sources": source_map,
            "components": {"bms_management": component, "cell_risk": risk_component},
            "trend_inputs": {"bms_management_aggregates":
                             f"aggregates/bms_management/{day.diagnostic_date}.json",
                             "cell_risk_aggregates":
                             f"aggregates/cell_risk/{day.diagnostic_date}.json"},
            "provenance": {"core_version": CORE_VERSION,
                           "component_manifest": manifest,
                           "config_history_projection": configs,
                           "maintenance_history_records": list(maintenance.records)},
            "execution": {"attempt_id": attempt_id, "started_at": started,
                          "completed_at": completed},
        }
        day_dir = output / "daily" / day.diagnostic_date
        result_path = day_dir / f"{fingerprint}.json"
        if not result_path.exists():
            _atomic_json(result_path, payload)
        index = {"schema_version": DAILY_SCHEMA_VERSION,
                 "diagnostic_date": day.diagnostic_date,
                 "daily_result_id": daily_result_id,
                 "input_fingerprint": fingerprint,
                 "result": result_path.name, "overall_status": overall,
                 "updated_at": completed}
        _atomic_json(day_dir / "index.json", index)
        return {**payload, "persisted": True, "result_path": str(result_path),
                "index_path": str(day_dir / "index.json")}
