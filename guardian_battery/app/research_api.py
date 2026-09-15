"""Stable, read-only HTTP-independent Guardian Research API."""
from __future__ import annotations

import hashlib
import base64
import json
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from canonical_phase import CanonicalPhaseReader
from display_history_reader import DisplayHistoryReader
from history_series import CellHistorySeries
from hycube_evidence import HycubeBatteryCapacitySeries, HycubePolicyHistory
from guardian_diagnostics import GuardianDiagnosticsRepository
from maintenance import MaintenanceEventLog
from maintenance_api import ApiResponse
from maintenance_service import MaintenanceRepository
from timeline import TechnicalEventSource
from research_identity import ResearchIdentityResolver
from research_timeseries import (CursorCodec, MAX_CELLS, MAX_POINTS,
                                 ResearchQueryError, ResearchTimeseriesService)
from rs485_evidence import decode_identity_record

API_ROUTE = "/api/research"
SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SERIALS = 6
HARD_TIMEOUT_SECONDS = 15
QUERY_TIMEOUT_SECONDS = 10
SOC_CRASH_VERSION = "guardian_soc_crash_v1"
EVIDENCE_PACKAGE_VERSION = "research_soc_crash_evidence_v2"
LOG = logging.getLogger("guardian_battery.research")

PACKAGE_PROFILE_STAGES = (
    "event_id_decode_checksum", "bounded_event_reconstruction", "event_match",
    "identity_epoch_resolution", "historical_position_resolution",
    "evidence_window_calculation", "module_soc", "module_current",
    "module_voltage", "power_derived", "cell_voltages", "temperature_channels",
    "cell_context_derived", "cell_minimum", "cell_maximum", "cell_spread",
    "lowest_cell", "highest_cell", "median_deviations", "canonical_phase",
    "maintenance", "alarms", "low_voltage", "bms_management", "dcl", "ccl",
    "charge_enable", "discharge_enable", "command_0x44", "balancing", "soc_recalibration",
    "hycube", "policy", "daily_diagnostics", "peer_topology_resolution",
    "peer_module_evidence", "comparison_window_construction",
    "coverage_calculation", "config_context", "provenance_fingerprint",
    "envelope_build", "serialization")


@dataclass(frozen=True)
class ResearchPaths:
    cell_history: Path
    position_history: Path
    maintenance: Path
    canonical_phase: Path
    daily_diagnostics: Path
    technical_events: Path | None = None
    hycube_history: Path | None = None
    hycube_projection: Path | None = None
    hycube_policy: Path | None = None
    display_history: Path | None = None
    config_history: Path | None = None
    rs485_history: Path | None = None


def research_envelope(*, source, evidence_class, authoritative, timestamp_from,
                      timestamp_to, resolution, data, quality="complete",
                      schema_version=1, semantics_version=None, config_revision=None,
                      truncated=False, next_cursor=None, provenance=None):
    result = {"research_schema_version": SCHEMA_VERSION, "data_source": source,
        "evidence_class": evidence_class, "authoritative": bool(authoritative),
        "schema_version": schema_version, "semantics_version": semantics_version,
        "config_revision": config_revision,
        "timestamp_range": {"from": timestamp_from, "to": timestamp_to},
        "resolution": resolution, "quality": {"status": quality, "confidence": None},
        "truncated": bool(truncated), "next_cursor": next_cursor, "data": data}
    if provenance:
        result["provenance"] = {key: value for key, value in provenance.items()
                                if value is not None}
    return result


class QueryGate:
    """Two queries overall, at most one full-resolution query."""
    def __init__(self):
        self.all = threading.BoundedSemaphore(2); self.raw = threading.BoundedSemaphore(1)
        self.lock = threading.Lock(); self.active = self.failures = 0
        self.queued = 0; self.last_query_at = None; self.audit = []

    def run(self, endpoint, full_resolution, callback):
        if not self.all.acquire(blocking=False):
            raise ResearchQueryError("busy", "research query capacity exhausted", 429)
        acquired_raw = False; started = time.monotonic(); request_id = str(uuid.uuid4())
        status, result = "failed", None
        try:
            if full_resolution:
                acquired_raw = self.raw.acquire(blocking=False)
                if not acquired_raw:
                    raise ResearchQueryError("busy", "full-resolution query busy", 429)
            with self.lock: self.active += 1
            result = callback(started + QUERY_TIMEOUT_SECONDS)
            if time.monotonic() - started > HARD_TIMEOUT_SECONDS:
                raise ResearchQueryError("timeout", "research query exceeded hard deadline", 503)
            size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            if size > MAX_RESPONSE_BYTES:
                raise ResearchQueryError("response_too_large", "response exceeds 2 MiB", 413)
            status = "ok"
            return result
        except Exception:
            with self.lock: self.failures += 1
            raise
        finally:
            duration = time.monotonic() - started
            with self.lock:
                self.active = max(0, self.active - 1)
                self.last_query_at = datetime.now(timezone.utc).isoformat()
                data = result.get("data", {}) if isinstance(result, dict) else {}
                records = (data.get("point_count") or len(data.get("events", ()))
                           if isinstance(data, dict) else None)
                size = (len(json.dumps(result, ensure_ascii=False,
                            separators=(",", ":")).encode()) if result is not None else 0)
                self.audit.append({"request_id": request_id, "endpoint": endpoint,
                    "duration_seconds": duration, "records": records,
                    "bytes": size, "truncated": (result or {}).get("truncated"),
                    "status": status})
                self.audit = self.audit[-100:]
            if acquired_raw: self.raw.release()
            self.all.release()


class GuardianResearchApi:
    SOURCE_REGISTRY = frozenset({"guardian.cell_history", "guardian.hycube",
        "guardian.display_history", "guardian.canonical_phase"})

    def __init__(self, paths, cursor_secret=None):
        self.paths = paths
        self.identity = ResearchIdentityResolver.from_path(paths.position_history)
        self._position_signature = self._file_signature(paths.position_history)
        self.series = ResearchTimeseriesService(
            paths.cell_history, self.identity, CursorCodec(cursor_secret or secrets.token_bytes(32)))
        self.gate = QueryGate()

    @staticmethod
    def _file_signature(path):
        try:
            stat = Path(path).stat(); return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _refresh_identity(self):
        signature = self._file_signature(self.paths.position_history)
        if signature != self._position_signature:
            self.identity = ResearchIdentityResolver.from_path(self.paths.position_history)
            self.series.identity = self.identity
            self._position_signature = signature

    @staticmethod
    def _event_id(serial, start, end):
        raw = json.dumps({"v": SOC_CRASH_VERSION, "s": serial, "f": start, "t": end},
                         sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()[:16]
        token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"SCE-{token}.{digest}"

    @staticmethod
    def _event_reference(event_id):
        try:
            token, digest = event_id.removeprefix("SCE-").rsplit(".", 1)
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            if hashlib.sha256(raw).hexdigest()[:16] != digest: raise ValueError
            value = json.loads(raw)
            if value["v"] != SOC_CRASH_VERSION: raise ValueError
            return value
        except Exception as exc:
            raise ResearchQueryError("invalid_argument", "event_id is invalid") from exc

    @staticmethod
    def error(code, message):
        return {"research_schema_version": 1, "error": {"code": code, "message": message}}

    def handle(self, method, target):
        if method != "GET":
            return ApiResponse(405, self.error("invalid_argument", "Research API is read-only"),
                               {"Allow": "GET"})
        try:
            split = urlsplit(target); marker = unquote(split.path).find(API_ROUTE)
            if marker < 0: raise ResearchQueryError("invalid_argument", "route not found", 404)
            endpoint = unquote(split.path)[marker + len(API_ROUTE):].strip("/") or "status"
            raw = parse_qs(split.query, keep_blank_values=True)
            if any(len(items) != 1 for items in raw.values()):
                raise ResearchQueryError("invalid_argument", "parameters must occur once")
            values = {key: items[0] for key, items in raw.items()}
            body = self.gate.run(endpoint, values.get("resolution") == "full",
                                 lambda deadline: self._dispatch(endpoint, values, deadline))
            return ApiResponse(200, body)
        except ResearchQueryError as exc:
            return ApiResponse(exc.status, self.error(exc.code, str(exc)))
        except Exception:
            LOG.exception("Research source unavailable")
            return ApiResponse(503, self.error("source_unavailable", "research source unavailable"))

    @staticmethod
    def _required(values, *keys):
        missing = [key for key in keys if not values.get(key)]
        if missing: raise ResearchQueryError("invalid_argument", "missing: " + ", ".join(missing))

    def _range(self, values):
        self._required(values, "from", "to")
        return self.series.normalize_range(values["from"], values["to"])

    def _query_series(self, values, deadline, cells=()):
        self._required(values, "physical_serial", "metric")
        start, end = self._range(values)
        try: max_points = int(values.get("max_points", MAX_POINTS))
        except ValueError as exc:
            raise ResearchQueryError("invalid_argument", "max_points must be an integer") from exc
        result = self.series.query(metric=values["metric"],
            physical_serial=values["physical_serial"], timestamp_from=start, timestamp_to=end,
            resolution=values.get("resolution", "auto"), max_points=max_points,
            cells=cells, cursor=values.get("cursor"), deadline=deadline)
        derived = values["metric"] in {"module_voltage", "module_temperature",
                                       "cell_deviation", "cell_spread"}
        return research_envelope(source="guardian.cell_history",
            evidence_class="DERIVED" if derived else "OBSERVED",
            authoritative=not derived, timestamp_from=start, timestamp_to=end,
            resolution=result["resolution"], data=result, quality=result["coverage"]["quality"],
            truncated=result["truncated"], next_cursor=result["next_cursor"],
            provenance={"physical_serial": values["physical_serial"],
                        "source_fingerprint": result["source_fingerprint"]})

    def _query_hycube(self, values):
        start, end = self._range(values)
        if not self.paths.hycube_history:
            raise ResearchQueryError("source_unavailable", "Hycube source unavailable", 503)
        try: max_points = int(values.get("max_points", 850))
        except ValueError as exc:
            raise ResearchQueryError("invalid_argument", "max_points must be an integer") from exc
        if not 1 <= max_points <= MAX_POINTS:
            raise ResearchQueryError("invalid_argument", "max_points must be 1..6000")
        metric = values.get("metric")
        if metric == "policy":
            segments = HycubePolicyHistory(self.paths.hycube_policy).query(
                timestamp_from=start, timestamp_to=end)
            return research_envelope(source="guardian.hycube", evidence_class="OBSERVED",
                authoritative=True, timestamp_from=start, timestamp_to=end,
                resolution="policy_segments", data={"segments": segments},
                quality="complete" if segments else "absent",
                provenance={"source_fingerprint": self._directory_fingerprint(
                    self.paths.hycube_policy)})
        if metric not in {"battery_capacity", "soc"}:
            raise ResearchQueryError("invalid_argument", "Hycube metric must be battery_capacity")
        result = HycubeBatteryCapacitySeries(self.paths.hycube_history,
            projection_directory=self.paths.hycube_projection).query(
                timestamp_from=start, timestamp_to=end, max_points=max_points)
        status = "complete" if result.get("points") else "absent"
        return research_envelope(source="guardian.hycube", evidence_class="OBSERVED",
            authoritative=True, timestamp_from=start, timestamp_to=end,
            resolution=result.get("source_mode", "auto"), data=result, quality=status,
            provenance={"source_fingerprint": self._directory_fingerprint(
                self.paths.hycube_history)})

    def _query_display(self, values):
        self._required(values, "physical_serial", "metric")
        start, end = self._range(values)
        if not self.paths.display_history:
            raise ResearchQueryError("source_unavailable", "Display History unavailable", 503)
        epochs = self.identity.epochs(values["physical_serial"], start, end)
        positioned = [item for item in epochs if item["position"] is not None]
        if not positioned:
            raise ResearchQueryError("identity_unresolved", "identity has no position in range", 404)
        full = CellHistorySeries(self.paths.cell_history,
                                 position_history_path=self.paths.position_history)
        reader = DisplayHistoryReader(self.paths.display_history, self.paths.cell_history,
            self.paths.hycube_projection or Path("/nonexistent"), full,
            HycubeBatteryCapacitySeries(self.paths.hycube_history or Path("/nonexistent"),
                projection_directory=self.paths.hycube_projection or Path("/nonexistent")))
        metric_map = {"soc": "soc", "module_current": "current",
                      "cell_voltage": "cell_voltage", "cell_temperature": "cell_temperature"}
        if values["metric"] not in metric_map:
            raise ResearchQueryError("invalid_argument", "metric is unavailable in Display History")
        points = []
        for epoch in positioned:
            lower = max(start, epoch["valid_from"])
            upper = min(end, epoch["valid_to"] or end)
            result = reader.query_bundles(requests=({"metric": metric_map[values["metric"]],
                "cell_numbers": tuple(int(item) for item in values.get("cell_numbers", "").split(",")
                                      if item)},), timestamp_from=lower, timestamp_to=upper,
                module_number=epoch["position"], max_points=int(values.get("max_points", MAX_POINTS)))
            for point in result["series"][0]["points"]:
                points.append({**point, "physical_serial": values["physical_serial"],
                    "position_at_time": epoch["position"],
                    "position_history_id": epoch["position_history_id"],
                    "identity_epoch_id": epoch["identity_epoch_id"]})
        points.sort(key=lambda item: (item["timestamp"], item.get("cell_number", 0)))
        return research_envelope(source="guardian.display_history", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="display",
            data={"metric": values["metric"], "physical_serial": values["physical_serial"],
                  "points": points[:MAX_POINTS], "point_count": min(len(points), MAX_POINTS)},
            quality="complete" if points else "absent",
            truncated=len(points) > MAX_POINTS,
            semantics_version="guardian_display_history_v1",
            provenance={"source_fingerprint": self._directory_fingerprint(
                self.paths.display_history)})

    @staticmethod
    def _directory_fingerprint(path):
        if not path or not Path(path).exists(): return None
        values = [(item.name, item.stat().st_size, item.stat().st_mtime_ns)
                  for item in sorted(Path(path).glob("*")) if item.is_file()]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    def _dispatch(self, endpoint, values, deadline):
        self._refresh_identity()
        if endpoint == "status":
            return {"research_schema_version": 1, "enabled": True, "schema_version": 1,
                "read_only": True, "available_sources": sorted(self.SOURCE_REGISTRY),
                "source_health": {"cell_history": self.paths.cell_history.exists(),
                    "position_history": self.paths.position_history.exists(),
                    "maintenance": self.paths.maintenance.exists(),
                    "canonical_phase": self.paths.canonical_phase.exists(),
                    "daily_diagnostics": self.paths.daily_diagnostics.exists(),
                    "hycube": bool(self.paths.hycube_history and self.paths.hycube_history.exists()),
                    "hycube_policy": bool(self.paths.hycube_policy and self.paths.hycube_policy.exists()),
                    "display_history": bool(self.paths.display_history and self.paths.display_history.exists()),
                    "config_history": bool(self.paths.config_history and self.paths.config_history.exists()),
                    "rs485": bool(self.paths.rs485_history and self.paths.rs485_history.exists())},
                "last_query_at": self.gate.last_query_at, "active_queries": self.gate.active,
                "queued_queries": self.gate.queued, "query_failures": self.gate.failures}
        if endpoint == "topology":
            timestamp = values.get("timestamp", datetime.now(timezone.utc).isoformat())
            data = self.identity.topology_at(timestamp)
            return research_envelope(source="guardian.position_history", evidence_class="OBSERVED",
                authoritative=True, timestamp_from=data["timestamp"], timestamp_to=data["timestamp"],
                resolution="snapshot", data=data,
                quality="complete" if data["position_history_id"] else "unknown")
        if endpoint == "identity-epochs":
            data = self.identity.epochs(values.get("physical_serial"), values.get("from"), values.get("to"))
            return research_envelope(source="guardian.position_history", evidence_class="DERIVED",
                authoritative=False, timestamp_from=values.get("from"), timestamp_to=values.get("to"),
                resolution="epochs", data={"epochs": data}, quality="complete" if data else "absent",
                semantics_version="research_identity_epochs_v1")
        if endpoint in {"timeseries", "module-history"}:
            source = values.get("source", "guardian.cell_history")
            if source not in self.SOURCE_REGISTRY:
                raise ResearchQueryError("invalid_argument", "source is not registered")
            if source == "guardian.hycube": return self._query_hycube(values)
            if source == "guardian.display_history": return self._query_display(values)
            if source == "guardian.canonical_phase":
                return self._phases(values)
            return self._query_series(values, deadline)
        if endpoint == "cell-history":
            try: cells = tuple(int(item) for item in values.get("cell_numbers", "").split(",") if item)
            except ValueError as exc:
                raise ResearchQueryError("invalid_argument", "cell_numbers must be integers") from exc
            if len(cells) > MAX_CELLS: raise ResearchQueryError("invalid_argument", "too many cells")
            return self._query_series(values, deadline, cells)
        if endpoint == "coverage": return self._coverage(values, deadline)
        if endpoint == "maintenance": return self._maintenance(values)
        if endpoint == "phases": return self._phases(values)
        if endpoint in {"daily-diagnostics", "diagnostic-evidence"}: return self._daily(values)
        if endpoint == "events/soc-crashes": return self._soc_crashes(values, deadline)
        if endpoint == "events/low-voltage": return self._low_voltage(values, deadline)
        if endpoint == "alarms": return self._alarms(values)
        if endpoint == "evidence-package": return self._package(values, deadline)
        raise ResearchQueryError("invalid_argument", "research endpoint not found", 404)

    def _coverage(self, values, deadline):
        self._required(values, "physical_serial"); start, end = self._range(values)
        mapping = {"soc": "soc", "module_voltage": "module_voltage",
            "module_current": "module_current", "cell_voltage": "cell_voltage",
                      "cell_temperature": "cell_temperature"}
        allowed = set(mapping) | {"hycube", "policy", "maintenance", "rs485",
            "soh", "cycles", "ah_coulomb", "daily_diagnostics"}
        rows = []
        for dataset in filter(None, values.get("datasets", "soc").split(",")):
            if dataset not in allowed:
                raise ResearchQueryError("invalid_argument", "coverage dataset is unsupported")
            if dataset in mapping:
                result = self.series.query(metric=mapping[dataset], physical_serial=values["physical_serial"],
                    timestamp_from=start, timestamp_to=end, resolution="display", max_points=10,
                    deadline=deadline)
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                             **result["coverage"]})
            elif dataset in {"hycube", "policy"} and self.paths.hycube_history:
                response = self._query_hycube({"metric": "policy" if dataset == "policy"
                    else "battery_capacity", "from": start, "to": end, "max_points": "6000"})
                timestamps = ([point["timestamp"] for point in response["data"].get("points", ())]
                    if dataset == "hycube" else
                    [segment["from"] for segment in response["data"].get("segments", ())])
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                    **self._coverage_row(start, end, timestamps)})
            elif dataset == "maintenance":
                response = self._maintenance({"physical_serial": values["physical_serial"],
                    "from": start, "to": end, "max_records": "10000"})
                timestamps = [event["occurred_at"] for event in response["data"]["events"]]
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                    **self._coverage_row(start, end, timestamps)})
            else:
                known_path = {"hycube": self.paths.hycube_history,
                    "policy": self.paths.hycube_policy, "maintenance": self.paths.maintenance,
                    "rs485": self.paths.rs485_history,
                    "daily_diagnostics": self.paths.daily_diagnostics}.get(dataset)
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                    "requested_range": {"from": start, "to": end}, "covered_intervals": [],
                    "missing_intervals": [], "first_observation": None, "last_observation": None,
                    "sample_count": None, "expected_cadence_seconds": None,
                    "largest_gap_seconds": None, "quality": (
                        "unknown" if known_path is None or Path(known_path).exists() else "absent")})
        overall = "complete" if rows and all(item["quality"] == "complete" for item in rows) else "partial"
        return research_envelope(source="guardian.coverage", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="dataset",
            data={"datasets": rows}, quality=overall)

    @staticmethod
    def _coverage_row(start, end, timestamps):
        timestamps = sorted(timestamps)
        epochs = [datetime.fromisoformat(item.replace("Z", "+00:00")).timestamp()
                  for item in timestamps]
        gaps = [right - left for left, right in zip(epochs, epochs[1:])]
        return {"requested_range": {"from": start, "to": end},
            "covered_intervals": ([{"from": timestamps[0], "to": timestamps[-1]}]
                                  if timestamps else []),
            "missing_intervals": ([] if timestamps else [{"from": start, "to": end}]),
            "first_observation": timestamps[0] if timestamps else None,
            "last_observation": timestamps[-1] if timestamps else None,
            "sample_count": len(timestamps), "expected_cadence_seconds": None,
            "largest_gap_seconds": max(gaps) if gaps else None,
            "quality": "complete" if timestamps else "absent"}

    @classmethod
    def _evidence_coverage(cls, start, end, timestamps, *, truncated=False):
        row = cls._coverage_row(start, end, timestamps)
        if timestamps:
            row["quality"] = ("complete" if not truncated and timestamps[0] <= start
                              and timestamps[-1] >= end else "partial")
            if row["quality"] == "partial":
                row["missing_intervals"] = []
                if timestamps[0] > start:
                    row["missing_intervals"].append({"from": start, "to": timestamps[0]})
                if timestamps[-1] < end:
                    row["missing_intervals"].append({"from": timestamps[-1], "to": end})
        else:
            row["quality"] = "unavailable"
        return row

    def _maintenance(self, values):
        start, end = self._range(values)
        events = MaintenanceRepository(MaintenanceEventLog(self.paths.maintenance)).list(include_archived=True)
        rows = []
        for event in events:
            if not start <= event.occurred_at <= end: continue
            serial = event.module_serial
            if not serial and event.module_number:
                serial = self.identity.serial_at(event.module_number, event.occurred_at)["physical_serial"]
            if values.get("physical_serial") and serial != values["physical_serial"]: continue
            if values.get("category") and event.category != values["category"]: continue
            if values.get("action") and event.action_taken != values["action"]: continue
            identity = self.identity.position_at(serial, event.occurred_at) if serial else {"resolved": False}
            occurred = datetime.fromisoformat(event.occurred_at)
            before = self.identity.position_at(serial, (occurred - timedelta(
                microseconds=1)).isoformat()) if serial else {"position_at_time": None}
            after = self.identity.position_at(serial, (occurred + timedelta(
                microseconds=1)).isoformat()) if serial else {"position_at_time": None}
            rows.append({**event.to_dict(), "physical_serial": serial,
                "position_at_event": identity.get("position_at_time"),
                "position_before_event": before.get("position_at_time"),
                "position_after_event": after.get("position_at_time"),
                "position_history_id": identity.get("position_history_id"),
                "identity_epoch_id": identity.get("identity_epoch_id")})
        try: limit = int(values.get("max_records", 1000))
        except ValueError as exc:
            raise ResearchQueryError("invalid_argument", "max_records must be an integer") from exc
        if not 1 <= limit <= 10_000:
            raise ResearchQueryError("invalid_argument", "max_records must be 1..10000")
        query_hash = hashlib.sha256(json.dumps((start, end, values.get("physical_serial"),
            values.get("category"), values.get("action"), limit), separators=(",", ":")).encode()).hexdigest()
        offset = self.series.cursor.decode(values["cursor"], query_hash) if values.get("cursor") else 0
        page = rows[offset:offset + limit]; truncated = offset + len(page) < len(rows)
        next_cursor = self.series.cursor.encode(query_hash, offset + len(page)) if truncated else None
        return research_envelope(source="guardian.maintenance", evidence_class="OBSERVED",
            authoritative=True, timestamp_from=start, timestamp_to=end, resolution="events",
            data={"events": page}, quality="complete", truncated=truncated,
            next_cursor=next_cursor)

    def _phases(self, values):
        self._required(values, "physical_serial"); start, end = self._range(values)
        result = CanonicalPhaseReader(self.paths.canonical_phase).query(
            start, end, physical_serial=values["physical_serial"])
        return research_envelope(source="guardian.canonical_phase", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="intervals",
            data=result, quality="complete" if result.get("available") else "absent",
            semantics_version=result.get("semantics_version", "guardian_canonical_phase_v2"),
            provenance={"physical_serial": values["physical_serial"]})

    def _daily(self, values):
        repository = GuardianDiagnosticsRepository(self.paths.daily_diagnostics)
        if values.get("date"):
            start = end = values["date"]; data = repository.day_dto(values["date"])
        else:
            start, end = self._range(values); data = repository.days_dto()
            data["days"] = [row for row in data["days"] if start[:10] <= row["date"] <= end[:10]]
        serial = values.get("physical_serial")
        if serial and isinstance(data, dict):
            management = data.get("bms_management")
            if isinstance(management, dict):
                management["aggregates"] = [row for row in management.get("aggregates", ())
                                            if row.get("physical_serial") == serial]
            risk = data.get("cell_risk")
            if isinstance(risk, dict):
                for key in ("cells", "top10"):
                    if isinstance(risk.get(key), list):
                        risk[key] = [row for row in risk[key]
                                     if row.get("physical_serial") == serial]
        component = values.get("component")
        if component and isinstance(data, dict) and isinstance(data.get("components"), dict):
            data["components"] = ({component: data["components"][component]}
                                  if component in data["components"] else {})
        return research_envelope(source="guardian.daily_diagnostics", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="daily",
            data=data, quality="complete")

    @staticmethod
    def _soc_profile():
        return {"profile_schema_version": 1, "endpoint": "events/soc-crashes",
            "status": "running", "stages_seconds": {
                "request_range_validation": 0.0, "identity_epoch_preparation": 0.0,
                "file_discovery": 0.0, "block_index_discovery": 0.0,
                "indexed_range_selection": 0.0,
                "jsonl_scan": 0.0, "range_seek": 0.0,
                "range_position_check": 0.0, "raw_line_read": 0.0,
                "raw_chunk_read": 0.0, "range_tail_read": 0.0,
                "serial_prefilter": 0.0, "serial_token_decode": 0.0,
                "full_json_decode": 0.0, "timestamp_parse_range_check": 0.0,
                "timestamp_format": 0.0, "identity_assignment": 0.0,
                "soc_current_extract": 0.0, "cell_context_extract": 0.0,
                "deadline_check": 0.0,
                "candidate_detection": 0.0, "grouping": 0.0,
                "historical_position_resolution": 0.0},
            "counts": {"requested_serials": 0, "files_discovered": 0,
                "raw_records_inspected": 0, "records_skipped_serial_prefilter": 0,
                "records_skipped_timestamp": 0, "records_fully_decoded": 0,
                "relevant_soc_current_samples": 0, "candidates": 0,
                "groups": 0, "events": 0, "raw_bytes_read": 0,
                "record_bytes_inspected": 0,
                "average_raw_line_bytes": 0.0, "maximum_raw_line_bytes": 0,
                "raw_chunk_reads": 0, "range_tail_reads": 0},
            "files": []}

    def _soc_crashes(self, values, deadline):
        profiling = values.get("profile")
        if profiling not in (None, "false", "true"):
            raise ResearchQueryError("invalid_argument", "profile must be true or false")
        profile = self._soc_profile() if profiling == "true" else None
        started = time.perf_counter()
        try:
            result = self._soc_crashes_impl(values, deadline, profile)
            if profile is not None: profile["status"] = "ok"
            return result
        except ResearchQueryError as exc:
            if profile is not None: profile["status"] = exc.code
            raise
        except Exception:
            if profile is not None: profile["status"] = "source_unavailable"
            raise
        finally:
            if profile is not None:
                profile["total_elapsed_seconds"] = time.perf_counter() - started
                LOG.info("RESEARCH_PROFILE %s", json.dumps(
                    profile, ensure_ascii=True, sort_keys=True, separators=(",", ":")))

    def _soc_crashes_impl(self, values, deadline, profile=None):
        started = time.perf_counter(); start, end = self._range(values)
        if profile is not None:
            profile["stages_seconds"]["request_range_validation"] = (
                time.perf_counter() - started)
        serials = ([values["physical_serial"]] if values.get("physical_serial") else
                   sorted({item["physical_serial"] for item in self.identity.epochs()}))[:MAX_SERIALS]
        if profile is not None: profile["counts"]["requested_serials"] = len(serials)
        events = []
        observations = self.series.soc_current_by_serial(
            serials, start, end, deadline, profile=profile)
        for serial in serials:
            rows = observations.get(serial, ()); candidates = []
            started = time.perf_counter()
            for before, after in zip(rows, rows[1:]):
                gap = datetime.fromisoformat(after["timestamp"]).timestamp() - datetime.fromisoformat(before["timestamp"]).timestamp()
                loss, value = before["soc"] - after["soc"], after["current"]
                if 0 <= gap <= 300 and loss >= 2 and value < -0.2:
                    candidates.append({"start": before["timestamp"], "end": after["timestamp"],
                        "soc_before": before["soc"], "soc_after": after["soc"],
                        "current": value, "identity_epoch_id": after["identity_epoch_id"],
                        "lowest_cell": after.get("lowest_cell"),
                        "cell_spread_mv": after.get("cell_spread_mv")})
            if profile is not None:
                profile["stages_seconds"]["candidate_detection"] += (
                    time.perf_counter() - started)
                profile["counts"]["candidates"] += len(candidates)
            groups = []
            started = time.perf_counter()
            for item in candidates:
                gap = ((datetime.fromisoformat(item["start"]) -
                        datetime.fromisoformat(groups[-1][-1]["end"])).total_seconds()
                       if groups else None)
                if groups and gap <= 600 and item["identity_epoch_id"] == groups[-1][-1]["identity_epoch_id"]:
                    groups[-1].append(item)
                else: groups.append([item])
            if profile is not None:
                profile["stages_seconds"]["grouping"] += time.perf_counter() - started
                profile["counts"]["groups"] += len(groups)
            for group in groups:
                started = time.perf_counter()
                position = self.identity.position_at(serial, group[0]["start"])
                if profile is not None:
                    profile["stages_seconds"]["historical_position_resolution"] += (
                        time.perf_counter() - started)
                events.append({"event_id": self._event_id(
                        serial, group[0]["start"], group[-1]["end"]),
                    "detector_version": SOC_CRASH_VERSION, "physical_serial": serial,
                    "position_at_event": position["position_at_time"],
                    "identity_epoch_id": group[0]["identity_epoch_id"],
                    "start": group[0]["start"], "end": group[-1]["end"],
                    "soc_before": group[0]["soc_before"], "soc_after": group[-1]["soc_after"],
                    "soc_loss": group[0]["soc_before"] - group[-1]["soc_after"],
                    "step_count": len(group), "module_current_context": [item["current"] for item in group],
                    "lowest_cell": group[-1].get("lowest_cell"),
                    "cell_spread_mv": group[-1].get("cell_spread_mv"),
                    "coverage": "complete", "source_references": ["guardian.cell_history"]})
        if profile is not None: profile["counts"]["events"] = len(events)
        return research_envelope(source="guardian.cell_history", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="events",
            data={"events": events, "detector_version": SOC_CRASH_VERSION,
                "thresholds": {"soc_loss_pp": 2, "sample_gap_seconds": 300,
                    "discharge_current_below_a": -0.2, "merge_gap_seconds": 600}},
            semantics_version=SOC_CRASH_VERSION)

    def _low_voltage(self, values, deadline, io_profile=None):
        self._required(values, "physical_serial"); start, end = self._range(values)
        result = self.series.query(metric="cell_voltage", physical_serial=values["physical_serial"],
            timestamp_from=start, timestamp_to=end, resolution=values.get("resolution", "auto"),
            max_points=int(values.get("max_points", MAX_POINTS)), deadline=deadline,
            io_profile=io_profile)
        alarms = self._alarms({"physical_serial": values["physical_serial"],
                               "from": start, "to": end})
        rs485 = self._rs485_low_voltage(values["physical_serial"], start, end, deadline)
        return research_envelope(source="guardian.cell_history", evidence_class="OBSERVED",
            authoritative=True, timestamp_from=start, timestamp_to=end, resolution=result["resolution"],
            data={"evidence": [
                    {"evidence_kind": "cell_voltage", "points": result["points"],
                     "coverage": result["coverage"]},
                    {"evidence_kind": "guardian_alarm", **alarms["data"]},
                    {"evidence_kind": "rs485_low_voltage", **rs485}],
                  "alarm_asserted": None, "points": result["points"],
                  "coverage": result["coverage"]},
            quality=result["coverage"]["quality"])

    def _rs485_low_voltage(self, serial, start, end, deadline):
        if not self.paths.rs485_history or not Path(self.paths.rs485_history).exists():
            return {"quality": "absent", "records": []}
        identities, records = {}, []
        first, last = start[:10], end[:10]
        for path in sorted(Path(self.paths.rs485_history).glob("*.jsonl")):
            if not first <= path.stem <= last: continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if time.monotonic() > deadline:
                        raise ResearchQueryError("timeout", "research query timed out", 503)
                    try: record = json.loads(line)
                    except json.JSONDecodeError: continue
                    timestamp = record.get("timestamp")
                    if not timestamp or not start <= timestamp <= end: continue
                    identity = decode_identity_record(record)
                    if identity:
                        identities[int(record.get("adr", -1))] = identity["serial_string"]
                        continue
                    if identities.get(int(record.get("adr", -1))) != serial: continue
                    decoded = record.get("decoded") or {}
                    low = {key: value for key, value in decoded.items()
                           if "low_voltage" in key or "under_voltage" in key}
                    if low or record.get("paired_command") == 0x44:
                        records.append({"timestamp": timestamp, "physical_serial": serial,
                            "adr": record.get("adr"), "paired_command": record.get("paired_command"),
                            "checksum_valid": record.get("checksum_valid"),
                            "request_matched": record.get("request_matched"), "decoded": low or None})
                    if len(records) >= 10_000:
                        return {"quality": "partial", "records": records, "truncated": True}
        return {"quality": "complete" if records else "absent", "records": records,
                "truncated": False}

    def _rs485_management(self, serial, start, end, deadline):
        """Return bounded observed 0x92/0x44 evidence for a serial, without interpretation."""
        requested = {"from": start, "to": end}
        if not self.paths.rs485_history or not Path(self.paths.rs485_history).exists():
            return {"evidence_class": "OBSERVED", "quality": "unavailable",
                    "records": [], "coverage": {"requested_range": requested,
                    "covered_intervals": [], "missing_intervals": [requested],
                    "first_observation": None, "last_observation": None,
                    "sample_count": 0, "expected_cadence_seconds": None,
                    "largest_gap_seconds": None, "quality": "unavailable"}}
        identities, records = {}, []
        first, last = start[:10], end[:10]
        for path in sorted(Path(self.paths.rs485_history).glob("*.jsonl")):
            if not first <= path.stem <= last:
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if time.monotonic() > deadline:
                        raise ResearchQueryError("timeout", "research query timed out", 503)
                    try: record = json.loads(line)
                    except json.JSONDecodeError: continue
                    identity = decode_identity_record(record)
                    if identity:
                        identities[int(record.get("adr", -1))] = identity["serial_string"]
                        continue
                    timestamp = record.get("timestamp")
                    if isinstance(timestamp, (int, float)):
                        timestamp = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
                    if not isinstance(timestamp, str) or not start <= timestamp <= end:
                        continue
                    adr = int(record.get("adr", -1))
                    if identities.get(adr) != serial:
                        continue
                    command = record.get("paired_command")
                    if command not in {0x92, 0x44}:
                        continue
                    if not (record.get("direction") == "response"
                            and record.get("checksum_valid") is True
                            and record.get("frame_complete") is True
                            and record.get("request_matched") is True):
                        continue
                    decoded = record.get("decoded") if isinstance(record.get("decoded"), dict) else {}
                    fields = ({key: decoded.get(key) for key in (
                        "charge_current_limit_a", "discharge_current_limit_a",
                        "charge_voltage_limit_v", "discharge_voltage_limit_v",
                        "charge_enable", "discharge_enable")}
                        if command == 0x92 else {"command": "0x44", "decoded": decoded or None})
                    records.append({"timestamp": timestamp, "physical_serial": serial,
                        "adr": adr, "paired_command": command, **fields})
                    if len(records) >= 10_000:
                        coverage = self._evidence_coverage(start, end,
                            [item["timestamp"] for item in records])
                        coverage["quality"] = "partial"
                        return {"evidence_class": "OBSERVED", "quality": "partial",
                                "records": records, "coverage": coverage, "truncated": True}
        coverage = self._evidence_coverage(start, end,
                                           [item["timestamp"] for item in records])
        return {"evidence_class": "OBSERVED",
                "quality": "complete" if records else "unavailable",
                "records": records, "coverage": coverage, "truncated": False}

    def _config_context(self, timestamp):
        if not self.paths.config_history or not Path(self.paths.config_history).is_file():
            return {"quality": "absent", "record": None}
        selected = None
        try:
            with Path(self.paths.config_history).open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip(): continue
                    record = json.loads(line)
                    effective = record.get("effective_at") or record.get("created_at")
                    if effective and effective <= timestamp: selected = record
        except (OSError, json.JSONDecodeError):
            return {"quality": "unknown", "record": None}
        return {"quality": "complete" if selected else "absent", "record": selected}

    def _alarms(self, values):
        start, end = self._range(values)
        rows = []
        for event in TechnicalEventSource(self.paths.technical_events).read() if self.paths.technical_events else ():
            if not start <= event.timestamp <= end or not event.event_type.startswith("alarm_"): continue
            identity = (self.identity.serial_at(event.module_number, event.timestamp)
                        if event.module_number else {"resolved": False, "physical_serial": None})
            if values.get("physical_serial") and identity.get("physical_serial") != values["physical_serial"]:
                continue
            if values.get("severity") and event.severity != values["severity"]: continue
            if values.get("type") and event.event_type != values["type"]: continue
            rows.append({**event.to_dict(), "physical_serial": identity.get("physical_serial"),
                "position_at_time": event.module_number,
                "position_history_id": identity.get("position_history_id"),
                "identity_epoch_id": identity.get("identity_epoch_id"),
                "identity_resolved": identity.get("resolved", False)})
        return research_envelope(source="guardian.events", evidence_class="OBSERVED",
            authoritative=True, timestamp_from=start, timestamp_to=end, resolution="events",
            data={"alarms": rows}, quality="complete" if self.paths.technical_events else "unknown")

    @staticmethod
    def _package_profile():
        return {"profile_schema_version": 1, "endpoint": "evidence-package",
            "status": "running", "total_elapsed_seconds": 0.0,
            "stages": {name: {"elapsed_seconds": 0.0, "calls": 0,
                "status": "not_run", "deadline_remaining_seconds_at_entry": None,
                "deadline_remaining_seconds_at_exit": None,
                "files_discovered": 0, "files_opened": 0, "bytes_read": 0,
                "records_inspected": 0, "samples_returned": 0,
                "index_present": None, "index_valid": None,
                "read_mode": "not_observed"} for name in PACKAGE_PROFILE_STAGES},
            "counts": {"cell_history_queries": 0, "cell_history_scans": 0,
                "peer_modules": 0, "peer_history_queries": 0,
                "comparison_windows": 0},
            "coverage_status": {}}

    @staticmethod
    def _run_package_stage(profile, name, deadline, callback, *, io_profile=None):
        if profile is None:
            return callback()
        stage = profile["stages"][name]
        stage["calls"] += 1
        stage["status"] = "running"
        stage["deadline_remaining_seconds_at_entry"] = max(
            0.0, deadline - time.monotonic())
        started = time.perf_counter()
        try:
            result = callback()
            stage["status"] = "complete"
            return result
        except ResearchQueryError as exc:
            stage["status"] = exc.code
            raise
        except Exception:
            stage["status"] = "source_unavailable"
            raise
        finally:
            stage["elapsed_seconds"] += time.perf_counter() - started
            stage["deadline_remaining_seconds_at_exit"] = max(
                0.0, deadline - time.monotonic())
            if io_profile:
                for key in ("files_discovered", "files_opened", "bytes_read",
                            "records_inspected", "samples_returned"):
                    stage[key] += io_profile.get(key, 0)
                stage["index_present"] = io_profile.get("index_present")
                stage["index_valid"] = io_profile.get("index_valid")
                stage["read_mode"] = io_profile.get("read_mode", "not_observed")

    def _package(self, values, deadline):
        profiling = values.get("profile")
        if profiling not in (None, "false", "true"):
            raise ResearchQueryError("invalid_argument", "profile must be true or false")
        profile = self._package_profile() if profiling == "true" else None
        started = time.perf_counter()
        try:
            result = self._package_impl(values, deadline, profile)
            if profile is not None:
                profile["status"] = "ok"
            return result
        except ResearchQueryError as exc:
            if profile is not None:
                profile["status"] = exc.code
            raise
        except Exception:
            if profile is not None:
                profile["status"] = "source_unavailable"
            raise
        finally:
            if profile is not None:
                profile["total_elapsed_seconds"] = time.perf_counter() - started
                LOG.info("RESEARCH_PACKAGE_PROFILE %s", json.dumps(
                    profile, ensure_ascii=True, sort_keys=True, separators=(",", ":")))

    def _package_impl(self, values, deadline, profile=None):
        self._required(values, "event_id")
        reference = self._run_package_stage(profile, "event_id_decode_checksum", deadline,
            lambda: self._event_reference(values["event_id"]))
        # Event timestamps originate as floating-point epochs but are represented
        # in the stable identifier as microsecond-resolution ISO strings.  Widen
        # only the reconstruction boundaries enough to include either side of
        # that lossless-for-datetime, but potentially rounded, conversion.
        try:
            lookup_start = (datetime.fromisoformat(reference["f"])
                            - timedelta(microseconds=1)).isoformat()
            lookup_end = (datetime.fromisoformat(reference["t"])
                          + timedelta(microseconds=1)).isoformat()
        except (TypeError, ValueError) as exc:
            raise ResearchQueryError("invalid_argument", "event_id is invalid") from exc
        detector_values = {"physical_serial": reference["s"], "from": lookup_start,
                           "to": lookup_end}
        crashes = self._run_package_stage(profile, "bounded_event_reconstruction", deadline,
            lambda: self._soc_crashes(detector_values, deadline))
        event = self._run_package_stage(profile, "event_match", deadline,
            lambda: next((item for item in crashes["data"]["events"]
                          if item["event_id"] == values["event_id"]), None))
        if event is None: raise ResearchQueryError("coverage_absent", "event is unavailable", 404)
        def duration(value, default):
            raw = value or default
            match = re.fullmatch(
                r"P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?)?",
                raw)
            if not match or not any(match.groups()):
                raise ResearchQueryError("invalid_argument", "duration must be a positive ISO-8601 duration")
            seconds = (float(match.group(1) or 0) * 86400
                       + float(match.group(2) or 0) * 3600
                       + float(match.group(3) or 0) * 60)
            if not 0 < seconds <= 90 * 86400:
                raise ResearchQueryError("invalid_argument", "duration must be greater than zero and at most P90D")
            return seconds
        serial = event["physical_serial"]
        def evidence_window():
            return ((datetime.fromisoformat(event["start"]) -
                    timedelta(seconds=duration(values.get("before"), "P1D"))).isoformat(),
                    (datetime.fromisoformat(event["end"]) +
                    timedelta(seconds=duration(values.get("after"), "PT30M"))).isoformat())
        start, end = self._run_package_stage(
            profile, "evidence_window_calculation", deadline, evidence_window)
        series = {}
        metric_stages = {"soc": "module_soc", "module_current": "module_current",
            "module_voltage": "module_voltage", "cell_voltage": "cell_voltages",
            "cell_temperature": "temperature_channels"}
        for metric, stage_name in metric_stages.items():
            io_profile = {} if profile is not None else None
            if profile is not None:
                profile["counts"]["cell_history_queries"] += 1
                profile["counts"]["cell_history_scans"] += 1
            result = self._run_package_stage(profile, stage_name, deadline,
                lambda metric=metric, io_profile=io_profile: self.series.query(
                    metric=metric, physical_serial=serial, timestamp_from=start,
                    timestamp_to=end, resolution="auto", max_points=800,
                    deadline=deadline, io_profile=io_profile), io_profile=io_profile)
            series[metric] = {**result, "evidence_class": (
                "DERIVED" if metric == "module_voltage" else "OBSERVED")}
            if profile is not None:
                profile["coverage_status"][stage_name] = result["coverage"]["quality"]
        trend_windows = [item for item in values.get("trend_windows", "PT6H,P1D,P7D").split(",")
                         if item]
        if len(trend_windows) > 3:
            raise ResearchQueryError("invalid_argument", "at most three trend windows are allowed")
        trends = {}
        event_end = datetime.fromisoformat(event["end"])
        for window in trend_windows:
            window_start = (event_end - timedelta(seconds=duration(window, window))).isoformat()
            io_profile = {} if profile is not None else None
            if profile is not None:
                profile["counts"]["cell_history_queries"] += 1
                profile["counts"]["cell_history_scans"] += 1
            trends[window] = self._run_package_stage(profile, "module_soc", deadline,
                lambda window_start=window_start, io_profile=io_profile: self.series.query(
                    metric="soc", physical_serial=serial, timestamp_from=window_start,
                    timestamp_to=event["end"], resolution="auto", max_points=200,
                    deadline=deadline, io_profile=io_profile), io_profile=io_profile)
        def isolated(name, callback):
            try: return callback()
            except Exception as exc:
                LOG.warning("Research package source unavailable source=%s error=%s",
                            name, type(exc).__name__)
                return {"quality": "unknown", "error": "source_unavailable"}
        hycube = self._run_package_stage(profile, "hycube", deadline,
            lambda: isolated("hycube", lambda: self._query_hycube({
                "metric": "battery_capacity", "from": start, "to": end,
                "max_points": "200"})["data"]))
        policy = self._run_package_stage(profile, "policy", deadline,
            lambda: isolated("policy", lambda: self._query_hycube({
                "metric": "policy", "from": start, "to": end})["data"]))
        daily = self._run_package_stage(profile, "daily_diagnostics", deadline,
            lambda: isolated("daily_diagnostics", lambda: self._daily({
                "date": event["start"][:10], "physical_serial": serial})["data"]))
        if profile is not None:
            profile["coverage_status"].update({
                "hycube": ("unavailable" if hycube.get("quality") == "unknown" else
                           "complete" if hycube.get("points") else "unavailable"),
                "policy": ("unavailable" if policy.get("quality") == "unknown" else
                           "complete" if policy.get("segments") else "unavailable"),
                "daily_diagnostics": ("unavailable" if daily.get("quality") == "unknown"
                                      else "complete")})
        identity = self._run_package_stage(profile, "historical_position_resolution", deadline,
            lambda: self.identity.position_at(serial, event["start"]))
        self._run_package_stage(profile, "identity_epoch_resolution", deadline,
            lambda: identity.get("identity_epoch_id"))
        stack = self._run_package_stage(profile, "peer_topology_resolution", deadline,
            lambda: self.identity.topology_at(event["start"]))
        peer_serials = [row["physical_serial"] for row in stack["positions"]
                        if row["physical_serial"] and row["physical_serial"] != serial]
        if profile is not None:
            profile["counts"]["peer_modules"] = len(peer_serials)
            profile["counts"]["peer_history_queries"] = 1 if peer_serials else 0
            profile["counts"]["cell_history_queries"] += 1
            profile["counts"]["cell_history_scans"] += 1
        peer_io = {} if profile is not None else None
        cell_evidence = self._run_package_stage(profile, "peer_module_evidence", deadline,
            lambda: self.series.evidence_by_serial(
                [serial, *peer_serials], start, end, deadline=deadline,
                io_profile=peer_io), io_profile=peer_io)
        if profile is not None:
            profile["coverage_status"]["peer_module_evidence"] = (
                "partial" if cell_evidence.get("truncated") else
                "complete" if any(cell_evidence["records"].values()) else "unavailable")
        target_records_all = cell_evidence["records"].get(serial, [])
        crash_start = datetime.fromisoformat(event["start"])
        crash_end = datetime.fromisoformat(event["end"])
        window_specs = (("minus_24h", crash_start - timedelta(hours=24), crash_start),
            ("minus_6h", crash_start - timedelta(hours=6), crash_start),
            ("minus_1h", crash_start - timedelta(hours=1), crash_start),
            ("minus_10m", crash_start - timedelta(minutes=10), crash_start),
            ("immediate_pre_crash", crash_start - timedelta(minutes=5), crash_start),
            ("crash", crash_start, crash_end),
            ("plus_10m", crash_end, crash_end + timedelta(minutes=10)),
            ("plus_30m", crash_end, crash_end + timedelta(minutes=30)))
        def bounded(rows, limit=600):
            if len(rows) <= limit: return rows
            step = (len(rows) - 1) / (limit - 1)
            return [rows[round(index * step)] for index in range(limit)]
        target_records = bounded(target_records_all)
        comparison_windows = {}
        comparison_started = time.perf_counter()
        for name, left, right in window_specs:
            selected = [row for row in target_records_all
                        if left <= datetime.fromisoformat(row["timestamp"]) <= right]
            soc = [float(row["soc"]) for row in selected if row.get("soc") is not None]
            current = [float(row["module_current_a"]) for row in selected
                       if row.get("module_current_a") is not None]
            comparison_windows[name] = {"from": left.isoformat(), "to": right.isoformat(),
                "sample_count": len(selected), "first_timestamp": selected[0]["timestamp"] if selected else None,
                "last_timestamp": selected[-1]["timestamp"] if selected else None,
                "soc_min": min(soc) if soc else None, "soc_max": max(soc) if soc else None,
                "current_min_a": min(current) if current else None,
                "current_max_a": max(current) if current else None,
                "records": selected if name in {"immediate_pre_crash", "crash"} else [],
                "records_resolution": "full" if name in {"immediate_pre_crash", "crash"} else "summary",
                "quality": "complete" if selected else "unavailable"}
        if profile is not None:
            comparison_stage = profile["stages"]["comparison_window_construction"]
            comparison_stage.update({"elapsed_seconds": time.perf_counter() - comparison_started,
                "calls": 1, "status": "complete", "samples_returned": sum(
                    item["sample_count"] for item in comparison_windows.values()),
                "deadline_remaining_seconds_at_entry": None,
                "deadline_remaining_seconds_at_exit": max(0.0, deadline - time.monotonic())})
            profile["counts"]["comparison_windows"] = len(comparison_windows)
        immediate_left, immediate_right = crash_start - timedelta(minutes=5), crash_end + timedelta(minutes=5)
        peers = []
        for peer_serial in peer_serials:
            rows = [row for row in cell_evidence["records"].get(peer_serial, [])
                    if immediate_left <= datetime.fromisoformat(row["timestamp"]) <= immediate_right]
            if rows:
                peers.append({"physical_serial": peer_serial,
                    "position_at_event": self.identity.position_at(peer_serial, event["start"])["position_at_time"],
                    "evidence_class": "OBSERVED", "records": rows,
                    "coverage": self._evidence_coverage(immediate_left.isoformat(),
                                                   immediate_right.isoformat(),
                                                   [row["timestamp"] for row in rows])})
        management = self._run_package_stage(profile, "bms_management", deadline,
            lambda: isolated("rs485_management", lambda: self._rs485_management(
                serial, start, end, deadline)))
        if profile is not None:
            profile["stages"]["bms_management"]["records_inspected"] = len(
                management.get("records", ()))
            profile["coverage_status"]["bms_management"] = management.get(
                "quality", "unknown")
            for name, field in (("ccl", "charge_current_limit_a"),
                                ("dcl", "discharge_current_limit_a"),
                                ("charge_enable", "charge_enable"),
                                ("discharge_enable", "discharge_enable")):
                count = sum(row.get(field) is not None for row in management.get("records", ()))
                profile["stages"][name].update({"calls": 1, "status": (
                    "complete" if count else "unavailable"), "records_inspected": count,
                    "samples_returned": count, "deadline_remaining_seconds_at_entry": max(
                        0.0, deadline - time.monotonic()),
                    "deadline_remaining_seconds_at_exit": max(
                        0.0, deadline - time.monotonic())})
            command_count = sum(row.get("paired_command") == 0x44
                                for row in management.get("records", ()))
            profile["stages"]["command_0x44"].update({"calls": 1,
                "status": "complete" if command_count else "unavailable",
                "records_inspected": command_count, "samples_returned": command_count,
                "deadline_remaining_seconds_at_entry": max(
                    0.0, deadline - time.monotonic()),
                "deadline_remaining_seconds_at_exit": max(
                    0.0, deadline - time.monotonic())})
        maintenance = self._run_package_stage(profile, "maintenance", deadline,
            lambda: self._maintenance({"physical_serial": serial, "from": start,
                                       "to": end})["data"])
        phase = self._run_package_stage(profile, "canonical_phase", deadline,
            lambda: self._phases({"physical_serial": serial, "from": start,
                                  "to": end})["data"])
        alarms = self._run_package_stage(profile, "alarms", deadline,
            lambda: self._alarms({"physical_serial": serial, "from": start,
                                  "to": end})["data"])
        low_voltage_io = {} if profile is not None else None
        if profile is not None:
            profile["counts"]["cell_history_queries"] += 1
            profile["counts"]["cell_history_scans"] += 1
        low_voltage = self._run_package_stage(profile, "low_voltage", deadline,
            lambda: self._low_voltage({"physical_serial": serial, "from": start,
                "to": end, "max_points": "200"}, deadline,
                io_profile=low_voltage_io)["data"], io_profile=low_voltage_io)
        coverage_started = time.perf_counter()
        coverage = {key: value["coverage"] for key, value in series.items()}
        coverage["cell_evidence"] = self._evidence_coverage(start, end,
            [row["timestamp"] for row in target_records_all],
            truncated=cell_evidence["truncated"])
        coverage["rs485_management"] = management.get("coverage", {
            "requested_range": {"from": start, "to": end}, "quality": "unknown"})
        coverage["module_power"] = self._evidence_coverage(start, end,
            [row["timestamp"] for row in target_records_all
             if row.get("module_power_w") is not None], truncated=cell_evidence["truncated"])
        for metric, field in (("ccl", "charge_current_limit_a"),
                              ("dcl", "discharge_current_limit_a"),
                              ("charge_enable", "charge_enable"),
                              ("discharge_enable", "discharge_enable")):
            coverage[metric] = self._evidence_coverage(start, end,
                [row["timestamp"] for row in management.get("records", [])
                 if row.get(field) is not None], truncated=management.get("truncated", False))
        config = self._run_package_stage(profile, "config_context", deadline,
            lambda: self._config_context(event["start"]))
        if profile is not None:
            coverage_stage = profile["stages"]["coverage_calculation"]
            coverage_stage.update({"elapsed_seconds": time.perf_counter() - coverage_started,
                "calls": 1, "status": "complete",
                "deadline_remaining_seconds_at_entry": None,
                "deadline_remaining_seconds_at_exit": max(0.0, deadline - time.monotonic())})
            for key, value in coverage.items():
                profile["coverage_status"][key] = value.get("quality", "unknown")
            for name in ("power_derived", "cell_context_derived", "cell_minimum",
                         "cell_maximum", "cell_spread", "lowest_cell", "highest_cell",
                         "median_deviations", "balancing", "soc_recalibration"):
                stage = profile["stages"][name]
                available = (False if name == "soc_recalibration" else
                    any(row.get("balancing") is not None for row in target_records_all)
                    if name == "balancing" else bool(target_records_all))
                stage.update({"calls": 1,
                    "status": "complete" if available else "unavailable",
                    "elapsed_seconds": 0.0,
                    "deadline_remaining_seconds_at_entry": max(
                        0.0, deadline - time.monotonic()),
                    "deadline_remaining_seconds_at_exit": max(
                        0.0, deadline - time.monotonic())})
        package = {"package_schema_version": 2,
            "purpose": "reproducible_evidence_not_causal_interpretation",
            "event": event,
            "crash_core": {"event_id": event["event_id"], "physical_serial": serial,
                "start": event["start"], "end": event["end"],
                "duration_seconds": (crash_end - crash_start).total_seconds(),
                "soc_before": event["soc_before"], "soc_after": event["soc_after"],
                "soc_delta": event["soc_after"] - event["soc_before"],
                "detector_version": event["detector_version"],
                "detector_thresholds": crashes["data"]["thresholds"],
                "coverage": event["coverage"]},
            "identity_topology": {**identity,
                "position_history_id": identity.get("position_history_id"),
                "identity_epoch_id": identity.get("identity_epoch_id")},
            "timeseries": series,
            "cell_evidence": {"evidence_class": "OBSERVED", "records": target_records,
                "source_sample_count": len(target_records_all),
                "truncated": len(target_records) < len(target_records_all),
                "derived_fields_evidence_class": "DERIVED",
                "temperature_semantics": "recorded_module_temperature_channels_only"},
            "comparison_windows": comparison_windows,
            "peer_evidence": {"historical_stack_at": event["start"], "modules": peers,
                "quality": "complete" if peers else "unavailable"},
            "bms_management": management,
            "optional_evidence": {
                "balancing": {"evidence_class": "OBSERVED",
                    "quality": "complete" if any(row.get("balancing") is not None
                                                  for row in target_records_all) else "unavailable",
                    "records": [{"timestamp": row["timestamp"], "value": row["balancing"]}
                                for row in target_records_all if row.get("balancing") is not None]},
                "soc_recalibration": {"evidence_class": "OBSERVED", "quality": "unavailable",
                                      "records": []}},
            "maintenance": maintenance,
            "phase": phase,
            "alarms": alarms,
            "low_voltage": low_voltage,
            "trend_windows": trends,
            "hycube": hycube, "policy": policy, "daily_diagnostics": daily,
            "config": config,
            "coverage": coverage, "evidence_classes": ["OBSERVED", "DERIVED"],
            "inferred": False, "causality_determined": False}
        fingerprint_input = self._run_package_stage(profile, "serialization", deadline,
            lambda: json.dumps(package, sort_keys=True, separators=(",", ":")).encode())
        fingerprint = self._run_package_stage(profile, "provenance_fingerprint", deadline,
            lambda: hashlib.sha256(fingerprint_input).hexdigest())
        package["input_fingerprint"] = fingerprint
        def build_envelope():
            return research_envelope(source="guardian.evidence_package", evidence_class="DERIVED",
                authoritative=False, timestamp_from=start, timestamp_to=end, resolution="mixed",
                data=package, semantics_version=EVIDENCE_PACKAGE_VERSION,
                provenance={"event_id": event["event_id"], "physical_serial": serial,
                    "detector_version": SOC_CRASH_VERSION,
                    "config_revision": ((config.get("record") or {}).get("config_revision")
                                        or (config.get("record") or {}).get("revision")),
                    "requested_range": {"from": start, "to": end},
                    "package_created_at": datetime.now(timezone.utc).isoformat(),
                    "sources": ["guardian.cell_history", "guardian.position_history",
                        "guardian.maintenance", "guardian.canonical_phase", "guardian.events",
                        "guardian.rs485", "guardian.hycube", "guardian.daily_diagnostics"],
                    "source_fingerprint": fingerprint})
        return self._run_package_stage(profile, "envelope_build", deadline, build_envelope)
