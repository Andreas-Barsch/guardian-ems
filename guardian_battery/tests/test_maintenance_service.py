import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from maintenance import (
    MAINTENANCE_SCHEMA_VERSION,
    MaintenanceEvent,
    MaintenanceEventLog,
    MaintenanceValidationError,
    new_maintenance_event,
)
from maintenance_service import (
    MaintenanceArchivedError,
    MaintenanceConflictError,
    MaintenanceHistoryError,
    MaintenanceNotArchivedError,
    MaintenanceNotFoundError,
    MaintenanceRepository,
    MaintenanceService,
)


BASE_TIME = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=BASE_TIME):
        self.current = start

    def __call__(self):
        result = self.current
        self.current += timedelta(minutes=1)
        return result


def service(tmp_path, *, clock=None):
    log = MaintenanceEventLog(tmp_path / "maintenance_events.jsonl")
    repository = MaintenanceRepository(log)
    return MaintenanceService(repository, clock=clock or Clock()), repository, log


def create(service, **overrides):
    values = {
        "occurred_at": "2025-03-12T14:00:00+01:00",
        "category": "maintenance",
        "title": "Batterie geprüft",
        "description": "Sicht- und Funktionsprüfung",
        "affected_system": "Pylontech Stack",
        "module_number": 1,
        "source": {"kind": "manual"},
    }
    values.update(overrides)
    return service.create(**values)


def test_create_persists_revision_one_and_reload_preserves_identity(tmp_path):
    first_service, _, log = service(tmp_path)
    created = create(first_service)

    reloaded = MaintenanceService(MaintenanceRepository(log))
    restored = reloaded.get(created.maintenance_event_id)

    assert created.revision == 1
    assert restored == created
    assert restored.maintenance_event_id == created.maintenance_event_id


def test_update_creates_revision_two_and_preserves_identity_and_creation(tmp_path):
    api, _, _ = service(tmp_path)
    created = create(api)
    updated = api.update(
        created.maintenance_event_id,
        expected_revision=1,
        changes={"title": "Batterie geprüft und freigegeben"},
    )

    assert updated.revision == 2
    assert updated.maintenance_event_id == created.maintenance_event_id
    assert updated.created_at == created.created_at
    assert updated.occurred_at == created.occurred_at
    assert updated.updated_at != created.updated_at
    assert api.history(created.maintenance_event_id) == (created, updated)


def test_multiple_updates_are_monotone_and_physically_append_only(tmp_path):
    api, _, log = service(tmp_path)
    first = create(api)
    first_bytes = log.path.read_bytes()
    second = api.update(
        first.maintenance_event_id,
        expected_revision=1,
        changes={"result": "Prüfung begonnen"},
    )
    third = api.update(
        first.maintenance_event_id,
        expected_revision=2,
        changes={"result": "Prüfung abgeschlossen"},
    )

    assert [item.revision for item in api.history(first.maintenance_event_id)] == [1, 2, 3]
    assert log.path.read_bytes().startswith(first_bytes)
    assert log.read_all() == [first, second, third]


def test_occurred_at_changes_only_when_explicitly_corrected(tmp_path):
    api, _, _ = service(tmp_path)
    first = create(api)
    unchanged = api.update(
        first.maintenance_event_id,
        expected_revision=1,
        changes={"description": "Ergänzte Notiz"},
    )
    corrected = api.update(
        first.maintenance_event_id,
        expected_revision=2,
        changes={"occurred_at": "2025-03-13T09:00:00+01:00"},
    )

    assert unchanged.occurred_at == first.occurred_at
    assert corrected.occurred_at == "2025-03-13T08:00:00+00:00"


def test_update_rejects_immutable_fields_and_empty_changes(tmp_path):
    api, _, _ = service(tmp_path)
    first = create(api)

    with pytest.raises(MaintenanceValidationError, match="at least one"):
        api.update(first.maintenance_event_id, expected_revision=1, changes={})
    with pytest.raises(MaintenanceValidationError, match="cannot be edited"):
        api.update(
            first.maintenance_event_id,
            expected_revision=1,
            changes={"created_at": "2020-01-01T00:00:00+00:00"},
        )


def test_optimistic_concurrency_accepts_current_revision(tmp_path):
    api, _, _ = service(tmp_path)
    first = create(api)

    updated = api.update(
        first.maintenance_event_id,
        expected_revision=first.revision,
        changes={"result": "erfolgreich"},
    )

    assert updated.revision == 2


def test_optimistic_concurrency_rejects_stale_revision_without_append(tmp_path):
    api, _, log = service(tmp_path)
    first = create(api)
    api.update(
        first.maintenance_event_id,
        expected_revision=1,
        changes={"result": "erste Änderung"},
    )
    lines_before = log.path.read_text(encoding="utf-8").splitlines()

    with pytest.raises(MaintenanceConflictError) as error:
        api.update(
            first.maintenance_event_id,
            expected_revision=1,
            changes={"result": "veraltete Änderung"},
        )

    assert error.value.expected_revision == 1
    assert error.value.actual_revision == 2
    assert log.path.read_text(encoding="utf-8").splitlines() == lines_before


def test_concurrent_compare_and_append_allows_only_one_successor(tmp_path):
    api, repository, _ = service(tmp_path)
    first = create(api)
    candidate = replace(
        first,
        revision=2,
        updated_at="2026-08-20T09:00:00+00:00",
        result="parallel candidate",
    )

    def append_candidate():
        try:
            repository.append_revision(candidate, expected_revision=1)
            return "written"
        except MaintenanceConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: append_candidate(), range(2)))

    assert sorted(outcomes) == ["conflict", "written"]
    assert [item.revision for item in repository.history(first.maintenance_event_id)] == [1, 2]


def test_archive_is_revision_and_hidden_by_default_but_remains_addressable(tmp_path):
    api, _, _ = service(tmp_path)
    first = create(api)
    archived = api.archive(first.maintenance_event_id, expected_revision=1)

    assert archived.revision == 2
    assert archived.archived_at is not None
    assert archived.updated_at == archived.archived_at
    assert api.list() == []
    assert api.list(include_archived=True) == [archived]
    assert api.get(first.maintenance_event_id) == archived
    assert api.history(first.maintenance_event_id) == (first, archived)


def test_restore_appends_revision_and_reactivates_event(tmp_path):
    api, _, _ = service(tmp_path)
    first = create(api)
    archived = api.archive(first.maintenance_event_id, expected_revision=1)
    restored = api.restore(first.maintenance_event_id, expected_revision=2)

    assert restored.revision == 3
    assert restored.maintenance_event_id == first.maintenance_event_id
    assert restored.archived_at is None
    assert api.list() == [restored]
    assert [item.revision for item in api.history(first.maintenance_event_id)] == [1, 2, 3]
    with pytest.raises(MaintenanceNotArchivedError):
        api.restore(first.maintenance_event_id, expected_revision=3)


def test_active_inactive_is_append_only_and_optimistically_concurrent(tmp_path):
    api, _, log = service(tmp_path)
    first = create(api)
    original = log.path.read_bytes()

    inactive = api.set_active(first.maintenance_event_id, expected_revision=1, active=False)
    assert inactive.revision == 2 and inactive.archived_at is not None
    assert api.list() == []
    assert api.list(include_archived=True) == [inactive]
    with pytest.raises(MaintenanceConflictError):
        api.set_active(first.maintenance_event_id, expected_revision=1, active=True)

    active = api.set_active(first.maintenance_event_id, expected_revision=2, active=True)
    assert active.revision == 3 and active.archived_at is None
    assert active.maintenance_event_id == first.maintenance_event_id
    assert log.path.read_bytes().startswith(original)


def test_archived_event_cannot_be_edited_or_archived_twice(tmp_path):
    api, _, _ = service(tmp_path)
    first = create(api)
    api.archive(first.maintenance_event_id, expected_revision=1)

    with pytest.raises(MaintenanceArchivedError):
        api.update(
            first.maintenance_event_id,
            expected_revision=2,
            changes={"title": "nicht zulässig"},
        )
    with pytest.raises(MaintenanceArchivedError):
        api.archive(first.maintenance_event_id, expected_revision=2)


def test_multiple_events_are_independent_and_sorted_deterministically(tmp_path):
    api, _, _ = service(tmp_path)
    later_b = create(api, occurred_at="2025-04-01T10:00:00+00:00", title="B")
    earlier = create(api, occurred_at="2025-03-01T10:00:00+00:00", title="A")
    later_a = create(api, occurred_at="2025-04-01T10:00:00+00:00", title="C")

    newest = api.list()
    chronological = api.list(newest_first=False)

    assert newest[0].occurred_at == later_a.occurred_at
    assert newest[-1] == earlier
    assert chronological[0] == earlier
    assert [item.maintenance_event_id for item in chronological[1:]] == sorted(
        [later_a.maintenance_event_id, later_b.maintenance_event_id]
    )
    assert {item.maintenance_event_id for item in newest} == {
        earlier.maintenance_event_id,
        later_a.maintenance_event_id,
        later_b.maintenance_event_id,
    }


def test_missing_event_has_domain_specific_error(tmp_path):
    api, _, _ = service(tmp_path)

    with pytest.raises(MaintenanceNotFoundError):
        api.get("MEV-00000000-0000-4000-8000-000000000000")


def test_identical_duplicate_revision_is_rejected(tmp_path):
    api, repository, log = service(tmp_path)
    first = create(api)
    log.append(first)

    with pytest.raises(MaintenanceHistoryError, match="identical duplicate"):
        repository.list()


def test_conflicting_duplicate_revision_is_rejected(tmp_path):
    api, repository, log = service(tmp_path)
    first = create(api)
    log.append(replace(first, title="Widerspruch"))

    with pytest.raises(MaintenanceHistoryError, match="conflicting duplicate"):
        repository.list()


def test_revision_gap_is_rejected(tmp_path):
    api, repository, log = service(tmp_path)
    first = create(api)
    log.append(
        replace(
            first,
            revision=3,
            updated_at="2026-08-20T09:00:00+00:00",
        )
    )

    with pytest.raises(MaintenanceHistoryError, match="revision gap"):
        repository.list()


def test_missing_revision_corrupt_json_and_unknown_schema_are_history_errors(tmp_path):
    cases = []
    valid = new_maintenance_event(
        occurred_at="2025-01-01T00:00:00+00:00",
        category="inspection",
        title="Test",
        affected_system="Stack",
        now=BASE_TIME,
    ).to_dict()
    missing_revision = dict(valid)
    missing_revision.pop("revision")
    cases.append(json.dumps(missing_revision))
    cases.append('{"broken":')
    unknown_schema = dict(valid)
    unknown_schema["schema_version"] = 99
    cases.append(json.dumps(unknown_schema))

    for index, raw in enumerate(cases):
        path = tmp_path / f"case-{index}.jsonl"
        path.write_text(raw + "\n", encoding="utf-8")
        repository = MaintenanceRepository(MaintenanceEventLog(path))
        with pytest.raises(MaintenanceHistoryError, match="line 1"):
            repository.list()


def test_changed_created_at_and_missing_updated_at_are_rejected(tmp_path):
    api, repository, log = service(tmp_path)
    first = create(api)
    log.append(
        replace(
            first,
            revision=2,
            created_at="2026-08-20T08:01:00+00:00",
            updated_at="2026-08-20T08:02:00+00:00",
        )
    )
    with pytest.raises(MaintenanceHistoryError, match="created_at changed"):
        repository.list()

    second_path = tmp_path / "missing-updated.jsonl"
    second_log = MaintenanceEventLog(second_path)
    second_log.append(first)
    second_log.append(replace(first, revision=2, updated_at=None))
    with pytest.raises(MaintenanceHistoryError, match="has no updated_at"):
        MaintenanceRepository(second_log).list()


def test_import_entry_point_accepts_prebuilt_uuid5_event_with_provenance(tmp_path):
    _, repository, _ = service(tmp_path)
    legacy_id = f"MEV-{uuid.uuid5(uuid.NAMESPACE_URL, 'guardian:legacy:row-7')}"
    imported = MaintenanceEvent(
        schema_version=MAINTENANCE_SCHEMA_VERSION,
        maintenance_event_id=legacy_id,
        revision=1,
        occurred_at="2024-01-05T10:00:00+00:00",
        created_at="2026-08-20T08:00:00+00:00",
        updated_at=None,
        category="maintenance",
        title="Importierter Testdatensatz",
        description=None,
        affected_system="Stack",
        module_number=None,
        module_serial=None,
        cell_number=None,
        action_taken=None,
        previous_state=None,
        result=None,
        reason=None,
        source={
            "kind": "legacy_import",
            "legacy_locator": "row-7",
            "legacy_hash": "abc123",
        },
        archived_at=None,
    )

    repository.import_revision(imported, expected_revision=0)

    assert repository.get(legacy_id) == imported
