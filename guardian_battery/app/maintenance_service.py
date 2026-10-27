"""Repository projection and domain service for maintenance events."""

from __future__ import annotations

from research_budget import ResearchQueryError, check_budget

import hashlib
import json
import uuid

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping

from maintenance import (
    MaintenanceEvent,
    MaintenanceEventLog,
    MaintenanceStorageError,
    MaintenanceValidationError,
    new_maintenance_event,
    utc_now,
)


class MaintenanceError(RuntimeError):
    """Base class for maintenance repository and service errors."""


class MaintenanceNotFoundError(MaintenanceError):
    """Raised when an event ID has no persisted history."""


class MaintenanceConflictError(MaintenanceError):
    """Raised when optimistic concurrency detects a stale revision."""

    def __init__(self, event_id: str, expected_revision: int, actual_revision: int):
        self.event_id = event_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"maintenance event {event_id} revision conflict: "
            f"expected {expected_revision}, actual {actual_revision}"
        )


class MaintenanceRequestConflictError(MaintenanceError):
    """A create request key was reused for different input."""


class MaintenanceHistoryError(MaintenanceError):
    """Raised when append-only history cannot form one reliable projection."""


class MaintenanceArchivedError(MaintenanceError):
    """Raised when an operation requires an active event."""


class MaintenanceNotArchivedError(MaintenanceError):
    """Raised when restore is requested for an active event."""


class MaintenanceRepository:
    """Reconstruct and extend consistent event projections from a JSONL log."""

    def __init__(self, log: MaintenanceEventLog, *, read_backend=None):
        self.log = log
        self.read_backend = read_backend

    @staticmethod
    def _project(
        records: Iterable[MaintenanceEvent],
    ) -> tuple[dict[str, MaintenanceEvent], dict[str, tuple[MaintenanceEvent, ...]]]:
        check_budget()
        histories: dict[str, list[MaintenanceEvent]] = {}
        for record in records:
            check_budget()
            history = histories.setdefault(record.maintenance_event_id, [])
            expected_revision = len(history) + 1
            if record.revision != expected_revision:
                if record.revision < expected_revision:
                    previous = next(
                        (item for item in history if item.revision == record.revision),
                        None,
                    )
                    duplicate_kind = "identical" if previous == record else "conflicting"
                    raise MaintenanceHistoryError(
                        f"{duplicate_kind} duplicate revision {record.revision} for "
                        f"{record.maintenance_event_id}"
                    )
                raise MaintenanceHistoryError(
                    f"revision gap for {record.maintenance_event_id}: "
                    f"expected {expected_revision}, found {record.revision}"
                )
            if history:
                first = history[0]
                if record.created_at != first.created_at:
                    raise MaintenanceHistoryError(
                        f"created_at changed for {record.maintenance_event_id}"
                    )
                if record.updated_at is None:
                    raise MaintenanceHistoryError(
                        f"revision {record.revision} for {record.maintenance_event_id} "
                        "has no updated_at"
                    )
            elif record.updated_at is not None:
                raise MaintenanceHistoryError(
                    f"initial revision for {record.maintenance_event_id} has updated_at"
                )
            history.append(record)

        immutable_histories = {
            event_id: tuple(history) for event_id, history in histories.items()
        }
        current = {event_id: history[-1] for event_id, history in immutable_histories.items()}
        return current, immutable_histories

    def _load(
        self,
    ) -> tuple[dict[str, MaintenanceEvent], dict[str, tuple[MaintenanceEvent, ...]]]:
        check_budget()
        try:
            return self._project(self.log.read_all())
        except MaintenanceStorageError as exc:
            raise MaintenanceHistoryError(str(exc)) from exc

    def get(self, event_id: str) -> MaintenanceEvent:
        if self.read_backend is not None:
            value = self.read_backend.maintenance_get(event_id)
            if value is None:
                raise MaintenanceNotFoundError(f"maintenance event not found: {event_id}")
            return value
        current, _ = self._load()
        try:
            return current[event_id]
        except KeyError as exc:
            raise MaintenanceNotFoundError(f"maintenance event not found: {event_id}") from exc

    def history(self, event_id: str) -> tuple[MaintenanceEvent, ...]:
        if self.read_backend is not None:
            values = self.read_backend.maintenance_history(event_id)
            if not values:
                raise MaintenanceNotFoundError(f"maintenance event not found: {event_id}")
            return values
        _, histories = self._load()
        try:
            return histories[event_id]
        except KeyError as exc:
            raise MaintenanceNotFoundError(f"maintenance event not found: {event_id}") from exc

    def list(
        self,
        *,
        include_archived: bool = False,
        newest_first: bool = True,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
    ) -> list[MaintenanceEvent]:
        if self.read_backend is not None:
            return self.read_backend.maintenance_list(include_archived=include_archived,newest_first=newest_first,
                occurred_from=occurred_from,occurred_to=occurred_to)
        check_budget()
        current, _ = self._load()
        items = [
            event
            for event in current.values()
            if (include_archived or event.archived_at is None)
            and (occurred_from is None or event.occurred_at >= occurred_from)
            and (occurred_to is None or event.occurred_at <= occurred_to)
        ]
        # Default logbook order is newest first. Passing newest_first=False
        # provides the chronological order required by a later timeline.
        return sorted(
            items,
            key=lambda event: (event.occurred_at, event.maintenance_event_id),
            reverse=newest_first,
        )

    def append_revision(
        self,
        event: MaintenanceEvent,
        *,
        expected_revision: int,
    ) -> MaintenanceEvent:
        """Atomically compare the current revision and append the successor."""

        with self.log.transaction():
            if self.read_backend is None:
                current, _ = self._load()
                existing = current.get(event.maintenance_event_id)
            else:
                existing = self.read_backend.maintenance_get(event.maintenance_event_id)
            actual_revision = existing.revision if existing else 0
            if actual_revision != expected_revision:
                raise MaintenanceConflictError(
                    event.maintenance_event_id,
                    expected_revision,
                    actual_revision,
                )
            if event.revision != actual_revision + 1:
                raise MaintenanceHistoryError(
                    f"new revision for {event.maintenance_event_id} must be "
                    f"{actual_revision + 1}, got {event.revision}"
                )
            if existing and event.created_at != existing.created_at:
                raise MaintenanceHistoryError(
                    f"created_at changed for {event.maintenance_event_id}"
                )
            self.log.append(event)
        return event

    def find_request(self, request_id):
        if self.read_backend is not None:
            return self.read_backend.maintenance_request(request_id)
        current, histories = self._load()
        for event_id, history in histories.items():
            if history[0].source.get("create_request_id") == request_id:
                return history[0], current[event_id]
        return None

    def import_revision(
        self,
        event: MaintenanceEvent,
        *,
        expected_revision: int,
    ) -> MaintenanceEvent:
        """Append one pre-built import revision without defining a legacy format.

        A future, explicitly approved adapter may construct UUIDv5-backed events
        with legacy provenance and use this entry point. All normal consistency
        and optimistic-concurrency checks still apply.
        """

        return self.append_revision(event, expected_revision=expected_revision)


_EDITABLE_FIELDS = frozenset(
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


class MaintenanceService:
    """Apply domain operations without HTTP, UI or MQTT concerns."""

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        clock: Callable[[], datetime] = utc_now,
    ):
        self.repository = repository
        self.clock = clock

    def create(self, **fields: Any) -> MaintenanceEvent:
        fields = dict(fields)
        fields["now"] = self.clock()
        event = new_maintenance_event(**fields)
        return self.repository.append_revision(event, expected_revision=0)

    def create_once(self, request_id: str, **fields: Any) -> tuple[MaintenanceEvent, bool]:
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MaintenanceValidationError("Idempotency-Key must be a UUID") from exc
        try:
            fingerprint = hashlib.sha256(json.dumps(
                fields, sort_keys=True, ensure_ascii=False, allow_nan=False,
                separators=(",", ":")).encode()).hexdigest()
        except (TypeError, ValueError) as exc:
            raise MaintenanceValidationError("Create request must contain valid JSON values") from exc
        # Read, compare and append use the same stable lock, including deletion.
        with self.repository.log.transaction():
            prior = self.repository.find_request(request_id)
            if prior is not None:
                first, current = prior
                if first.source.get("create_request_hash") != fingerprint:
                    raise MaintenanceRequestConflictError("Request key already used for different input")
                return current, False
            if self.repository.log.request_retired(request_id):
                raise MaintenanceNotFoundError("The event for this save request has been deleted")
            source = dict(fields.pop("source", {"kind": "manual"}))
            source.update(create_request_id=request_id, create_request_hash=fingerprint)
            event = new_maintenance_event(**fields, source=source, now=self.clock())
            self.repository.log.append(event)
            return event, True

    def delete(self, event_id: str, *, expected_revision: int) -> None:
        with self.repository.log.transaction():
            if self.repository.read_backend is None:
                current, histories = self.repository._load()
                event = current.get(event_id)
                if event is None:
                    raise MaintenanceNotFoundError(f"maintenance event not found: {event_id}")
                history = histories[event_id]
            else:
                event = self.repository.get(event_id)
                history = self.repository.history(event_id)
            if event.revision != expected_revision:
                raise MaintenanceConflictError(event_id, expected_revision, event.revision)
            self.repository.log.remove_event(event_id, history[0].source.get("create_request_id"))

    def get(self, event_id: str) -> MaintenanceEvent:
        return self.repository.get(event_id)

    def history(self, event_id: str) -> tuple[MaintenanceEvent, ...]:
        return self.repository.history(event_id)

    def list(
        self,
        *,
        include_archived: bool = False,
        newest_first: bool = True,
        occurred_from: str | None = None,
        occurred_to: str | None = None,
    ) -> list[MaintenanceEvent]:
        return self.repository.list(
            include_archived=include_archived,
            newest_first=newest_first,
            occurred_from=occurred_from,occurred_to=occurred_to,
        )

    def update(
        self,
        event_id: str,
        *,
        expected_revision: int,
        changes: Mapping[str, Any],
    ) -> MaintenanceEvent:
        if not changes:
            raise MaintenanceValidationError("update requires at least one changed field")
        unknown = set(changes) - _EDITABLE_FIELDS
        if unknown:
            raise MaintenanceValidationError(
                f"fields cannot be edited: {', '.join(sorted(unknown))}"
            )
        current = self.repository.get(event_id)
        if current.archived_at is not None:
            raise MaintenanceArchivedError(f"maintenance event is archived: {event_id}")
        updated = replace(
            current,
            **dict(changes),
            revision=current.revision + 1,
            updated_at=self.clock().isoformat(),
        )
        return self.repository.append_revision(
            updated,
            expected_revision=expected_revision,
        )

    def archive(self, event_id: str, *, expected_revision: int) -> MaintenanceEvent:
        current = self.repository.get(event_id)
        if current.archived_at is not None:
            raise MaintenanceArchivedError(f"maintenance event is already archived: {event_id}")
        archived_at = self.clock().isoformat()
        archived = replace(
            current,
            revision=current.revision + 1,
            updated_at=archived_at,
            archived_at=archived_at,
        )
        return self.repository.append_revision(
            archived,
            expected_revision=expected_revision,
        )

    def restore(self, event_id: str, *, expected_revision: int) -> MaintenanceEvent:
        current = self.repository.get(event_id)
        if current.archived_at is None:
            raise MaintenanceNotArchivedError(f"maintenance event is active: {event_id}")
        restored = replace(
            current,
            revision=current.revision + 1,
            updated_at=self.clock().isoformat(),
            archived_at=None,
        )
        return self.repository.append_revision(
            restored,
            expected_revision=expected_revision,
        )

    def set_active(
        self, event_id: str, *, expected_revision: int, active: bool
    ) -> MaintenanceEvent:
        """Change fachliche activity without changing schema-1 persistence.

        ``archived_at`` remains the on-disk compatibility representation for
        data written by 0.5.0.  The public/domain meaning is active/inactive.
        """

        if not isinstance(active, bool):
            raise MaintenanceValidationError("active must be boolean")
        return (
            self.restore(event_id, expected_revision=expected_revision)
            if active
            else self.archive(event_id, expected_revision=expected_revision)
        )
