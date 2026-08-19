from datetime import datetime, timezone

from event_overlay import EventOverlayAdapter, OverlayContext, matches_chart
from test_timeline import add, build
from timeline import TimelineEvent


def projected(*, module=None, cell=None):
    return TimelineEvent(event_type="maintenance", timestamp="2026-08-12T10:42:00+00:00",
                         title="Prüfung", summary="", source="maintenance_events",
                         projection_key="maintenance:x", module_number=module, cell_number=cell)


def test_central_system_module_and_cell_matching_rules():
    system, module3, cell7, cell8, module4 = (
        projected(), projected(module=3), projected(module=3, cell=7),
        projected(module=3, cell=8), projected(module=4),
    )
    assert all(matches_chart(event, module_number=None, cell_number=None)
               for event in (system, module3, cell7, cell8, module4))
    assert matches_chart(system, module_number=3, cell_number=None)
    assert matches_chart(module3, module_number=3, cell_number=None)
    assert not matches_chart(module4, module_number=3, cell_number=None)
    assert matches_chart(system, module_number=3, cell_number=7)
    assert matches_chart(module3, module_number=3, cell_number=7)
    assert matches_chart(cell7, module_number=3, cell_number=7)
    assert not matches_chart(cell8, module_number=3, cell_number=7)
    assert not matches_chart(module4, module_number=3, cell_number=7)


def test_marker_position_uses_occurred_at_and_not_capture_or_update(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    created = add(maintenance, occurred_at="2026-08-12T10:42:00Z")
    maintenance.update(created.maintenance_event_id, expected_revision=1,
                       changes={"title": "Bearbeitet"})
    marker = EventOverlayAdapter(timeline).markers(OverlayContext(
        timestamp_from="2026-08-01T00:00:00+00:00",
        timestamp_to="2026-09-01T00:00:00+00:00", module_number=3,
    ))[0]
    expected = ((datetime(2026, 8, 12, 10, 42, tzinfo=timezone.utc) -
                 datetime(2026, 8, 1, tzinfo=timezone.utc)).total_seconds() /
                (datetime(2026, 9, 1, tzinfo=timezone.utc) -
                 datetime(2026, 8, 1, tzinfo=timezone.utc)).total_seconds())
    assert marker.position == expected
    assert marker.timestamp == "2026-08-12T10:42:00+00:00"
    assert marker.maintenance_event_id == created.maintenance_event_id
    assert marker.deep_link.endswith(created.maintenance_event_id)


def test_window_archive_and_reload_semantics(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    in_window = add(maintenance, occurred_at="2026-08-12T10:42:00Z")
    add(maintenance, occurred_at="2025-08-12T10:42:00Z", title="außerhalb")
    adapter = EventOverlayAdapter(timeline)
    context = OverlayContext(timestamp_from="2026-08-01T00:00:00+00:00",
                             timestamp_to="2026-09-01T00:00:00+00:00")
    assert [m.maintenance_event_id for m in adapter.markers(context)] == [in_window.maintenance_event_id]
    maintenance.archive(in_window.maintenance_event_id, expected_revision=1)
    assert adapter.markers(context) == []
    archived = adapter.markers(OverlayContext(**{**context.__dict__, "include_archived": True}))
    assert archived[0].deep_link.endswith(in_window.maintenance_event_id)
