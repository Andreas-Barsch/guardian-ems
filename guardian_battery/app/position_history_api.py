"""Ingress API for documented physical-module position history."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from maintenance_api import ApiResponse, error_json
from position_history import (PositionHistoryConflictError, PositionHistoryError,
                              PositionHistoryValidationError, current_presence)

POSITION_HISTORY_API_ROUTE = "/api/position-history"


class PositionHistoryApi:
    def __init__(self, service, module_count_provider=None):
        self.service = service
        self.module_count_provider = module_count_provider

    def handle(self, method: str, target: str, headers=None, body: bytes = b"") -> ApiResponse:
        try:
            split = urlsplit(target)
            marker = split.path.find(POSITION_HISTORY_API_ROUTE)
            if marker < 0:
                return ApiResponse(404, error_json("not_found", "Position history route not found"))
            suffix = split.path[marker + len(POSITION_HISTORY_API_ROUTE):].strip("/")
            if method == "GET":
                return self._get(suffix, split.query)
            if method == "POST" and not suffix:
                return self._post(headers or {}, body)
            return ApiResponse(405, error_json("method_not_allowed", "Method not allowed"))
        except PositionHistoryValidationError as exc:
            return ApiResponse(400, error_json("validation_error", str(exc)))
        except PositionHistoryConflictError as exc:
            return ApiResponse(409, error_json("conflict", str(exc), {"expected": exc.expected, "actual": exc.actual}))
        except PositionHistoryError:
            return ApiResponse(503, error_json("history_error", "Position history is unavailable"))
        except Exception:
            return ApiResponse(500, error_json("internal_error", "Internal server error"))

    def _get(self, suffix: str, query: str) -> ApiResponse:
        raw = parse_qs(query, keep_blank_values=True)
        values = {key: item[0] for key, item in raw.items() if len(item) == 1}
        if len(values) != len(raw):
            raise PositionHistoryValidationError("query parameters must occur once")
        if not suffix:
            return ApiResponse(200, {"snapshots": [item.to_dict() for item in self.service.list()],
                                     "serial_histories": self.service.serial_histories()})
        if suffix == "current":
            item = self.service.current()
            module_count = (int(self.module_count_provider())
                            if self.module_count_provider else None)
            presence = current_presence(expected_module_count=module_count)
            observed = {str(position): value["observed_serial"]
                        for position, value in presence.items()
                        if value["status"] == "present"}
            return ApiResponse(200, {"snapshot": item.to_dict() if item else None,
                                     "documented": self.service.last_documented_serials(),
                                     "observed": observed,
                                     "presence": {str(k): v for k, v in presence.items()},
                                     "expected_module_count": sum(
                                         1 for value in presence.values() if value["expected"]),
                                     "divergence": self.service.divergence(observed)})
        if suffix == "resolve":
            timestamp = values.get("at")
            if not timestamp:
                raise PositionHistoryValidationError("at is required")
            if "module_number" in values and "serial" not in values:
                serial = self.service.position_at(int(values["module_number"]), timestamp)
                return ApiResponse(200, {"at": timestamp, "module_number": int(values["module_number"]), "serial": serial})
            if "serial" in values and "module_number" not in values:
                position = self.service.serial_at(values["serial"], timestamp)
                return ApiResponse(200, {"at": timestamp, "serial": values["serial"], "module_number": position})
            raise PositionHistoryValidationError("provide exactly one of module_number or serial")
        if suffix == "known-serials":
            position = int(values.get("module_number", "0"))
            at = values.get("at")
            effective = self.service.position_at(position, at) if at else None
            return ApiResponse(200, {"module_number": position, "at": at,
                                     "effective_serial": effective,
                                     "known_serials": self.service.known_serials(position),
                                     "serial_options": self.service.serial_options(position, at) if at else []})
        return ApiResponse(404, error_json("not_found", "Position history route not found"))

    def _post(self, headers, body: bytes) -> ApiResponse:
        content_type = next((str(value) for key, value in headers.items() if str(key).lower() == "content-type"), "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise PositionHistoryValidationError("Content-Type must be application/json")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PositionHistoryValidationError("Malformed JSON request body") from exc
        if not isinstance(payload, dict) or set(payload) != {"effective_at", "maintenance_event_id", "positions", "expected_latest_snapshot_id"}:
            raise PositionHistoryValidationError("request requires effective_at, maintenance_event_id, positions and expected_latest_snapshot_id")
        item = self.service.record(**payload)
        return ApiResponse(201, {"snapshot": item.to_dict()}, {"Location": f"{POSITION_HISTORY_API_ROUTE}/{item.position_history_id}"})
