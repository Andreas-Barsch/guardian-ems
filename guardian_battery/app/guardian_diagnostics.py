"""Read-only projection of persisted Guardian Daily Diagnostics derived data."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from maintenance_api import ApiResponse, error_json


LOG = logging.getLogger("guardian_battery.diagnostics")
API_ROUTE = "/api/diagnostics"
SCHEMA_VERSION = 1
MAX_OVERVIEW_DAYS = 30
MAX_EVENT_RESPONSE = 500
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class DiagnosticsError(Exception):
    pass


class InvalidDiagnosticDate(DiagnosticsError):
    pass


class DiagnosticsNotFound(DiagnosticsError):
    pass


class DiagnosticsCorrupt(DiagnosticsError):
    pass


def validate_diagnostic_date(value: str) -> str:
    """Accept only one canonical ISO calendar date, never a path fragment."""
    if not _DATE.fullmatch(value):
        raise InvalidDiagnosticDate("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidDiagnosticDate("date is not a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise InvalidDiagnosticDate("date must be canonical YYYY-MM-DD")
    return value


def _contained_file(path: Path, parent: Path) -> bool:
    """Reject symlinks and resolved files outside their fixed directory."""
    try:
        return (not path.is_symlink() and not parent.is_symlink()
                and path.is_file() and path.resolve().parent == parent.resolve())
    except OSError:
        return False


def read_validated_daily_result(output_root: Path | str, diagnostic_date: str
                                ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read an index and its immutable revision using the worker's full contract."""
    text = validate_diagnostic_date(diagnostic_date)
    day_dir = Path(output_root) / "daily" / text
    index_path = day_dir / "index.json"
    if not _contained_file(index_path, day_dir):
        raise DiagnosticsNotFound("daily result is not available")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(index, dict):
            raise ValueError("index is not an object")
        result_name = index["result"]
        if (not isinstance(result_name, str) or Path(result_name).name != result_name
                or result_name == "index.json" or not result_name.endswith(".json")):
            raise ValueError("unsafe or incomplete index")
        if (index.get("schema_version") != SCHEMA_VERSION
                or index.get("diagnostic_date") != text
                or not index.get("daily_result_id")
                or not index.get("input_fingerprint")
                or index.get("overall_status") not in {"complete", "partial"}):
            raise ValueError("invalid index contract")
        result_path = day_dir / result_name
        if not _contained_file(result_path, day_dir):
            raise ValueError("result revision is missing")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (not isinstance(result, dict)
                or result.get("schema_version") != SCHEMA_VERSION
                or result.get("diagnostic_date") != text
                or result.get("daily_result_id") != index["daily_result_id"]
                or result.get("input_fingerprint") != index["input_fingerprint"]
                or result.get("overall_status") not in {"complete", "partial"}
                or result.get("overall_status") != index["overall_status"]):
            raise ValueError("index and result revision are inconsistent")
        return index, result
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DiagnosticsCorrupt("daily result is inconsistent") from exc


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _quality(result: Mapping[str, Any]) -> dict[str, Any]:
    status = result.get("overall_status")
    component = result.get("components", {}).get("bms_management", {})
    quality = component.get("quality", {}) if isinstance(component, dict) else {}
    reasons = (quality.get("reasons", []) if isinstance(quality, dict)
               else component.get("warnings", []) if isinstance(component, dict) else [])
    return {
        "level": quality.get("level") if isinstance(quality, dict) else quality or status,
        "overall_status": status,
        "label": ("Vollständig analysiert" if status == "complete" else
                  "Datenlage teilweise" if status == "partial" else
                  "Analyse nicht verfügbar"),
        "reasons": [str(item) for item in reasons if item],
        "interpretation_limited": status != "complete",
    }


def _public_sources(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep quality/count provenance while withholding physical source paths."""
    public = {}
    for name, value in result.get("sources", {}).items():
        if not isinstance(value, dict):
            continue
        public[name] = {key: item for key, item in value.items() if key != "files"}
    return public


class GuardianDiagnosticsRepository:
    """Short-lived, read-only access to one diagnostics output root."""

    def __init__(self, output_root: Path | str):
        self.output_root = Path(output_root)
        self._warned: set[str] = set()

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            LOG.warning("Guardian Diagnostics %s", message)
            self._warned.add(key)

    def available(self, limit: int | None = None
                  ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[str]]:
        daily = self.output_root / "daily"
        if not daily.is_dir():
            return [], []
        valid, corrupt = [], []
        children = sorted((child for child in daily.iterdir()
                           if not child.is_symlink() and child.is_dir()
                           and _DATE.fullmatch(child.name)),
                          key=lambda child: child.name, reverse=True)
        for child in children[:limit]:
            try:
                valid.append(read_validated_daily_result(self.output_root, child.name))
            except DiagnosticsNotFound:
                continue
            except (InvalidDiagnosticDate, DiagnosticsCorrupt):
                corrupt.append(child.name)
                self._warn_once(child.name, f"derived result unavailable date={child.name}")
        valid.sort(key=lambda pair: pair[1]["diagnostic_date"], reverse=True)
        corrupt.sort(reverse=True)
        return valid, corrupt

    def result(self, diagnostic_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return read_validated_daily_result(self.output_root, diagnostic_date)

    def aggregates(self, diagnostic_date: str, *, required: bool = False) -> list[dict[str, Any]]:
        text = validate_diagnostic_date(diagnostic_date)
        path = self.output_root / "aggregates" / "bms_management" / f"{text}.json"
        if not _contained_file(path, path.parent):
            if required:
                raise DiagnosticsNotFound("BMS management aggregate is not available")
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload["aggregates"]
            if payload.get("schema_version") != 1 or not isinstance(rows, list):
                raise ValueError("invalid aggregate contract")
            if any(not isinstance(row, dict) or row.get("day") != text for row in rows):
                raise ValueError("aggregate date mismatch")
            return rows
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DiagnosticsCorrupt("BMS management aggregate is inconsistent") from exc

    def events(self, diagnostic_date: str, *, required: bool = False) -> dict[str, Any]:
        text = validate_diagnostic_date(diagnostic_date)
        path = self.output_root / "events" / "bms_management" / f"{text}.jsonl"
        if not _contained_file(path, path.parent):
            if required:
                raise DiagnosticsNotFound("BMS management events are not available")
            return {"events": [], "event_count": 0, "truncated": False}
        events = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("event is not an object")
                    events.append(item)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DiagnosticsCorrupt("BMS management event store is inconsistent") from exc
        public = [self._without_raw(item) for item in events[:MAX_EVENT_RESPONSE]]
        return {"events": public, "event_count": len(events),
                "truncated": len(events) > MAX_EVENT_RESPONSE}

    def cell_risk(self, diagnostic_date: str, *, required: bool = False) -> dict[str, Any]:
        text = validate_diagnostic_date(diagnostic_date)
        path = self.output_root / "aggregates" / "cell_risk" / f"{text}.json"
        if not _contained_file(path, path.parent):
            if required:
                raise DiagnosticsNotFound("Cell Risk aggregate is not available")
            return {"schema_version": 1, "diagnostic_date": text, "cells": [], "top10": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                    or payload.get("diagnostic_date") != text
                    or not isinstance(payload.get("cells"), list)):
                raise ValueError("invalid Cell Risk aggregate")
            return payload
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DiagnosticsCorrupt("Cell Risk aggregate is inconsistent") from exc

    def cell_risk_detail(self, diagnostic_date: str, serial: str,
                         cell_number: int) -> dict[str, Any]:
        current = self.cell_risk(diagnostic_date, required=True)
        selected = next((row for row in current["cells"]
                         if row.get("physical_serial") == serial
                         and row.get("cell_number") == cell_number), None)
        if selected is None:
            raise DiagnosticsNotFound("Cell Risk cell is not available")
        history = []
        aggregate_root = self.output_root / "aggregates" / "cell_risk"
        if aggregate_root.is_dir():
            for path in sorted(aggregate_root.glob("*.json")):
                if not _DATE.fullmatch(path.stem) or path.stem > diagnostic_date:
                    continue
                try:
                    payload = self.cell_risk(path.stem)
                except DiagnosticsError:
                    continue
                row = next((item for item in payload["cells"]
                            if item.get("physical_serial") == serial
                            and item.get("cell_number") == cell_number), None)
                if row:
                    history.append({"date": path.stem, "risk_score_v2": row["risk_score_v2"],
                                    "risk_class": row["risk_class"]})
        return {"schema_version": 1, "date": diagnostic_date, "cell": selected,
                "risk_history": history[-90:], "causality": "not_determined"}

    @classmethod
    def _without_raw(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._without_raw(item) for key, item in value.items()
                    if key not in {"raw_frame", "frame_raw", "info_raw", "serial_raw", "path"}}
        if isinstance(value, list):
            return [cls._without_raw(item) for item in value]
        return value

    def day_summary(self, result: Mapping[str, Any], aggregates: list[Mapping[str, Any]]
                    ) -> dict[str, Any]:
        aggregate_event_count = sum(int(row.get("ccl_reduction_event_count", 0) or 0)
                                    + int(row.get("dcl_reduction_event_count", 0) or 0)
                                    for row in aggregates)
        component_count = result.get("components", {}).get(
            "bms_management", {}).get("events", {}).get("count")
        event_count = component_count if isinstance(component_count, int) else aggregate_event_count
        consistent = component_count is None or component_count == aggregate_event_count
        serials = sorted({str(row["physical_serial"]) for row in aggregates
                          if row.get("physical_serial")})
        observations = []
        if result.get("overall_status") == "partial":
            observations.append({"kind": "data_quality", "text":
                                 "Datenlage teilweise – Interpretation eingeschränkt."})
        if not consistent:
            observations.append({"kind": "derived_data_inconsistent", "text":
                                 "Daily Result und BMS-Aggregat melden unterschiedliche Ereigniszahlen."})
        observations.append({
            "kind": "management_events",
            "text": (f"{event_count} BMS-Management-Ereignisse erkannt."
                     if event_count else "Keine besonderen BMS-Management-Ereignisse erkannt."),
        })
        for row in aggregates:
            serial = str(row.get("physical_serial", "Seriennummer unbekannt"))
            dcl_zero = int(row.get("dcl_zero_event_count", row.get("dcl_zero_count", 0)) or 0)
            if dcl_zero:
                observations.append({"kind": "dcl_zero", "physical_serial": serial,
                    "text": f"{serial}: {dcl_zero} Entladebegrenzungen auf DCL 0 A erkannt."})
            lowest = row.get("dominant_lowest_cell")
            ratio = _number(row.get("dominant_lowest_ratio"))
            if lowest and ratio is not None:
                observations.append({"kind": "lowest_cell", "physical_serial": serial,
                    "text": f"{serial}: Zelle {lowest} war bei {ratio:.0%} der auswertbaren Ereignisse die niedrigste Zelle."})
        return {"event_count": event_count, "affected_serials": serials,
                "important_observations": observations,
                "data_consistency": {"consistent": consistent,
                                     "component_event_count": component_count,
                                     "aggregate_event_count": aggregate_event_count},
                "causality": "not_determined",
                "recommendation": ("Datenlage teilweise – Interpretation eingeschränkt."
                                   if result.get("overall_status") == "partial" else
                                   "Beobachtung fortsetzen."),
                "trend": "Trend noch nicht bestimmbar."}

    def day_dto(self, diagnostic_date: str) -> dict[str, Any]:
        index, result = self.result(diagnostic_date)
        aggregates = self.aggregates(diagnostic_date)
        event_payload = self.events(diagnostic_date)
        positions: dict[str, set[int]] = {}
        for event in event_payload["events"]:
            serial, position = event.get("physical_serial"), event.get("position_at_time")
            if serial and isinstance(position, int):
                positions.setdefault(str(serial), set()).add(position)
        aggregates = [{**row, "positions_at_time": sorted(
            positions.get(str(row.get("physical_serial")), set()))}
            for row in aggregates]
        summary = self.day_summary(result, aggregates)
        return {
            "schema_version": SCHEMA_VERSION,
            "date": diagnostic_date,
            "timezone": result.get("timezone", "Europe/Berlin"),
            "overall_status": result["overall_status"],
            "quality": _quality(result),
            "sources": _public_sources(result),
            "components": result.get("components", {}),
            "summary": summary,
            "bms_management": {"aggregates": aggregates,
                               "event_count": summary["event_count"],
                               "event_details_available": event_payload["event_count"] > 0},
            "cell_risk": self.cell_risk(diagnostic_date),
            "provenance": {"daily_result_id": result["daily_result_id"],
                           "input_fingerprint": result["input_fingerprint"],
                           "index_updated_at": index.get("updated_at"),
                           "execution": result.get("execution"),
                           "component_manifest": result.get("provenance", {}).get("component_manifest")},
        }

    def days_dto(self) -> dict[str, Any]:
        valid, corrupt = self.available()
        days = []
        for _index, result in valid:
            try:
                aggregates = self.aggregates(result["diagnostic_date"])
            except DiagnosticsCorrupt:
                corrupt.append(result["diagnostic_date"])
                self._warn_once(result["diagnostic_date"] + ":aggregate",
                                f"aggregate unavailable date={result['diagnostic_date']}")
                continue
            summary = self.day_summary(result, aggregates)
            days.append({"date": result["diagnostic_date"],
                         "overall_status": result["overall_status"],
                         "quality": _quality(result),
                         "event_count": summary["event_count"],
                         "affected_serials": summary["affected_serials"]})
        return {"schema_version": SCHEMA_VERSION, "days": days,
                "unavailable_days": corrupt, "failed_history_available": False}

    def _window(self, rows: list[dict[str, Any]], latest: date, days: int) -> dict[str, Any]:
        floor = latest - timedelta(days=days - 1)
        selected = [row for row in rows if date.fromisoformat(row["date"]) >= floor]
        counts = {status: sum(row["overall_status"] == status for row in selected)
                  for status in ("complete", "partial", "failed")}
        serials = sorted({serial for row in selected for serial in row["affected_serials"]})
        return {"calendar_days": days, "available_days": len(selected),
                **{f"{key}_days": value for key, value in counts.items()},
                "management_event_count": sum(row["event_count"] for row in selected),
                "affected_serials": serials,
                "history_sufficient": len(selected) >= min(days, 3),
                "missing_days_are_not_zero_days": True}

    def overview(self, now: datetime | None = None) -> dict[str, Any]:
        valid, corrupt = self.available(limit=MAX_OVERVIEW_DAYS)
        rows = []
        for _index, result in valid:
            try:
                aggregates = self.aggregates(result["diagnostic_date"])
            except DiagnosticsCorrupt:
                corrupt.append(result["diagnostic_date"])
                continue
            summary = self.day_summary(result, aggregates)
            rows.append({"date": result["diagnostic_date"],
                         "overall_status": result["overall_status"],
                         "quality": _quality(result), "event_count": summary["event_count"],
                         "affected_serials": summary["affected_serials"]})
        generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        if not rows:
            empty = {"calendar_days": 7, "available_days": 0, "complete_days": 0,
                     "partial_days": 0, "failed_days": 0, "management_event_count": 0,
                     "affected_serials": [], "history_sufficient": False,
                     "missing_days_are_not_zero_days": True}
            return {"schema_version": SCHEMA_VERSION, "generated_at": generated,
                    "latest_date": None, "available_day_count": 0,
                    "windows": {"7d": empty, "30d": {**empty, "calendar_days": 30}},
                    "active_components": ["bms_management"],
                    "active_component_labels": ["BMS Management Evidence"],
                    "important_observations": [],
                    "data_quality": {"label": "Nicht genügend Historie", "status": "insufficient_history"},
                    "failed_history_available": False,
                    "unavailable_days": sorted(set(corrupt), reverse=True)}
        latest = date.fromisoformat(rows[0]["date"])
        latest_detail = self.day_dto(rows[0]["date"])
        return {"schema_version": SCHEMA_VERSION, "generated_at": generated,
                "latest_date": rows[0]["date"], "latest_status": rows[0]["overall_status"],
                "available_day_count": len(rows), "window_is_capped_at_days": 30,
                "windows": {"7d": self._window(rows, latest, 7),
                            "30d": self._window(rows, latest, 30)},
                "active_components": ["bms_management"],
                "active_component_labels": ["BMS Management Evidence"],
                "important_observations": latest_detail["summary"]["important_observations"],
                "data_quality": latest_detail["quality"],
                "failed_history_available": False,
                "unavailable_days": sorted(set(corrupt), reverse=True)}


class GuardianDiagnosticsApi:
    def __init__(self, repository: GuardianDiagnosticsRepository):
        self.repository = repository

    def handle(self, method: str, target: str) -> ApiResponse:
        if method != "GET":
            return ApiResponse(405, error_json("method_not_allowed", "Method not allowed"),
                               {"Allow": "GET"})
        try:
            path = unquote(urlsplit(target).path)
            marker = path.find(API_ROUTE)
            if marker < 0:
                raise DiagnosticsNotFound("Diagnostics API route not found")
            suffix = path[marker + len(API_ROUTE):].strip("/")
            parts = suffix.split("/") if suffix else []
            if parts == ["overview"]:
                body = self.repository.overview()
            elif parts == ["days"]:
                body = self.repository.days_dto()
            elif parts and parts[0] == "daily" and len(parts) != 2:
                raise InvalidDiagnosticDate("date must use YYYY-MM-DD")
            elif len(parts) == 2 and parts[0] == "daily":
                body = self.repository.day_dto(parts[1])
            elif (len(parts) != 3 and len(parts) >= 2
                  and parts[:2] in (["bms-management", "aggregate"],
                                    ["bms-management", "events"])):
                raise InvalidDiagnosticDate("date must use YYYY-MM-DD")
            elif len(parts) == 3 and parts[:2] == ["bms-management", "aggregate"]:
                body = {"schema_version": 1, "date": parts[2],
                        "aggregates": self.repository.aggregates(parts[2], required=True)}
            elif len(parts) == 3 and parts[:2] == ["bms-management", "events"]:
                body = {"schema_version": 1, "date": parts[2],
                        **self.repository.events(parts[2], required=True)}
            elif len(parts) == 3 and parts[:2] == ["cell-risk", "top10"]:
                result = self.repository.cell_risk(parts[2], required=True)
                body = {"schema_version": 1, "date": parts[2],
                        "cell_risk_algorithm_version": result.get("cell_risk_algorithm_version"),
                        "cells": result.get("top10", result.get("cells", [])[:10])}
            elif len(parts) == 5 and parts[:2] == ["cell-risk", "cell"]:
                try:
                    number = int(parts[4])
                except ValueError as exc:
                    raise DiagnosticsNotFound("Cell Risk cell is not available") from exc
                body = self.repository.cell_risk_detail(parts[2], parts[3], number)
            else:
                raise DiagnosticsNotFound("Diagnostics API route not found")
            return ApiResponse(200, body)
        except InvalidDiagnosticDate as exc:
            return ApiResponse(400, error_json("invalid_date", str(exc)))
        except DiagnosticsNotFound as exc:
            return ApiResponse(404, error_json("not_found", str(exc)))
        except DiagnosticsCorrupt:
            LOG.warning("Guardian Diagnostics derived data is inconsistent")
            return ApiResponse(503, error_json("derived_data_inconsistent",
                                               "Diagnostics derived data is unavailable"))
        except Exception:
            LOG.exception("Unhandled Guardian Diagnostics API error")
            return ApiResponse(500, error_json("internal_error", "Internal server error"))
