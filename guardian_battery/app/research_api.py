"""Stable, read-only HTTP-independent Guardian Research API."""
from __future__ import annotations

from contextlib import nullcontext
from history_live_priority import historical_slot, measurement_slot
import hashlib
import base64
import json
import logging
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
from guardian_diagnostics import (GuardianDiagnosticsRepository, DiagnosticsNotFound,
                                  DiagnosticsCorrupt, InvalidDiagnosticDate)
from prepared_daily_reader import SelectedDailyRepository
from maintenance import MaintenanceEventLog
from maintenance_api import ApiResponse
from maintenance_service import MaintenanceRepository
from timeline import TechnicalEventSource
from research_identity import ResearchIdentityResolver
from research_budget import check_budget, query_budget
from research_timeseries import (CursorCodec, MAX_CELLS, MAX_POINTS,
                                 ResearchQueryError, ResearchTimeseriesService, observation_coverage, METRIC_UNITS)
from rs485_evidence import decode_identity_record
from soc_crash_events import DETECTOR_VERSION, EVENT_COLLECTION_VERSION, SocCrashEventStore

API_ROUTE = "/api/research"
SCHEMA_VERSION = 1
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SERIALS = 6
QUERY_TIMEOUT_SECONDS = 60
SOC_CRASH_VERSION = "guardian_soc_crash_v1"
LOG = logging.getLogger("guardian_battery.research")


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
    soc_crash_events: Path | None = None


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

    def run(self, endpoint, full_resolution, callback, *, cooperative_measurement=False):
        from work_scheduler import active
        scheduled = active()
        if not scheduled and not self.all.acquire(blocking=False):
            raise ResearchQueryError("busy", "research query capacity exhausted", 429)
        acquired_raw = counted_active = False
        started = time.monotonic(); request_id = str(uuid.uuid4())
        deadline = started + QUERY_TIMEOUT_SECONDS
        status, result, size = "failed", None, 0
        try:
            if full_resolution and not scheduled:
                acquired_raw = self.raw.acquire(blocking=False)
                if not acquired_raw:
                    raise ResearchQueryError("busy", "full-resolution query busy", 429)
            with self.lock:
                self.active += 1
                counted_active = True
            historical = endpoint in {"timeseries", "module-history", "cell-history", "coverage", "phases", "daily-diagnostics", "diagnostic-evidence"}
            slot = measurement_slot if cooperative_measurement else historical_slot
            with query_budget(deadline), (slot() if historical else nullcontext()):
                result = callback(deadline)
                check_budget()
                # Include response-size validation in the same budget. Do not
                # serialize a second time in finally while holding the gate lock.
                for chunk in json.JSONEncoder(ensure_ascii=False,
                        separators=(",", ":")).iterencode(result):
                    check_budget()
                    size += len(chunk.encode())
                    if size > MAX_RESPONSE_BYTES:
                        raise ResearchQueryError("response_too_large", "response exceeds 2 MiB", 413)
            status = "ok"
            return result
        except Exception:
            with self.lock: self.failures += 1
            raise
        finally:
            # Capacity must be returned even if audit bookkeeping itself fails.
            try:
                duration = time.monotonic() - started
                with self.lock:
                    if counted_active:
                        self.active -= 1
                    self.last_query_at = datetime.now(timezone.utc).isoformat()
                    data = result.get("data", {}) if isinstance(result, dict) else {}
                    records = ((data.get("point_count") or
                                len(data.get("events", data.get("alarms", ()))))
                               if isinstance(data, dict) else None)
                    self.audit.append({"request_id": request_id, "endpoint": endpoint,
                        "duration_seconds": duration, "records": records,
                        "bytes": size, "truncated": (result or {}).get("truncated"),
                        "status": status})
                    self.audit = self.audit[-100:]
            finally:
                if acquired_raw: self.raw.release()
                if not scheduled: self.all.release()


class GuardianResearchApi:
    SOURCE_REGISTRY = frozenset({"guardian.cell_history", "guardian.hycube",
        "guardian.display_history", "guardian.canonical_phase"})

    def __init__(self, paths, cursor_secret=None, *, window_backend=None,
                 identity_backend=None, hycube_reader=None, display_reader=None,
                 canonical_reader=None, scheduler=None, read_scope=None, rs485_window=None, daily_repository_factory=None, policy_reader=None):
        from manual_history_page import ManualHistoryPages
        self.manual_pages = ManualHistoryPages(cursor_secret)
        self.paths = paths
        self.window_backend = window_backend
        self.identity_backend = identity_backend
        self.hycube_reader = hycube_reader
        self.display_reader = display_reader
        self.canonical_reader = canonical_reader
        self.rs485_window = rs485_window
        self.daily_repository_factory = daily_repository_factory
        self.policy_reader = policy_reader
        self.scheduler = scheduler
        self.read_scope = read_scope or nullcontext
        # Persisted-event requests must not load any measurement/identity history.
        self._identity = None
        self._position_signature = None
        self.series = ResearchTimeseriesService(
            paths.cell_history, identity_backend, CursorCodec(cursor_secret or secrets.token_bytes(32)),
            window_backend=window_backend)
        self.event_store = (SocCrashEventStore(paths.soc_crash_events)
                            if paths.soc_crash_events is not None else None)
        self.gate = QueryGate()

    @property
    def identity(self):
        if self._identity is None:
            self._refresh_identity()
        return self._identity

    @staticmethod
    def _file_signature(path):
        try:
            stat = Path(path).stat(); return stat.st_size, stat.st_mtime_ns
        except OSError:
            return None

    def _refresh_identity(self):
        check_budget()
        if self.window_backend is not None:
            if self.identity_backend is None:
                raise ResearchQueryError('history_preparation_required', 'Prepared identity metadata unavailable', 503)
            self._identity = self.identity_backend
            self.series.identity = self._identity
            return
        signature = self._file_signature(self.paths.position_history)
        if self._identity is None or signature != self._position_signature:
            identity = ResearchIdentityResolver.from_path(self.paths.position_history)
            check_budget()
            self._identity = identity
            self.series.identity = self._identity
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

    def handle(self, method, target, *, cancelled=None, deadline=None):
        if deadline is None: deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
        from work_scheduler import WorkCancelled, AdmissionError
        from research_budget import cancellation_scope
        def run():
            with cancellation_scope(cancelled), query_budget(deadline):
                return self._handle(method, target)
        try:
            if self.scheduler is None:
                return run()
            return self.scheduler.execute('manual_research_read', run, lane='read',
                cancelled=cancelled, deadline=deadline)
        except (WorkCancelled, AdmissionError, ResearchQueryError) as exc:
            code = getattr(exc, 'code', 'cancelled' if isinstance(exc, WorkCancelled) else 'scheduler_full')
            return ApiResponse(503, self.error(code, str(exc)))

    def _handle(self, method, target):
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
            def read_page(deadline):
                if values.get('paged')=='true' and endpoint in {'timeseries','module-history','cell-history'}:
                    return self.manual_pages.run(endpoint,values,
                        lambda page:self._dispatch(endpoint,page,deadline))
                return self._dispatch(endpoint,values,deadline)
            def dispatch(deadline):
                # Persisted SOC events retain their measurement/identity-free path.
                if endpoint == 'soc-crash-events' or endpoint.startswith('soc-crash-events/'):
                    return self._dispatch(endpoint, values, deadline)
                with self.read_scope():
                    return read_page(deadline)
            # These readers access bounded existing products, without raw-writer
            # locks, rebuilding or background source scans.
            measurement = (endpoint in {"cell-history", "phases", "daily-diagnostics", "diagnostic-evidence"} or
                (endpoint in {"timeseries", "module-history"} and
                 values.get("source", "guardian.cell_history") in {"guardian.cell_history", "guardian.canonical_phase"}) or
                (endpoint == "coverage" and bool(values.get("datasets", "soc")) and
                 set(values.get("datasets", "soc").split(",")) <= {
                    "soc", "module_voltage", "module_current", "cell_voltage", "cell_temperature"}))
            body = self.gate.run(endpoint, values.get("resolution") == "full", dispatch,
                                 cooperative_measurement=measurement)
            return ApiResponse(200, body)
        except InvalidDiagnosticDate:
            return ApiResponse(400, self.error("invalid_argument", "Diagnostic date must be a valid YYYY-MM-DD date"))
        except DiagnosticsNotFound:
            return ApiResponse(503, self.error("history_preparation_required", "Requested persisted daily product is not available"))
        except DiagnosticsCorrupt:
            return ApiResponse(503, self.error("history_product_invalid", "Requested persisted daily product is inconsistent"))
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
            if self.window_backend is not None and self.policy_reader is None:
                raise ResearchQueryError('history_preparation_required', 'Indexed policy reader is not prepared', 503)
            segments = (self.policy_reader or HycubePolicyHistory(self.paths.hycube_policy)).query(
                timestamp_from=start, timestamp_to=end)
            return research_envelope(source="guardian.hycube", evidence_class="OBSERVED",
                authoritative=True, timestamp_from=start, timestamp_to=end,
                resolution="policy_segments", data={"segments": segments},
                quality="complete" if segments else "absent",
                provenance={"source_fingerprint": (hashlib.sha256(json.dumps(segments,sort_keys=True).encode()).hexdigest()
                    if self.policy_reader is not None else self._directory_fingerprint(self.paths.hycube_policy))})
        if metric not in {"battery_capacity", "soc"}:
            raise ResearchQueryError("invalid_argument", "Hycube metric must be battery_capacity")
        reader = self.hycube_reader or HycubeBatteryCapacitySeries(self.paths.hycube_history,
            projection_directory=self.paths.hycube_projection)
        if self.window_backend is not None and self.hycube_reader is None:
            raise ResearchQueryError('history_preparation_required', 'Indexed Hycube reader unavailable', 503)
        result = reader.query(
                timestamp_from=start, timestamp_to=end, max_points=max_points)
        status = "complete" if result.get("points") else "absent"
        return research_envelope(source="guardian.hycube", evidence_class="OBSERVED",
            authoritative=True, timestamp_from=start, timestamp_to=end,
            resolution=result.get("source_mode", "auto"), data=result, quality=status,
            provenance={"source_fingerprint": (reader.window_backend.metrics.get('source_fingerprint')
                if self.window_backend is not None else self._directory_fingerprint(self.paths.hycube_history))})

    def _query_display(self, values):
        self._required(values, "physical_serial", "metric")
        start, end = self._range(values)
        if not self.paths.display_history:
            raise ResearchQueryError("source_unavailable", "Display History unavailable", 503)
        epochs = self.identity.epochs(values["physical_serial"], start, end)
        positioned = [item for item in epochs if item["position"] is not None]
        if not positioned:
            raise ResearchQueryError("identity_unresolved", "identity has no position in range", 404)
        if self.display_reader is not None:
            reader = self.display_reader
        elif self.window_backend is not None:
            raise ResearchQueryError('history_preparation_required', 'Published display reader unavailable', 503)
        else:
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
        data_states = []
        for epoch in positioned:
            check_budget()
            lower = max(start, epoch["valid_from"])
            upper = min(end, epoch["valid_to"] or end)
            result = reader.query_bundles(requests=({"metric": metric_map[values["metric"]],
                "cell_numbers": tuple(int(item) for item in values.get("cell_numbers", "").split(",")
                                      if item)},), timestamp_from=lower, timestamp_to=upper,
                module_number=epoch["position"], max_points=int(values.get("max_points", MAX_POINTS)),
                **({'include_samples': False} if self.window_backend is not None else {}))
            if result.get('data_state'): data_states.append(result['data_state'])
            for point in result["series"][0]["points"]:
                check_budget()
                if self.window_backend is not None:
                    serial = point.get('physical_serial', point.get('module_serial'))
                    if serial is not None and serial != values['physical_serial']: continue
                    if epoch['valid_to'] and datetime.fromisoformat(point['timestamp']) >= datetime.fromisoformat(epoch['valid_to']): continue
                points.append({**point, "physical_serial": values["physical_serial"],
                    "position_at_time": epoch["position"],
                    "position_history_id": epoch["position_history_id"],
                    "identity_epoch_id": epoch["identity_epoch_id"]})
        points.sort(key=lambda item: (item["timestamp"], item.get("cell_number", 0)))
        return research_envelope(source="guardian.display_history", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="display",
            data={"metric": values["metric"], "unit": METRIC_UNITS[values["metric"]],
                  "physical_serial": values["physical_serial"],
                  "points": points[:MAX_POINTS], "point_count": min(len(points), MAX_POINTS),
                  **({'data_states': data_states} if self.window_backend is not None else {})},
            quality="complete" if points else "absent",
            truncated=len(points) > MAX_POINTS,
            semantics_version="guardian_display_history_v1",
            provenance={"source_fingerprint": (hashlib.sha256(json.dumps(data_states, sort_keys=True).encode()).hexdigest()
                if self.window_backend is not None else self._directory_fingerprint(self.paths.display_history))})

    @staticmethod
    def _directory_fingerprint(path):
        if not path or not Path(path).exists(): return None
        values = [(item.name, item.stat().st_size, item.stat().st_mtime_ns)
                  for item in sorted(Path(path).glob("*")) if item.is_file()]
        return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()

    def _persisted_soc_events(self, endpoint, values):
        if endpoint == "soc-crash-events":
            if set(values) - {"from", "to", "max_records"}:
                raise ResearchQueryError("invalid_argument", "unsupported event parameters")
            start, end = self._range(values)
            start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if (end_dt - start_dt).total_seconds() > 31 * 86400:
                raise ResearchQueryError("range_too_large", "event range must not exceed 31 days", 413)
            try:
                limit = int(values.get("max_records", "100"))
            except ValueError as exc:
                raise ResearchQueryError("invalid_argument", "max_records must be 1..500") from exc
            if not 1 <= limit <= 500:
                raise ResearchQueryError("invalid_argument", "max_records must be 1..500")
            if self.event_store is None:
                raise ResearchQueryError("source_unavailable", "event store is not configured", 503)
            events, truncated = self.event_store.list(start_dt, end_dt, limit)
            data = {"events": [event.to_dict() for event in events]}
        else:
            if values:
                raise ResearchQueryError("invalid_argument", "single-event query takes no parameters")
            event_id = endpoint.removeprefix("soc-crash-events/")
            if self.event_store is None:
                raise ResearchQueryError("source_unavailable", "event store is not configured", 503)
            event = self.event_store.get(event_id)
            if event is None:
                raise ResearchQueryError("not_found", "SOC event not found", 404)
            start, end = event.observation_started_at, event.confirmed_at
            data, truncated = {"event": event.to_dict()}, False
        return research_envelope(source="guardian.soc_crash_events", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end,
            resolution="events", data=data, truncated=truncated,
            semantics_version=EVENT_COLLECTION_VERSION if endpoint == "soc-crash-events" else event.detector_version)

    def _dispatch(self, endpoint, values, deadline):
        if endpoint == "soc-crash-events" or endpoint.startswith("soc-crash-events/"):
            return self._persisted_soc_events(endpoint, values)
        if endpoint in {"events/soc-crashes", "evidence-package", "evidence-core"}:
            raise ResearchQueryError("not_found", "legacy SOC workflow is retired", 404)
        if self.window_backend is not None and endpoint not in {
                'status', 'topology', 'identity-epochs', 'timeseries', 'module-history',
                'cell-history', 'coverage', 'phases', 'maintenance', 'alarms'} and not (
                (endpoint == 'events/low-voltage' and self.rs485_window is not None) or
                (endpoint in {'daily-diagnostics', 'diagnostic-evidence'} and self.daily_repository_factory is not None)):
            raise ResearchQueryError('history_preparation_required', 'This reader has no prepared local publication', 503)
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
        if endpoint == "events/low-voltage": return self._low_voltage(values, deadline)
        if endpoint == "alarms": return self._alarms(values)
        raise ResearchQueryError("invalid_argument", "research endpoint not found", 404)

    def _coverage(self, values, deadline):
        self._required(values, "physical_serial"); start, end = self._range(values)
        mapping = {"soc": "soc", "module_voltage": "module_voltage",
            "module_current": "module_current", "cell_voltage": "cell_voltage",
                      "cell_temperature": "cell_temperature"}
        allowed = set(mapping) | {"hycube", "policy", "maintenance", "rs485",
            "soh", "cycles", "ah_coulomb", "daily_diagnostics"}
        datasets = list(dict.fromkeys(filter(None, values.get("datasets", "soc").split(","))))
        if not datasets or any(dataset not in allowed for dataset in datasets):
            raise ResearchQueryError("invalid_argument", "coverage dataset is unsupported")
        coverage = self.series.coverage([mapping[d] for d in datasets if d in mapping],
            values["physical_serial"], start, end, deadline) if any(d in mapping for d in datasets) else {}
        rows = []
        for dataset in datasets:
            check_budget()
            if dataset in mapping:
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                             **coverage[mapping[dataset]]})
            elif dataset in {"hycube", "policy"} and self.paths.hycube_history:
                response = self._query_hycube({"metric": "policy" if dataset == "policy"
                    else "battery_capacity", "from": start, "to": end, "max_points": "6000"})
                if dataset == "policy":
                    coverage_row = self._policy_coverage(start, end, response["data"].get("segments", ()))
                else:
                    coverage_row = self._coverage_row(start, end,
                        [point["timestamp"] for point in response["data"].get("points", ())])
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                    **coverage_row})
            elif dataset == "maintenance":
                response = self._maintenance({"physical_serial": values["physical_serial"],
                    "from": start, "to": end, "max_records": "10000"})
                timestamps = [event["occurred_at"] for event in response["data"]["events"]]
                rows.append({"dataset": dataset, "physical_serial": values["physical_serial"],
                    **self._event_coverage(start, end, timestamps)})
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
        epochs = [datetime.fromisoformat(item.replace("Z", "+00:00")).timestamp()
                  for item in timestamps]
        return observation_coverage(start, end, epochs)

    @staticmethod
    def _policy_coverage(start, end, segments):
        """Use the policy reader's validity intervals, never change-point cadence."""
        intervals = []
        for segment in sorted(segments, key=lambda item: item["from"]):
            check_budget()
            lower, upper = max(start, segment["from"]), min(end, segment["to"])
            if lower > upper:
                continue
            if intervals and lower <= intervals[-1]["to"]:
                intervals[-1]["to"] = max(upper, intervals[-1]["to"])
            else:
                intervals.append({"from": lower, "to": upper})
        missing = []
        cursor = start
        for interval in intervals:
            check_budget()
            if interval["from"] > cursor:
                missing.append({"from": cursor, "to": interval["from"],
                    "kind": "leading" if cursor == start else "internal"})
            cursor = interval["to"]
        if not intervals:
            missing.append({"from": start, "to": end, "kind": "entire_range"})
        elif cursor < end:
            missing.append({"from": cursor, "to": end, "kind": "trailing"})
        observations = sorted(segment["observed_at"] for segment in segments)
        return {"requested_range": {"from": start, "to": end},
            "covered_intervals": intervals, "missing_intervals": missing,
            "first_observation": observations[0] if observations else None,
            "last_observation": observations[-1] if observations else None,
            "sample_count": len(segments), "expected_cadence_seconds": None,
            "largest_gap_seconds": None, "coverage_basis": "policy_intervals",
            "quality": "complete" if intervals and not missing else "partial" if intervals else "absent"}

    @staticmethod
    def _event_coverage(start, end, timestamps):
        """Event presence/count cannot establish continuous recording coverage."""
        timestamps = sorted(timestamps)
        return {"requested_range": {"from": start, "to": end},
            "covered_intervals": [], "missing_intervals": [],
            "first_observation": timestamps[0] if timestamps else None,
            "last_observation": timestamps[-1] if timestamps else None,
            "sample_count": len(timestamps), "expected_cadence_seconds": None,
            "largest_gap_seconds": None, "coverage_basis": "event_occurrences",
            "quality": "unknown"}

    def _maintenance(self, values):
        start, end = self._range(values)
        events = (self.identity_backend.maintenance_window(start, end) if self.window_backend is not None
                  else MaintenanceRepository(MaintenanceEventLog(self.paths.maintenance)).list(include_archived=True))
        rows = []
        for event in events:
            check_budget()
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
        if self.window_backend is not None and self.canonical_reader is None:
            raise ResearchQueryError('history_preparation_required', 'Published phase reader unavailable', 503)
        reader = self.canonical_reader or CanonicalPhaseReader(self.paths.canonical_phase)
        result = reader.query(
            start, end, physical_serial=values["physical_serial"])
        return research_envelope(source="guardian.canonical_phase", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="intervals",
            data=result, quality="complete" if result.get("available") else "absent",
            semantics_version=result.get("semantics_version", "guardian_canonical_phase_v2"),
            provenance={"physical_serial": values["physical_serial"]})

    def _daily(self, values):
        if values.get('date'):
            start = end = values['date']
        else:
            start, end = self._range(values)
        repository = (self.daily_repository_factory(self.paths.daily_diagnostics,start[:10],end[:10])
                      if self.daily_repository_factory is not None else
                      SelectedDailyRepository(self.paths.daily_diagnostics,start[:10],end[:10]))
        if values.get("date"):
            data = repository.day_dto(values["date"])
        else:
            data = repository.days_dto()
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
                    check_budget()
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

    def _soc_crashes(self, values, deadline):
        start, end = self._range(values)
        serials = ([values["physical_serial"]] if values.get("physical_serial") else
                   sorted({item["physical_serial"] for item in self.identity.epochs()}))[:MAX_SERIALS]
        events = []
        observations = self.series.soc_current_by_serial(serials, start, end, deadline)
        for serial in serials:
            rows = observations.get(serial, ()); candidates = []
            for before, after in zip(rows, rows[1:]):
                gap = datetime.fromisoformat(after["timestamp"]).timestamp() - datetime.fromisoformat(before["timestamp"]).timestamp()
                loss, value = before["soc"] - after["soc"], after["current"]
                if 0 <= gap <= 300 and loss >= 2 and value < -0.2:
                    candidates.append({"start": before["timestamp"], "end": after["timestamp"],
                        "soc_before": before["soc"], "soc_after": after["soc"],
                        "current": value, "identity_epoch_id": after["identity_epoch_id"],
                        "lowest_cell": after.get("lowest_cell"),
                        "cell_spread_mv": after.get("cell_spread_mv")})
            groups = []
            for item in candidates:
                gap = ((datetime.fromisoformat(item["start"]) -
                        datetime.fromisoformat(groups[-1][-1]["end"])).total_seconds()
                       if groups else None)
                if groups and gap <= 600 and item["identity_epoch_id"] == groups[-1][-1]["identity_epoch_id"]:
                    groups[-1].append(item)
                else: groups.append([item])
            for group in groups:
                position = self.identity.position_at(serial, group[0]["start"])
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
        return research_envelope(source="guardian.cell_history", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="events",
            data={"events": events, "detector_version": SOC_CRASH_VERSION,
                "thresholds": {"soc_loss_pp": 2, "sample_gap_seconds": 300,
                    "discharge_current_below_a": -0.2, "merge_gap_seconds": 600}},
            semantics_version=SOC_CRASH_VERSION)

    def _low_voltage(self, values, deadline):
        self._required(values, "physical_serial"); start, end = self._range(values)
        result = self.series.query(metric="cell_voltage", physical_serial=values["physical_serial"],
            timestamp_from=start, timestamp_to=end, resolution=values.get("resolution", "auto"),
            max_points=int(values.get("max_points", MAX_POINTS)), deadline=deadline)
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
        if self.rs485_window is not None:
            records = []
            with self.rs485_window.read(start, end) as selected:
                for record, reference in selected:
                    check_budget()
                    if time.monotonic() > deadline:
                        raise ResearchQueryError('timeout', 'research query timed out', 503)
                    identity = json.loads(reference.identity_context).get('bus_identity')
                    if identity is None or identity.get('serial_string') != serial:
                        continue
                    if decode_identity_record(record) is not None:
                        continue
                    decoded = record.get('decoded') or {}
                    low = {key:value for key,value in decoded.items()
                           if 'low_voltage' in key or 'under_voltage' in key}
                    if low or record.get('paired_command') == 0x44:
                        records.append({'timestamp':record['timestamp'], 'physical_serial':serial,
                            'adr':record.get('adr'), 'paired_command':record.get('paired_command'),
                            'checksum_valid':record.get('checksum_valid'),
                            'request_matched':record.get('request_matched'), 'decoded':low or None})
                    if len(records) >= 10000:
                        return {'quality':'partial', 'records':records, 'truncated':True}
            return {'quality':'complete' if records else 'absent', 'records':records,
                    'truncated':False}
        if not self.paths.rs485_history or not Path(self.paths.rs485_history).exists():
            return {"quality": "absent", "records": []}
        identities, records = {}, []
        first, last = start[:10], end[:10]
        for path in sorted(Path(self.paths.rs485_history).glob("*.jsonl")):
            check_budget()
            if not first <= path.stem <= last: continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    check_budget()
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

    def _config_context(self, timestamp):
        if not self.paths.config_history or not Path(self.paths.config_history).is_file():
            return {"quality": "absent", "record": None}
        selected = None
        try:
            with Path(self.paths.config_history).open(encoding="utf-8") as handle:
                for line in handle:
                    check_budget()
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
        events = (self.identity_backend.technical_window(start, end)
                  if self.window_backend is not None else
                  TechnicalEventSource(self.paths.technical_events).read(check=check_budget) if self.paths.technical_events else ())
        for event in events:
            check_budget()
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

    def _package(self, values, deadline):
        self._required(values, "event_id")
        reference = self._event_reference(values["event_id"])
        detector_values = {"physical_serial": reference["s"], "from": reference["f"],
                           "to": reference["t"]}
        crashes = self._soc_crashes(detector_values, deadline)
        event = next((item for item in crashes["data"]["events"]
                      if item["event_id"] == values["event_id"]), None)
        if event is None: raise ResearchQueryError("coverage_absent", "event is unavailable", 404)
        def duration(value, default):
            raw = value or default
            if raw.startswith("PT") and raw.endswith("H"): return float(raw[2:-1]) * 3600
            if raw.startswith("PT") and raw.endswith("M"): return float(raw[2:-1]) * 60
            if raw.startswith("P") and raw.endswith("D"): return float(raw[1:-1]) * 86400
            raise ResearchQueryError("invalid_argument", "duration must use PTnH, PTnM, or PnD")
        serial = event["physical_serial"]
        start = (datetime.fromisoformat(event["start"]) -
                 timedelta(seconds=duration(values.get("before"), "PT1H"))).isoformat()
        end = (datetime.fromisoformat(event["end"]) +
               timedelta(seconds=duration(values.get("after"), "PT1H"))).isoformat()
        series = {}
        for metric in ("soc", "module_current", "module_voltage", "cell_voltage", "cell_temperature"):
            series[metric] = self.series.query(metric=metric, physical_serial=serial,
                timestamp_from=start, timestamp_to=end, resolution="auto", max_points=800,
                deadline=deadline)
        trend_windows = [item for item in values.get("trend_windows", "PT6H,P1D,P7D").split(",")
                         if item]
        if len(trend_windows) > 3:
            raise ResearchQueryError("invalid_argument", "at most three trend windows are allowed")
        trends = {}
        event_end = datetime.fromisoformat(event["end"])
        for window in trend_windows:
            window_start = (event_end - timedelta(seconds=duration(window, window))).isoformat()
            trends[window] = self.series.query(metric="soc", physical_serial=serial,
                timestamp_from=window_start, timestamp_to=event["end"], resolution="auto",
                max_points=200, deadline=deadline)
        def isolated(name, callback):
            try: return callback()
            except Exception as exc:
                LOG.warning("Research package source unavailable source=%s error=%s",
                            name, type(exc).__name__)
                return {"quality": "unknown", "error": "source_unavailable"}
        hycube = isolated("hycube", lambda: self._query_hycube({"metric": "battery_capacity",
            "from": start, "to": end, "max_points": "200"})["data"])
        policy = isolated("policy", lambda: self._query_hycube({"metric": "policy",
            "from": start, "to": end})["data"])
        daily = isolated("daily_diagnostics", lambda: self._daily({
            "date": event["start"][:10], "physical_serial": serial})["data"])
        package = {"event": event, "identity_topology": self.identity.position_at(serial, event["start"]),
            "timeseries": series,
            "maintenance": self._maintenance({"physical_serial": serial, "from": start, "to": end})["data"],
            "phase": self._phases({"physical_serial": serial, "from": start, "to": end})["data"],
            "alarms": self._alarms({"physical_serial": serial, "from": start, "to": end})["data"],
            "low_voltage": self._low_voltage({"physical_serial": serial, "from": start,
                                               "to": end, "max_points": "200"}, deadline)["data"],
            "trend_windows": trends,
            "hycube": hycube, "policy": policy, "daily_diagnostics": daily,
            "config": self._config_context(event["start"]),
            "coverage": {key: value["coverage"] for key, value in series.items()}, "inferred": False}
        fingerprint = hashlib.sha256(json.dumps(package, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        package["input_fingerprint"] = fingerprint
        return research_envelope(source="guardian.evidence_package", evidence_class="DERIVED",
            authoritative=False, timestamp_from=start, timestamp_to=end, resolution="mixed",
            data=package, semantics_version="research_evidence_package_v1",
            provenance={"physical_serial": serial, "source_fingerprint": fingerprint})
