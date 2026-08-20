"""Read-only API for Guardian-owned series plus timeline-based overlays."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

from event_overlay import EventOverlayAdapter, OverlayContext
from history_series import CellHistorySeries, SERIES_METRICS, SeriesHistoryError
from maintenance import MaintenanceValidationError, normalize_utc_timestamp
from maintenance_api import ApiResponse, error_json
from maintenance_service import MaintenanceHistoryError
from timeline import TechnicalHistoryError
from phase_engine import PhaseEngineError, PHASE_PARAMETER_KEYS


LOG = logging.getLogger(__name__)
HISTORY_API_ROUTE = "/api/history/series"
HISTORY_FILTERS = frozenset(
    {"metric", "from", "to", "module_number", "cell_number", "include_archived", "active",
     "analysis_mode", "what_if_low_soc_percent", "what_if_high_soc_percent",
     "what_if_charge_current_a", "what_if_discharge_current_a"}
)


class HistoryApiProblem(ValueError):
    pass


class HistoryApi:
    def __init__(self, series: CellHistorySeries, overlays: EventOverlayAdapter, phase_engine=None):
        self.series = series
        self.overlays = overlays
        self.phase_engine = phase_engine

    def handle(self, method: str, target: str) -> ApiResponse:
        try:
            if method != "GET":
                return ApiResponse(405, error_json("method_not_allowed", "Method not allowed"), {"Allow": "GET"})
            split = urlsplit(target)
            marker = split.path.find(HISTORY_API_ROUTE)
            if marker < 0 or split.path[marker + len(HISTORY_API_ROUTE):].strip("/"):
                return ApiResponse(404, error_json("not_found", "History API route not found"))
            return self._query(split.query)
        except (HistoryApiProblem, PhaseEngineError) as exc:
            return ApiResponse(400, error_json("invalid_request", str(exc)))
        except SeriesHistoryError as exc:
            LOG.error("Guardian series unavailable: %s", exc)
            return ApiResponse(503, error_json("series_history_error", "Guardian series history is unavailable"))
        except MaintenanceHistoryError as exc:
            LOG.error("Maintenance overlay unavailable: %s", exc)
            return ApiResponse(503, error_json("maintenance_history_error", "Maintenance history is unavailable"))
        except TechnicalHistoryError as exc:
            LOG.error("Timeline overlay unavailable: %s", exc)
            return ApiResponse(503, error_json("technical_history_error", "Technical event history is unavailable"))
        except Exception:
            LOG.exception("Unhandled history API error")
            return ApiResponse(500, error_json("internal_error", "Internal server error"))

    def _query(self, query: str) -> ApiResponse:
        raw = parse_qs(query, keep_blank_values=True)
        unknown = set(raw) - HISTORY_FILTERS
        if unknown:
            raise HistoryApiProblem(f"unknown query parameters: {', '.join(sorted(unknown))}")
        if any(len(values) != 1 for values in raw.values()):
            raise HistoryApiProblem("query parameters must occur once")
        values = {key: items[0] for key, items in raw.items()}
        missing = {"metric", "from", "to", "module_number"} - set(values)
        if missing:
            raise HistoryApiProblem(f"missing query parameters: {', '.join(sorted(missing))}")
        metric = values["metric"]
        if metric not in SERIES_METRICS:
            raise HistoryApiProblem("metric is unsupported")
        timestamp_from = self._timestamp(values["from"], "from")
        timestamp_to = self._timestamp(values["to"], "to")
        if timestamp_from > timestamp_to:
            raise HistoryApiProblem("from must not exceed to")
        module_number = self._integer(values["module_number"], "module_number", 1, 6)
        cell_number = self._integer(values.get("cell_number"), "cell_number", 1, 15)
        active = self._active(values.get("active", "true"))
        include_archived = self._boolean(values.get("include_archived", "false"))
        if active in (False, None):
            include_archived = True
        points = self.series.query(metric=metric, timestamp_from=timestamp_from,
                                   timestamp_to=timestamp_to, module_number=module_number,
                                   cell_number=cell_number)
        markers = self.overlays.markers(OverlayContext(
            timestamp_from=timestamp_from, timestamp_to=timestamp_to,
            module_number=module_number, cell_number=cell_number,
            event_types=("maintenance",), include_archived=include_archived,
            active=active,
        ))
        mode = values.get("analysis_mode", "historical")
        phases = []
        if self.phase_engine is not None:
            what_if = None
            if mode == "what_if":
                names = {
                    "cell_diag_low_soc_percent": "what_if_low_soc_percent",
                    "cell_diag_high_soc_percent": "what_if_high_soc_percent",
                    "cell_diag_charge_current_a": "what_if_charge_current_a",
                    "cell_diag_discharge_current_a": "what_if_discharge_current_a",
                }
                try: what_if = {key: float(values[source]) for key, source in names.items()}
                except (KeyError, ValueError) as exc: raise HistoryApiProblem("what-if mode requires four numeric phase parameters") from exc
            samples = self.series.samples(timestamp_from=timestamp_from, timestamp_to=timestamp_to,
                                          module_number=module_number)
            phases = self.phase_engine.intervals(samples, mode=mode, what_if=what_if,
                                                  window_to=timestamp_to)
        return ApiResponse(200, {
            "series": {"metric": metric, "module_number": module_number,
                       "cell_number": cell_number, "points": points},
            "overlays": [marker.to_dict() for marker in markers],
            "window": {"from": timestamp_from, "to": timestamp_to, "inclusive": True},
            "semantics": {"overlay_timestamp": "occurred_at", "correlation_only": True},
            "phase_analysis": {"mode": mode, "intervals": phases,
                               "raw_measurements_unchanged": True},
        })

    @staticmethod
    def _timestamp(value: str, field: str) -> str:
        try:
            return normalize_utc_timestamp(value, field)
        except MaintenanceValidationError as exc:
            raise HistoryApiProblem(str(exc)) from exc

    @staticmethod
    def _integer(value: str | None, field: str, minimum: int, maximum: int) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise HistoryApiProblem(f"{field} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise HistoryApiProblem(f"{field} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _boolean(value: str) -> bool:
        if value == "true": return True
        if value == "false": return False
        raise HistoryApiProblem("include_archived must be true or false")

    @staticmethod
    def _active(value: str) -> bool | None:
        if value == "true": return True
        if value == "false": return False
        if value == "all": return None
        raise HistoryApiProblem("active must be true, false or all")
