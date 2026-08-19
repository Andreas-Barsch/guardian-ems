"""REST-style, handler-independent API for Guardian maintenance events."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from maintenance import (
    MaintenanceEvent,
    MaintenanceValidationError,
    normalize_utc_timestamp,
    validate_maintenance_category,
)
from maintenance_service import (
    MaintenanceArchivedError,
    MaintenanceConflictError,
    MaintenanceHistoryError,
    MaintenanceNotArchivedError,
    MaintenanceNotFoundError,
    MaintenanceService,
)


LOG = logging.getLogger("guardian_battery.maintenance_api")

API_ROUTE = "/api/maintenance/events"
MAX_REQUEST_BODY_BYTES = 64 * 1024
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

CREATE_FIELDS = frozenset(
    {
        "occurred_at",
        "ended_at",
        "category",
        "title",
        "description",
        "affected_system",
        "module_number",
        "module_serial",
        "cell_number",
        "action_taken",
        "previous_state",
        "result",
        "reason",
    }
)
REQUIRED_CREATE_FIELDS = frozenset(
    {"occurred_at", "category", "title", "affected_system"}
)
LIST_FILTERS = frozenset(
    {
        "active",
        "include_archived",
        "newest_first",
        "occurred_from",
        "occurred_to",
        "category",
        "module_number",
        "cell_number",
        "limit",
        "offset",
    }
)
EVENT_FIELDS = (
    "schema_version",
    "maintenance_event_id",
    "revision",
    "occurred_at",
    "created_at",
    "updated_at",
    "category",
    "title",
    "description",
    "affected_system",
    "module_number",
    "module_serial",
    "cell_number",
    "action_taken",
    "previous_state",
    "result",
    "reason",
    "source",
    "archived_at",
    "ended_at",
)


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.headers = dict(headers or {})
        super().__init__(message)


def event_json(event: MaintenanceEvent) -> dict[str, Any]:
    """Return the explicit canonical public representation."""

    result = {name: getattr(event, name) for name in EVENT_FIELDS}
    result["active"] = event.archived_at is None
    return result


def error_json(code: str, message: str, details: Mapping[str, Any] | None = None):
    return {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
        }
    }


class MaintenanceApi:
    def __init__(self, service: MaintenanceService, live_publisher=None):
        self.service = service
        self.live_publisher = live_publisher

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        try:
            return self._dispatch(method.upper(), target, headers or {}, body)
        except ApiProblem as exc:
            return ApiResponse(
                exc.status,
                error_json(exc.code, exc.message, exc.details),
                exc.headers,
            )
        except MaintenanceValidationError as exc:
            return ApiResponse(400, error_json("validation_error", str(exc)))
        except MaintenanceNotFoundError as exc:
            return ApiResponse(404, error_json("not_found", str(exc)))
        except MaintenanceConflictError as exc:
            return ApiResponse(
                409,
                error_json(
                    "conflict",
                    str(exc),
                    {
                        "maintenance_event_id": exc.event_id,
                        "expected_revision": exc.expected_revision,
                        "actual_revision": exc.actual_revision,
                    },
                ),
            )
        except (MaintenanceArchivedError, MaintenanceNotArchivedError) as exc:
            return ApiResponse(409, error_json("conflict", str(exc)))
        except MaintenanceHistoryError as exc:
            LOG.error("Maintenance history unavailable: %s", exc)
            return ApiResponse(503, error_json("history_error", "Maintenance history is unavailable"))
        except Exception:
            LOG.exception("Unhandled maintenance API error")
            return ApiResponse(500, error_json("internal_error", "Internal server error"))

    def _dispatch(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ApiResponse:
        split = urlsplit(target)
        marker_index = split.path.find(API_ROUTE)
        if marker_index < 0:
            raise ApiProblem(404, "not_found", "API route not found")
        suffix = split.path[marker_index + len(API_ROUTE) :]
        if suffix and not suffix.startswith("/"):
            raise ApiProblem(404, "not_found", "API route not found")
        parts = [unquote(part) for part in suffix.strip("/").split("/") if part]

        if not parts:
            if method == "GET":
                return self._list(split.query)
            if method == "POST":
                return self._create(headers, body)
            return self._method_not_allowed("GET, POST")

        event_id = parts[0]
        if len(parts) == 1:
            if method == "GET":
                return ApiResponse(200, {"event": event_json(self.service.get(event_id))})
            if method == "PATCH":
                return self._update(event_id, headers, body)
            return self._method_not_allowed("GET, PATCH")

        if len(parts) == 2 and parts[1] == "archive":
            if method != "POST":
                return self._method_not_allowed("POST")
            return self._archive(event_id, headers, body)
        if len(parts) == 2 and parts[1] == "restore":
            if method != "POST":
                return self._method_not_allowed("POST")
            return self._restore(event_id, headers, body)
        if len(parts) == 2 and parts[1] in {"activate", "deactivate"}:
            if method != "POST":
                return self._method_not_allowed("POST")
            return self._set_active(
                event_id, parts[1] == "activate", headers, body
            )
        if len(parts) == 2 and parts[1] == "history":
            if method != "GET":
                return self._method_not_allowed("GET")
            history = self.service.history(event_id)
            return ApiResponse(
                200,
                {
                    "maintenance_event_id": event_id,
                    "history": [event_json(item) for item in history],
                },
            )
        raise ApiProblem(404, "not_found", "API route not found")

    @staticmethod
    def _method_not_allowed(allow: str) -> ApiResponse:
        return ApiResponse(
            405,
            error_json("method_not_allowed", "Method not allowed"),
            {"Allow": allow},
        )

    @staticmethod
    def _json_body(headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise ApiProblem(
                413,
                "request_too_large",
                f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
            )
        normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
        content_type = normalized_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ApiProblem(400, "invalid_request", "Content-Type must be application/json")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiProblem(400, "invalid_request", "Malformed JSON request body") from exc
        if not isinstance(payload, dict):
            raise ApiProblem(400, "invalid_request", "JSON request body must be an object")
        return payload

    @staticmethod
    def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str]) -> None:
        unknown = set(payload) - allowed
        if unknown:
            raise ApiProblem(
                400,
                "invalid_request",
                "Unknown request fields",
                {"fields": sorted(unknown)},
            )

    @staticmethod
    def _expected_revision(payload: Mapping[str, Any]) -> int:
        if "expected_revision" not in payload:
            raise ApiProblem(400, "invalid_request", "expected_revision is required")
        value = payload["expected_revision"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ApiProblem(
                400,
                "invalid_request",
                "expected_revision must be a positive integer",
            )
        return value

    def _create(self, headers: Mapping[str, str], body: bytes) -> ApiResponse:
        payload = self._json_body(headers, body)
        self._reject_unknown(payload, CREATE_FIELDS)
        missing = REQUIRED_CREATE_FIELDS - set(payload)
        if missing:
            raise ApiProblem(
                400,
                "invalid_request",
                "Missing required request fields",
                {"fields": sorted(missing)},
            )
        event = self.service.create(**payload, source={"kind": "manual"})
        if self.live_publisher is not None:
            try:
                self.live_publisher.publish_if_live(event)
            except Exception:
                LOG.exception(
                    "Maintenance event persisted but MQTT live publish failed: %s",
                    event.maintenance_event_id,
                )
        return ApiResponse(201, {"event": event_json(event)})

    def _update(
        self,
        event_id: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ApiResponse:
        payload = self._json_body(headers, body)
        self._reject_unknown(payload, frozenset({"expected_revision", "changes"}))
        expected_revision = self._expected_revision(payload)
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            raise ApiProblem(400, "invalid_request", "changes must be an object")
        event = self.service.update(
            event_id,
            expected_revision=expected_revision,
            changes=changes,
        )
        return ApiResponse(200, {"event": event_json(event)})

    def _archive(
        self,
        event_id: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ApiResponse:
        payload = self._json_body(headers, body)
        self._reject_unknown(payload, frozenset({"expected_revision"}))
        event = self.service.archive(
            event_id,
            expected_revision=self._expected_revision(payload),
        )
        return ApiResponse(200, {"event": event_json(event)})

    def _restore(
        self,
        event_id: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ApiResponse:
        payload = self._json_body(headers, body)
        self._reject_unknown(payload, frozenset({"expected_revision"}))
        event = self.service.restore(
            event_id,
            expected_revision=self._expected_revision(payload),
        )
        return ApiResponse(200, {"event": event_json(event)})

    def _set_active(
        self,
        event_id: str,
        active: bool,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ApiResponse:
        payload = self._json_body(headers, body)
        self._reject_unknown(payload, frozenset({"expected_revision"}))
        event = self.service.set_active(
            event_id,
            expected_revision=self._expected_revision(payload),
            active=active,
        )
        return ApiResponse(200, {"event": event_json(event)})

    def _list(self, query: str) -> ApiResponse:
        raw = parse_qs(query, keep_blank_values=True)
        unknown = set(raw) - LIST_FILTERS
        if unknown:
            raise ApiProblem(
                400,
                "invalid_request",
                "Unknown query parameters",
                {"fields": sorted(unknown)},
            )
        for key, values in raw.items():
            if len(values) != 1:
                raise ApiProblem(400, "invalid_request", f"{key} must occur once")
        values = {key: items[0] for key, items in raw.items()}
        active_filter = values.get("active")
        if active_filter not in (None, "true", "false", "all"):
            raise ApiProblem(400, "invalid_request", "active must be true, false or all")
        include_archived = self._boolean(values.get("include_archived", "false"), "include_archived")
        if active_filter in {"false", "all"}:
            include_archived = True
        newest_first = self._boolean(values.get("newest_first", "true"), "newest_first")
        occurred_from = self._timestamp(values.get("occurred_from"), "occurred_from")
        occurred_to = self._timestamp(values.get("occurred_to"), "occurred_to")
        if occurred_from and occurred_to and occurred_from > occurred_to:
            raise ApiProblem(400, "invalid_request", "occurred_from must not exceed occurred_to")
        category = None
        if "category" in values:
            category = validate_maintenance_category(values["category"])
        module_number = self._bounded_integer(values.get("module_number"), "module_number", 1, 6)
        cell_number = self._bounded_integer(values.get("cell_number"), "cell_number", 1, 15)
        limit = self._bounded_integer(
            values.get("limit", str(DEFAULT_PAGE_LIMIT)), "limit", 1, MAX_PAGE_LIMIT
        )
        offset = self._bounded_integer(values.get("offset", "0"), "offset", 0, 2**31 - 1)

        items = self.service.list(
            include_archived=include_archived,
            newest_first=newest_first,
        )
        filtered = [
            item
            for item in items
            if (occurred_from is None or item.occurred_at >= occurred_from)
            and (occurred_to is None or item.occurred_at <= occurred_to)
            and (category is None or item.category == category)
            and (module_number is None or item.module_number == module_number)
            and (cell_number is None or item.cell_number == cell_number)
            and (active_filter in (None, "all") or
                 (item.archived_at is None) == (active_filter == "true"))
        ]
        page = filtered[offset : offset + limit]
        return ApiResponse(
            200,
            {
                "events": [event_json(item) for item in page],
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "returned": len(page),
                    "total": len(filtered),
                },
                "sorting": {
                    "field": "occurred_at",
                    "newest_first": newest_first,
                    "tie_breaker": "maintenance_event_id",
                },
            },
        )

    @staticmethod
    def _boolean(value: str, field: str) -> bool:
        if value == "true":
            return True
        if value == "false":
            return False
        raise ApiProblem(400, "invalid_request", f"{field} must be true or false")

    @staticmethod
    def _timestamp(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        try:
            return normalize_utc_timestamp(value, field)
        except MaintenanceValidationError as exc:
            raise ApiProblem(400, "invalid_request", str(exc)) from exc

    @staticmethod
    def _bounded_integer(
        value: str | None,
        field: str,
        minimum: int,
        maximum: int,
    ) -> int | None:
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ApiProblem(400, "invalid_request", f"{field} must be an integer") from exc
        if str(parsed) != value and not (value == "0" and parsed == 0):
            raise ApiProblem(400, "invalid_request", f"{field} must be an integer")
        if not minimum <= parsed <= maximum:
            raise ApiProblem(
                400,
                "invalid_request",
                f"{field} must be between {minimum} and {maximum}",
            )
        return parsed
