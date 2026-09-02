"""Read-only API for Guardian-owned series plus timeline-based overlays."""

from __future__ import annotations

import logging
import json
import time
from urllib.parse import parse_qs, urlsplit

from event_overlay import EventOverlayAdapter, OverlayContext
from history_series import CellHistorySeries, SERIES_METRICS, SeriesHistoryError
from rs485_evidence import RS485_SERIES_METRICS, Rs485HistorySeries
from hycube_evidence import HycubeBatteryCapacitySeries, HycubeHistoryError
from maintenance import MaintenanceValidationError, normalize_utc_timestamp
from maintenance_api import ApiResponse, error_json
from maintenance_service import MaintenanceHistoryError
from timeline import TechnicalHistoryError
from phase_engine import PhaseEngineError, PHASE_PARAMETER_KEYS
from maintenance_diagnostics import project_maintenance_boundaries


LOG = logging.getLogger(__name__)
HISTORY_API_ROUTE = "/api/history/series"
HISTORY_FILTERS = frozenset(
    {"metric", "metrics", "from", "to", "module_number", "cell_number", "cell_numbers",
     "voltage_cell_numbers", "temperature_cell_numbers", "include_archived", "active",
     "analysis_mode", "what_if_low_soc_percent", "what_if_high_soc_percent",
     "what_if_charge_current_a", "what_if_discharge_current_a"}
)


class HistoryApiProblem(ValueError):
    pass


class HistoryApi:
    def __init__(self, series: CellHistorySeries, overlays: EventOverlayAdapter, phase_engine=None,
                 rs485_series: Rs485HistorySeries | None = None,
                 hycube_series: HycubeBatteryCapacitySeries | None = None):
        self.series = series
        self.overlays = overlays
        self.phase_engine = phase_engine
        self.rs485_series = rs485_series
        self.hycube_series = hycube_series

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
        except HycubeHistoryError as exc:
            LOG.error("Hycube history unavailable: %s", exc)
            return ApiResponse(503, error_json("hycube_history_error", "Hycube history is unavailable"))
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
        missing = {"from", "to", "module_number"} - set(values)
        if missing:
            raise HistoryApiProblem(f"missing query parameters: {', '.join(sorted(missing))}")
        if ("metric" in values) == ("metrics" in values):
            raise HistoryApiProblem("exactly one of metric or metrics is required")
        combined = "metrics" in values
        metrics = self._metrics(values.get("metrics")) if combined else (values["metric"],)
        supported_metrics = SERIES_METRICS | RS485_SERIES_METRICS
        if any(metric not in supported_metrics for metric in metrics):
            raise HistoryApiProblem("metric is unsupported")
        timestamp_from = self._timestamp(values["from"], "from")
        timestamp_to = self._timestamp(values["to"], "to")
        if timestamp_from > timestamp_to:
            raise HistoryApiProblem("from must not exceed to")
        module_number = self._integer(values["module_number"], "module_number", 1, 6)
        cell_number = self._integer(values.get("cell_number"), "cell_number", 1, 15)
        cell_numbers = self._integers(values.get("cell_numbers"), "cell_numbers", 1, 15)
        voltage_cells = self._integers(values.get("voltage_cell_numbers"),
                                       "voltage_cell_numbers", 1, 15)
        temperature_cells = self._integers(values.get("temperature_cell_numbers"),
                                           "temperature_cell_numbers", 1, 15)
        if cell_number is not None and cell_numbers is not None:
            raise HistoryApiProblem("cell_number and cell_numbers are mutually exclusive")
        if combined and (cell_number is not None or cell_numbers is not None):
            raise HistoryApiProblem("combined mode uses metric-specific cell selections")
        if not combined and (voltage_cells is not None or temperature_cells is not None):
            raise HistoryApiProblem("metric-specific cell selections require combined mode")
        if cell_numbers is not None and metrics[0] not in {"cell_voltage", "cell_temperature"}:
            raise HistoryApiProblem("cell_numbers requires a cell metric")
        if voltage_cells is not None and "cell_voltage" not in metrics:
            raise HistoryApiProblem("voltage_cell_numbers requires cell_voltage")
        if temperature_cells is not None and "cell_temperature" not in metrics:
            raise HistoryApiProblem("temperature_cell_numbers requires cell_temperature")
        active = self._active(values.get("active", "true"))
        include_archived = self._boolean(values.get("include_archived", "false"))
        if active in (False, None):
            include_archived = True
        backend_started = time.perf_counter()
        requests = []
        for metric in metrics:
            selected = (voltage_cells if metric == "cell_voltage" else temperature_cells
                        if metric == "cell_temperature" else None)
            requests.append({"metric": metric,
                             "cell_number": None if combined else cell_number,
                             "cell_numbers": selected if combined else cell_numbers})
        cell_requests = [item for item in requests if item["metric"] not in RS485_SERIES_METRICS]
        bundle = self.series.query_bundles(
            requests=cell_requests or [{"metric": "soc", "cell_number": None, "cell_numbers": None}],
            timestamp_from=timestamp_from, timestamp_to=timestamp_to,
            module_number=module_number, include_all_module_soc="soc" in metrics)
        hycube = (self.hycube_series.query(timestamp_from=timestamp_from,
                                           timestamp_to=timestamp_to,
                                           max_points=max(4, 6000 // 7))
                  if "soc" in metrics and self.hycube_series is not None else None)
        projected_series = list(bundle["series"] if cell_requests else [])
        if any(item["metric"] in RS485_SERIES_METRICS for item in requests):
            if self.rs485_series is None:
                raise HistoryApiProblem("RS485 history is unavailable")
            rs485_requests = [item for item in requests if item["metric"] in RS485_SERIES_METRICS]
            projected_series.extend(self.rs485_series.query_bundles(
                rs485_requests, timestamp_from=timestamp_from, timestamp_to=timestamp_to,
                module_number=module_number))
        by_metric = {item["metric"]: item for item in projected_series}
        projected_series = [by_metric[metric] for metric in metrics]
        marker_cells = (tuple(sorted(set((voltage_cells or ()) + (temperature_cells or ()))))
                        or (None,)) if combined else cell_numbers or (cell_number,)
        projected = [marker for selected_cell in marker_cells
                     for marker in self.overlays.markers(OverlayContext(
                         timestamp_from=timestamp_from, timestamp_to=timestamp_to,
                         module_number=module_number, cell_number=selected_cell,
                         event_types=("maintenance",), include_archived=include_archived,
                         active=active))]
        markers = list({(marker.event_type, marker.maintenance_event_id, marker.timestamp,
                         marker.title): marker for marker in projected}.values())
        markers.sort(key=lambda marker: (marker.timestamp, marker.event_type,
                                         marker.maintenance_event_id or marker.title))
        maintenance_boundaries = project_maintenance_boundaries(
            [{**marker.to_dict(), "occurred_at": marker.timestamp}
             for marker in markers if marker.event_type == "maintenance"]
        )
        mode = values.get("analysis_mode", "historical")
        phases = []
        diagnostic_phases = []
        relative_endpoints = []
        visual_parameters = {}
        phase_seconds = 0.0
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
            phase_started = time.perf_counter()
            analysis = self.phase_engine.analyse(bundle["samples"], mode=mode, what_if=what_if,
                                                 window_to=timestamp_to)
            phase_seconds = time.perf_counter() - phase_started
            phases = analysis["visual_intervals"]
            diagnostic_phases = analysis["diagnostic_intervals"]
            relative_endpoints = analysis.get("relative_endpoints", [])
            visual_parameters = analysis["visual_parameters"]
        response = {
            "series": [{**{key: value for key, value in item.items() if key != "raw_points"},
                        "module_number": module_number} for item in bundle["series"]]
                      if combined else {
                          **{key: value for key, value in bundle["series"][0].items()
                             if key != "raw_points"},
                          "module_number": module_number,
                      },
            "overlays": [marker.to_dict() for marker in markers],
            "window": {"from": timestamp_from, "to": timestamp_to, "inclusive": True},
            "semantics": {"overlay_timestamp": "occurred_at", "correlation_only": True,
                          "evidence_levels": ["observation", "correlation", "hypothesis",
                                              "direct_evidence", "confirmed_cause"],
                          "relative_endpoints": "observation_only",
                          "bms_limit_requires_direct_evidence": True},
            "soc_timeline": {
                "module_series": bundle.get("soc_module_series", []),
                "hycube_series": hycube if hycube and hycube["points"] else None,
                "policy_series": [], "policy_evidence": "unavailable",
                "policy_evidence_reason": "no_verified_read_only_source",
                "battery_capacity_semantics": "separate_hycube_system_value",
                "aggregation_rule": "not_verified", "causality": "not_determined",
            } if "soc" in metrics else None,
            "phase_analysis": {"mode": mode, "intervals": phases,
                               "diagnostic_intervals": diagnostic_phases,
                               "relative_endpoints": relative_endpoints,
                               "maintenance_boundaries": maintenance_boundaries,
                               "visual_parameters": visual_parameters,
                               "raw_measurements_unchanged": True,
                               "diagnostic_phase_unchanged": True},
        }
        response["series"] = ([{**item, "module_number": module_number} for item in projected_series]
                              if combined else {**projected_series[0], "module_number": module_number})
        response["performance"] = {
            "raw_records": bundle["raw_records"] + (hycube["raw_records"] if hycube else 0),
            "raw_points": sum(item.get("raw_points", len(item["points"])) for item in projected_series),
            "display_points": sum(len(item["points"]) for item in projected_series),
            "history_read_seconds": round(bundle["read_seconds"] + (hycube["read_seconds"] if hycube else 0), 6),
            "downsample_seconds": round(bundle["downsample_seconds"] + (hycube["downsample_seconds"] if hycube else 0), 6),
            "phase_projection_seconds": round(phase_seconds, 6), "cache_hit": bundle["cache_hit"],
            "backend_seconds": round(time.perf_counter() - backend_started, 6),
        }
        response["performance"]["payload_bytes"] = len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode())
        return ApiResponse(200, response)

    @staticmethod
    def _metrics(value: str | None) -> tuple[str, ...]:
        parts = tuple(part.strip() for part in (value or "").split(","))
        if not parts or any(not part for part in parts):
            raise HistoryApiProblem("metrics must contain comma-separated metric names")
        if len(set(parts)) != len(parts):
            raise HistoryApiProblem("metrics must be unique")
        if any(metric not in SERIES_METRICS | RS485_SERIES_METRICS for metric in parts):
            raise HistoryApiProblem("metric is unsupported")
        return parts

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

    @classmethod
    def _integers(cls, value: str | None, field: str, minimum: int,
                  maximum: int) -> tuple[int, ...] | None:
        if value is None:
            return None
        parts = value.split(",")
        if not parts or any(not part.strip() for part in parts):
            raise HistoryApiProblem(f"{field} must contain comma-separated integers")
        values = tuple(sorted({cls._integer(part.strip(), field, minimum, maximum)
                               for part in parts}))
        return values

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
