import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from maintenance import (
    MAINTENANCE_SCHEMA_VERSION,
    MaintenanceEvent,
    MaintenanceEventLog,
    MaintenanceStorageError,
    MaintenanceValidationError,
    new_maintenance_event,
)


CAPTURED_AT = datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc)


def event(**overrides):
    values = {
        "occurred_at": "2025-03-12T14:00:00+01:00",
        "category": "module_replacement",
        "title": "Modul 3 ersetzt",
        "description": "Austausch nach technischer Prüfung",
        "affected_system": "Pylontech Stack",
        "module_number": 3,
        "module_serial": "Y225004C32250185",
        "action_taken": "Modul ersetzt",
        "previous_state": "Zellabweichung auffällig",
        "result": "Anlage wieder in Betrieb",
        "reason": "Prüfergebnis",
        "source": {"kind": "manual"},
        "now": CAPTURED_AT,
    }
    values.update(overrides)
    return new_maintenance_event(**values)


def test_new_event_has_uuid4_identity_and_separate_utc_timestamps():
    item = event()

    assert item.schema_version == MAINTENANCE_SCHEMA_VERSION
    assert item.revision == 1
    assert item.maintenance_event_id.startswith("MEV-")
    assert uuid.UUID(item.maintenance_event_id.removeprefix("MEV-")).version == 4
    assert item.occurred_at == "2025-03-12T13:00:00+00:00"
    assert item.created_at == "2026-08-20T08:30:00+00:00"
    assert item.updated_at is None
    assert item.archived_at is None


def test_optional_empty_text_is_normalized_without_inventing_values():
    item = event(description="  ", module_serial="", action_taken=None, result=" ")

    assert item.description is None
    assert item.module_serial is None
    assert item.action_taken is None
    assert item.result is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"occurred_at": "2025-03-12T14:00:00"}, "timezone"),
        ({"category": "Modultausch"}, "lower-case slug"),
        ({"title": " "}, "title"),
        ({"module_number": 7}, "module_number"),
        ({"cell_number": 16}, "cell_number"),
        ({"module_number": None, "cell_number": 2}, "requires module_number"),
    ],
)
def test_invalid_event_input_is_rejected(changes, message):
    with pytest.raises(MaintenanceValidationError, match=message):
        event(**changes)


def test_taxonomy_is_extensible_with_stable_slug_categories():
    item = event(category="cooling_system_service")

    assert item.category == "cooling_system_service"


def test_ended_at_is_optional_and_cannot_precede_event_start():
    assert event(ended_at=None).ended_at is None

    with pytest.raises(MaintenanceValidationError, match="must not precede"):
        event(ended_at="2025-03-12T12:59:59+00:00")


def test_revision_timestamps_cannot_precede_creation():
    original = event()

    with pytest.raises(MaintenanceValidationError, match="updated_at"):
        replace(original, updated_at="2026-08-20T08:29:59+00:00")
    with pytest.raises(MaintenanceValidationError, match="archived_at"):
        replace(original, archived_at="2026-08-20T08:29:59+00:00")


def test_schema_round_trip_preserves_all_fields():
    original = event(cell_number=4, ended_at="2025-03-12T14:30:00+01:00")

    restored = MaintenanceEvent.from_dict(original.to_dict())

    assert restored == original


def test_log_is_append_only_and_keeps_multiple_revisions(tmp_path):
    path = tmp_path / "maintenance_events.jsonl"
    log = MaintenanceEventLog(path)
    first = event()
    second = replace(
        first,
        revision=2,
        title="Modul 3 ersetzt und geprüft",
        updated_at=(CAPTURED_AT + timedelta(hours=1)).isoformat(),
    )

    log.append(first)
    first_bytes = path.read_bytes()
    log.append(second)

    assert path.read_bytes().startswith(first_bytes)
    assert log.read_all() == [first, second]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_empty_or_absent_log_reads_as_empty_history(tmp_path):
    path = tmp_path / "maintenance_events.jsonl"
    log = MaintenanceEventLog(path)

    assert log.read_all() == []
    path.touch()
    assert log.read_all() == []


def test_corrupt_jsonl_is_reported_with_line_number(tmp_path):
    path = tmp_path / "maintenance_events.jsonl"
    log = MaintenanceEventLog(path)
    log.append(event())
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"incomplete":')

    with pytest.raises(MaintenanceStorageError, match="line 2"):
        log.read_all()


def test_unknown_schema_version_is_not_silently_reinterpreted(tmp_path):
    path = tmp_path / "maintenance_events.jsonl"
    raw = event().to_dict()
    raw["schema_version"] = 2
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(MaintenanceStorageError, match="unsupported maintenance schema"):
        MaintenanceEventLog(path).read_all()


def test_source_requires_kind_and_json_values():
    with pytest.raises(MaintenanceValidationError, match="source.kind"):
        event(source={})
    with pytest.raises(MaintenanceValidationError, match="JSON values"):
        event(source={"kind": "manual", "invalid": object()})
