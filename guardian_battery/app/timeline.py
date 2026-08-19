"""Read-only projection of Guardian maintenance and technical event histories."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from maintenance_service import MaintenanceService
from maintenance_ui import maintenance_deep_link


DEFAULT_TECHNICAL_EVENT_FILE = Path("/share/guardian_battery/events.jsonl")
TECHNICAL_EVENT_TYPES = frozenset({"alarm_started", "alarm_cleared", "status_changed"})
TIMELINE_EVENT_TYPES = frozenset({"maintenance", *TECHNICAL_EVENT_TYPES})


class TimelineError(RuntimeError):
    """Base class for controlled timeline failures."""


class TechnicalHistoryError(TimelineError):
    """Raised when events.jsonl cannot be projected reliably."""


@dataclass(frozen=True)
class TimelineEvent:
    event_type: str
    timestamp: str
    title: str
    summary: str
    source: str
    projection_key: str
    deep_link: str | None = None
    maintenance_event_id: str | None = None
    module_number: int | None = None
    cell_number: int | None = None
    severity: str | None = None
    status: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unix_timestamp(value: Any, line_number: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TechnicalHistoryError(f"technical event line {line_number}: timestamp must be numeric")
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise TechnicalHistoryError(
            f"technical event line {line_number}: timestamp is out of range"
        ) from exc


class TechnicalEventSource:
    """Strict reader for the schema emitted by main.update_events()."""

    def __init__(self, path: Path | str = DEFAULT_TECHNICAL_EVENT_FILE):
        self.path = Path(path)

    def read(self) -> list[TimelineEvent]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TechnicalHistoryError("technical event history is unavailable") from exc
        events: list[TimelineEvent] = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TechnicalHistoryError(
                    f"technical event line {line_number}: malformed JSON"
                ) from exc
            events.append(self._project(raw, line_number))
        return events

    @staticmethod
    def _project(raw: Any, line_number: int) -> TimelineEvent:
        if not isinstance(raw, dict):
            raise TechnicalHistoryError(f"technical event line {line_number}: object required")
        kind = raw.get("type")
        if kind not in TECHNICAL_EVENT_TYPES:
            raise TechnicalHistoryError(f"technical event line {line_number}: unsupported type")
        timestamp = _unix_timestamp(raw.get("timestamp"), line_number)
        key = f"technical:{line_number}"
        if kind == "alarm_started":
            alarm = raw.get("alarm")
            if not isinstance(alarm, dict):
                raise TechnicalHistoryError(
                    f"technical event line {line_number}: alarm object required"
                )
            code, message = alarm.get("code"), alarm.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                raise TechnicalHistoryError(
                    f"technical event line {line_number}: alarm code and message required"
                )
            module = alarm.get("module")
            if module is not None and (isinstance(module, bool) or not isinstance(module, int)):
                raise TechnicalHistoryError(
                    f"technical event line {line_number}: alarm module must be an integer"
                )
            return TimelineEvent(
                event_type=kind, timestamp=timestamp, title="Alarm begonnen",
                summary=message, source="guardian_events", projection_key=key,
                module_number=module, severity=alarm.get("level"), status=raw.get("status"),
                metadata={"alarm_code": code},
            )
        if kind == "alarm_cleared":
            code = raw.get("code")
            if not isinstance(code, str):
                raise TechnicalHistoryError(
                    f"technical event line {line_number}: cleared alarm code required"
                )
            module = None
            prefix = code.split(":", 1)[0]
            if prefix.isdigit():
                module = int(prefix)
            return TimelineEvent(
                event_type=kind, timestamp=timestamp, title="Alarm beendet",
                summary=code, source="guardian_events", projection_key=key,
                module_number=module, status="cleared", metadata={"alarm_code": code},
            )
        before, after = raw.get("from"), raw.get("to")
        if before is not None and not isinstance(before, str):
            raise TechnicalHistoryError(f"technical event line {line_number}: from must be text or null")
        if not isinstance(after, str):
            raise TechnicalHistoryError(f"technical event line {line_number}: to must be text")
        return TimelineEvent(
            event_type=kind, timestamp=timestamp, title="Status geändert",
            summary=f"{before or 'unbekannt'} → {after}", source="guardian_events",
            projection_key=key, status=after, metadata={"from": before, "to": after},
        )


class TimelineService:
    def __init__(self, maintenance: MaintenanceService, technical: TechnicalEventSource):
        self.maintenance = maintenance
        self.technical = technical

    def query(
        self, *, timestamp_from: str | None = None, timestamp_to: str | None = None,
        event_types: Iterable[str] | None = None, category: str | None = None,
        module_number: int | None = None, cell_number: int | None = None,
        include_archived: bool = False,
    ) -> list[TimelineEvent]:
        selected = set(event_types or TIMELINE_EVENT_TYPES)
        events: list[TimelineEvent] = []
        if "maintenance" in selected:
            for item in self.maintenance.list(
                include_archived=include_archived, newest_first=False
            ):
                events.append(TimelineEvent(
                    event_type="maintenance", timestamp=item.occurred_at,
                    title=item.title, summary=item.description or item.action_taken or "",
                    source="maintenance_events", projection_key=f"maintenance:{item.maintenance_event_id}",
                    deep_link=maintenance_deep_link(item.maintenance_event_id),
                    maintenance_event_id=item.maintenance_event_id,
                    module_number=item.module_number, cell_number=item.cell_number,
                    status="archived" if item.archived_at else "active",
                    metadata={"category": item.category, "created_at": item.created_at,
                              "updated_at": item.updated_at, "archived_at": item.archived_at,
                              "revision": item.revision},
                ))
        if selected & TECHNICAL_EVENT_TYPES:
            events.extend(event for event in self.technical.read() if event.event_type in selected)
        filtered = [event for event in events if
                    (timestamp_from is None or event.timestamp >= timestamp_from) and
                    (timestamp_to is None or event.timestamp <= timestamp_to) and
                    (module_number is None or event.module_number == module_number) and
                    (cell_number is None or event.cell_number == cell_number) and
                    (category is None or (event.event_type == "maintenance" and
                     event.metadata.get("category") == category))]
        return sorted(filtered, key=lambda event: (event.timestamp, event.event_type, event.projection_key))
