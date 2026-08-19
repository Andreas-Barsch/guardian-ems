"""Reusable adapter from the shared timeline projection to chart markers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from timeline import TimelineEvent, TimelineService


@dataclass(frozen=True)
class OverlayContext:
    timestamp_from: str
    timestamp_to: str
    module_number: int | None = None
    cell_number: int | None = None
    event_types: tuple[str, ...] = ("maintenance",)
    include_archived: bool = False


@dataclass(frozen=True)
class EventMarker:
    event_type: str
    timestamp: str
    position: float
    title: str
    summary: str
    deep_link: str | None
    maintenance_event_id: str | None
    module_number: int | None
    cell_number: int | None
    status: str | None
    metadata: dict

    def to_dict(self) -> dict:
        return asdict(self)


def matches_chart(event: TimelineEvent, *, module_number: int | None, cell_number: int | None) -> bool:
    """Apply one non-aggressive relevance rule for every Guardian chart.

    System charts include every event. Module charts include system-wide events
    and events for that module. Cell charts additionally exclude events for a
    different explicit cell while retaining system- and module-wide context.
    """

    if module_number is None:
        return True
    if event.module_number is not None and event.module_number != module_number:
        return False
    if cell_number is None:
        return True
    if event.cell_number is not None and event.cell_number != cell_number:
        return False
    return True


class EventOverlayAdapter:
    def __init__(self, timeline: TimelineService):
        self.timeline = timeline

    def markers(self, context: OverlayContext) -> list[EventMarker]:
        start = datetime.fromisoformat(context.timestamp_from)
        end = datetime.fromisoformat(context.timestamp_to)
        span = (end - start).total_seconds()
        if span < 0:
            raise ValueError("overlay window start must not exceed end")
        events = self.timeline.query(
            timestamp_from=context.timestamp_from,
            timestamp_to=context.timestamp_to,
            event_types=context.event_types,
            include_archived=context.include_archived,
        )
        result = []
        for event in events:
            if not matches_chart(event, module_number=context.module_number, cell_number=context.cell_number):
                continue
            elapsed = (datetime.fromisoformat(event.timestamp) - start).total_seconds()
            position = 0.0 if span == 0 else min(1.0, max(0.0, elapsed / span))
            result.append(EventMarker(
                event_type=event.event_type, timestamp=event.timestamp, position=position,
                title=event.title, summary=event.summary, deep_link=event.deep_link,
                maintenance_event_id=event.maintenance_event_id,
                module_number=event.module_number, cell_number=event.cell_number,
                status=event.status, metadata=dict(event.metadata),
            ))
        return result
