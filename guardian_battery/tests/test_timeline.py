import json
from datetime import datetime, timezone

import pytest

from maintenance import MaintenanceEventLog
from maintenance_service import MaintenanceRepository, MaintenanceService
from timeline import TechnicalEventSource, TechnicalHistoryError, TimelineService


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def build(tmp_path):
    maintenance = MaintenanceService(
        MaintenanceRepository(MaintenanceEventLog(tmp_path / "maintenance.jsonl")),
        clock=lambda: NOW,
    )
    technical_path = tmp_path / "events.jsonl"
    return maintenance, technical_path, TimelineService(
        maintenance, TechnicalEventSource(technical_path)
    )


def add(maintenance, **changes):
    fields = dict(occurred_at="2024-04-05T09:00:00+00:00", category="inspection",
                  title="Rückwirkende Prüfung", description="Kontakte geprüft",
                  affected_system="Stack", module_number=3, cell_number=7,
                  source={"kind": "manual"})
    fields.update(changes)
    return maintenance.create(**fields)


def write_technical(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_maintenance_uses_occurred_at_not_created_or_updated_and_only_once(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    created = add(maintenance)
    updated = maintenance.update(created.maintenance_event_id, expected_revision=1,
                                 changes={"title": "Aktuelle Projektion"})

    events = timeline.query()

    assert len(events) == 1
    assert events[0].timestamp == "2024-04-05T09:00:00+00:00"
    assert events[0].title == "Aktuelle Projektion"
    assert events[0].metadata["created_at"] == "2026-08-20T12:00:00+00:00"
    assert events[0].metadata["updated_at"] == updated.updated_at


def test_archived_default_filter_include_and_stable_deep_link(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    event = add(maintenance)
    maintenance.archive(event.maintenance_event_id, expected_revision=1)

    assert timeline.query() == []
    projected = timeline.query(include_archived=True)[0]
    assert projected.maintenance_event_id == event.maintenance_event_id
    assert projected.deep_link == f"maintenance?event_id={event.maintenance_event_id}"
    assert projected.status == "archived"


def test_real_technical_schema_is_typed_and_not_maintenance(tmp_path):
    _, path, timeline = build(tmp_path)
    write_technical(path, [
        {"timestamp": 1000, "type": "alarm_started", "status": "warning",
         "alarm": {"level": "warning", "code": "cell_delta_warning", "module": 2,
                   "message": "Modul 2 auffällig"}},
        {"timestamp": 1001, "type": "alarm_cleared", "code": "2:cell_delta_warning"},
        {"timestamp": 1002, "type": "status_changed", "from": "warning", "to": "ok"},
    ])

    events = timeline.query()

    assert [event.event_type for event in events] == [
        "alarm_started", "alarm_cleared", "status_changed"
    ]
    assert all(event.maintenance_event_id is None for event in events)
    assert events[0].module_number == 2 and events[0].severity == "warning"


def test_chronological_sort_tie_breaker_and_combined_filters(tmp_path):
    maintenance, path, timeline = build(tmp_path)
    add(maintenance, occurred_at="1970-01-01T00:16:40+00:00")
    write_technical(path, [{"timestamp": 1000, "type": "status_changed", "from": None, "to": "ok"}])

    events = timeline.query(event_types={"maintenance", "status_changed"},
                            timestamp_from="1970-01-01T00:16:40+00:00",
                            timestamp_to="1970-01-01T00:16:40+00:00", module_number=3,
                            category="inspection")

    assert [event.event_type for event in events] == ["maintenance"]
    all_events = timeline.query(timestamp_from="1970-01-01T00:16:40+00:00",
                                timestamp_to="1970-01-01T00:16:40+00:00")
    assert [event.event_type for event in all_events] == ["maintenance", "status_changed"]


@pytest.mark.parametrize("content", ["{broken\n", '{"timestamp": 1, "type": "unknown"}\n',
                                      '{"timestamp": "now", "type": "status_changed", "to": "ok"}\n'])
def test_corrupt_technical_history_is_controlled(tmp_path, content):
    _, path, timeline = build(tmp_path)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TechnicalHistoryError):
        timeline.query()
