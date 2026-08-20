"""HTTP adapter for the read-only Guardian timeline projection."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlsplit

from maintenance import MaintenanceValidationError, normalize_utc_timestamp, validate_maintenance_category
from maintenance_api import ApiResponse, error_json
from maintenance_service import MaintenanceHistoryError
from timeline import TIMELINE_EVENT_TYPES, TechnicalHistoryError, TimelineService


LOG = logging.getLogger(__name__)
TIMELINE_API_ROUTE = "/api/timeline"
TIMELINE_FILTERS = frozenset(
    {"from", "to", "event_type", "category", "module_number", "cell_number", "include_archived"}
)


class TimelineApiProblem(ValueError):
    pass


class TimelineApi:
    def __init__(self, service: TimelineService):
        self.service = service

    def handle(self, method: str, target: str) -> ApiResponse:
        try:
            if method != "GET":
                return ApiResponse(405, error_json("method_not_allowed", "Method not allowed"), {"Allow": "GET"})
            split = urlsplit(target)
            marker = split.path.find(TIMELINE_API_ROUTE)
            if marker < 0 or split.path[marker + len(TIMELINE_API_ROUTE):].strip("/"):
                return ApiResponse(404, error_json("not_found", "Timeline API route not found"))
            return self._query(split.query)
        except TimelineApiProblem as exc:
            return ApiResponse(400, error_json("invalid_request", str(exc)))
        except MaintenanceHistoryError as exc:
            LOG.error("Maintenance timeline source unavailable: %s", exc)
            return ApiResponse(503, error_json("maintenance_history_error", "Maintenance history is unavailable"))
        except TechnicalHistoryError as exc:
            LOG.error("Technical timeline source unavailable: %s", exc)
            return ApiResponse(503, error_json("technical_history_error", "Technical event history is unavailable"))
        except Exception:
            LOG.exception("Unhandled timeline API error")
            return ApiResponse(500, error_json("internal_error", "Internal server error"))

    def _query(self, query: str) -> ApiResponse:
        raw = parse_qs(query, keep_blank_values=True)
        unknown = set(raw) - TIMELINE_FILTERS
        if unknown:
            raise TimelineApiProblem(f"unknown query parameters: {', '.join(sorted(unknown))}")
        for key, values in raw.items():
            if len(values) != 1:
                raise TimelineApiProblem(f"{key} must occur once")
        values = {key: items[0] for key, items in raw.items()}
        timestamp_from = self._timestamp(values.get("from"), "from")
        timestamp_to = self._timestamp(values.get("to"), "to")
        if timestamp_from and timestamp_to and timestamp_from > timestamp_to:
            raise TimelineApiProblem("from must not exceed to")
        event_types = None
        if "event_type" in values:
            event_types = {value.strip() for value in values["event_type"].split(",") if value.strip()}
            if not event_types or event_types - TIMELINE_EVENT_TYPES:
                raise TimelineApiProblem("event_type contains an unsupported value")
        category = None
        if "category" in values:
            try:
                category = validate_maintenance_category(values["category"])
            except MaintenanceValidationError as exc:
                raise TimelineApiProblem(str(exc)) from exc
        module_number = self._integer(values.get("module_number"), "module_number", 1, 6)
        cell_number = self._integer(values.get("cell_number"), "cell_number", 1, 15)
        include_archived = self._boolean(values.get("include_archived", "false"))
        events = self.service.query(
            timestamp_from=timestamp_from, timestamp_to=timestamp_to,
            event_types=event_types, category=category, module_number=module_number,
            cell_number=cell_number, include_archived=include_archived,
        )
        return ApiResponse(200, {
            "events": [event.to_dict() for event in events],
            "window": {"from": timestamp_from, "to": timestamp_to, "inclusive": True},
            "sorting": {"direction": "newest_first", "field": "timestamp",
                        "tie_breaker": ["event_type", "projection_key"]},
            "filters": {"event_type": sorted(event_types) if event_types else None,
                        "category": category, "module_number": module_number,
                        "cell_number": cell_number, "include_archived": include_archived},
        })

    @staticmethod
    def _timestamp(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        try:
            return normalize_utc_timestamp(value, field)
        except MaintenanceValidationError as exc:
            raise TimelineApiProblem(str(exc)) from exc

    @staticmethod
    def _integer(value: str | None, field: str, minimum: int, maximum: int) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise TimelineApiProblem(f"{field} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise TimelineApiProblem(f"{field} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _boolean(value: str) -> bool:
        if value == "true":
            return True
        if value == "false":
            return False
        raise TimelineApiProblem("include_archived must be true or false")
