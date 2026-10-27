"""Maintenance revisions with append persistence and explicit atomic deletion."""

from __future__ import annotations
from history_live_priority import bounded_metadata_lines


from research_budget import check_budget, budget_lock, acquire_shared_file_lock

import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


MAINTENANCE_SCHEMA_VERSION = 1
MAINTENANCE_EVENT_ID_PREFIX = "MEV-"
DEFAULT_MAINTENANCE_EVENT_FILE = Path(
    "/share/guardian_battery/maintenance_events.jsonl"
)
MAINTENANCE_TEXT_LIMITS = {
    "category": 64,
    "title": 200,
    "affected_system": 200,
    "module_serial": 200,
    "description": 10000,
    "action_taken": 10000,
    "previous_state": 10000,
    "result": 10000,
    "reason": 10000,
    "source.kind": 64,
}

# Stable keys for the initial UI. Additional lower-case slug keys remain valid
# so the taxonomy can grow without rewriting existing history.
MAINTENANCE_CATEGORIES = (
    "maintenance",
    "inspection",
    "repair",
    "module_replacement",
    "module_identification",
    "module_position_change",
    "module_added",
    "module_removed",
    "battery_cell_test",
    "firmware_change",
    "configuration_change",
    "wiring_connection",
    "troubleshooting",
    "other_technical",
)

_CATEGORY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_EVENT_ID_PATTERN = re.compile(
    rf"^{MAINTENANCE_EVENT_ID_PREFIX}[0-9a-f]{{8}}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class MaintenanceValidationError(ValueError):
    """Raised when a maintenance event violates schema semantics."""


class MaintenanceStorageError(RuntimeError):
    """Raised when persisted maintenance history cannot be read safely."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | str, field: str) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise MaintenanceValidationError(
                f"{field} must be an ISO-8601 timestamp"
            ) from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise MaintenanceValidationError(f"{field} must be a datetime or string")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaintenanceValidationError(f"{field} must include timezone information")
    return parsed.astimezone(timezone.utc).isoformat()


def _required_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MaintenanceValidationError(f"{field} must not be empty")
    result = value.strip()
    if len(result) > maximum:
        raise MaintenanceValidationError(f"{field} exceeds {maximum} characters")
    return result


def _optional_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MaintenanceValidationError(f"{field} must be text or null")
    result = value.strip()
    if not result:
        return None
    if len(result) > maximum:
        raise MaintenanceValidationError(f"{field} exceeds {maximum} characters")
    return result


def _optional_number(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MaintenanceValidationError(f"{field} must be an integer or null")
    if not minimum <= value <= maximum:
        raise MaintenanceValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def _source(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MaintenanceValidationError("source must be an object")
    result = dict(value)
    result["kind"] = _required_text(
        result.get("kind"), "source.kind", MAINTENANCE_TEXT_LIMITS["source.kind"]
    )
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MaintenanceValidationError("source must contain JSON values") from exc
    return result


@dataclass(frozen=True)
class MaintenanceEvent:
    """One immutable revision of a single maintenance event."""

    schema_version: int
    maintenance_event_id: str
    revision: int
    occurred_at: str
    created_at: str
    updated_at: str | None
    category: str
    title: str
    description: str | None
    affected_system: str
    module_number: int | None
    module_serial: str | None
    cell_number: int | None
    action_taken: str | None
    previous_state: str | None
    result: str | None
    reason: str | None
    source: dict[str, Any]
    archived_at: str | None
    ended_at: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != MAINTENANCE_SCHEMA_VERSION:
            raise MaintenanceValidationError(
                f"unsupported maintenance schema version: {self.schema_version}"
            )
        if not isinstance(self.maintenance_event_id, str) or not _EVENT_ID_PATTERN.fullmatch(
            self.maintenance_event_id
        ):
            raise MaintenanceValidationError("maintenance_event_id must be MEV- plus a canonical UUID")
        try:
            parsed_event_id = uuid.UUID(
                self.maintenance_event_id.removeprefix(MAINTENANCE_EVENT_ID_PREFIX)
            )
        except ValueError as exc:
            raise MaintenanceValidationError(
                "maintenance_event_id must contain a valid UUID"
            ) from exc
        if str(parsed_event_id) != self.maintenance_event_id.removeprefix(
            MAINTENANCE_EVENT_ID_PREFIX
        ):
            raise MaintenanceValidationError("maintenance_event_id must use canonical UUID form")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise MaintenanceValidationError("revision must be a positive integer")

        object.__setattr__(self, "occurred_at", _utc_iso(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "created_at", _utc_iso(self.created_at, "created_at"))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", _utc_iso(self.updated_at, "updated_at"))
            if self.updated_at < self.created_at:
                raise MaintenanceValidationError("updated_at must not precede created_at")
        if self.archived_at is not None:
            object.__setattr__(self, "archived_at", _utc_iso(self.archived_at, "archived_at"))
            if self.archived_at < self.created_at:
                raise MaintenanceValidationError("archived_at must not precede created_at")
        if self.ended_at is not None:
            object.__setattr__(self, "ended_at", _utc_iso(self.ended_at, "ended_at"))
            if self.ended_at < self.occurred_at:
                raise MaintenanceValidationError("ended_at must not precede occurred_at")

        category = _required_text(
            self.category, "category", MAINTENANCE_TEXT_LIMITS["category"]
        )
        if not _CATEGORY_PATTERN.fullmatch(category):
            raise MaintenanceValidationError("category must be a lower-case slug")
        object.__setattr__(self, "category", category)
        object.__setattr__(
            self, "title", _required_text(self.title, "title", MAINTENANCE_TEXT_LIMITS["title"])
        )
        object.__setattr__(
            self,
            "affected_system",
            _required_text(
                self.affected_system,
                "affected_system",
                MAINTENANCE_TEXT_LIMITS["affected_system"],
            ),
        )
        object.__setattr__(
            self,
            "description",
            _optional_text(
                self.description, "description", MAINTENANCE_TEXT_LIMITS["description"]
            ),
        )
        object.__setattr__(
            self,
            "module_serial",
            _optional_text(
                self.module_serial, "module_serial", MAINTENANCE_TEXT_LIMITS["module_serial"]
            ),
        )
        object.__setattr__(
            self,
            "action_taken",
            _optional_text(
                self.action_taken, "action_taken", MAINTENANCE_TEXT_LIMITS["action_taken"]
            ),
        )
        object.__setattr__(
            self,
            "previous_state",
            _optional_text(
                self.previous_state,
                "previous_state",
                MAINTENANCE_TEXT_LIMITS["previous_state"],
            ),
        )
        object.__setattr__(
            self,
            "result",
            _optional_text(self.result, "result", MAINTENANCE_TEXT_LIMITS["result"]),
        )
        object.__setattr__(
            self,
            "reason",
            _optional_text(self.reason, "reason", MAINTENANCE_TEXT_LIMITS["reason"]),
        )
        object.__setattr__(self, "module_number", _optional_number(self.module_number, "module_number", 1, 6))
        object.__setattr__(self, "cell_number", _optional_number(self.cell_number, "cell_number", 1, 15))
        if self.cell_number is not None and self.module_number is None:
            raise MaintenanceValidationError("cell_number requires module_number")
        object.__setattr__(self, "source", _source(self.source))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MaintenanceEvent":
        if not isinstance(data, Mapping):
            raise MaintenanceValidationError("maintenance record must be an object")
        try:
            return cls(**dict(data))
        except TypeError as exc:
            raise MaintenanceValidationError(f"invalid maintenance record fields: {exc}") from exc


def new_maintenance_event(
    *,
    occurred_at: datetime | str,
    category: str,
    title: str,
    affected_system: str,
    description: str | None = None,
    module_number: int | None = None,
    module_serial: str | None = None,
    cell_number: int | None = None,
    action_taken: str | None = None,
    previous_state: str | None = None,
    result: str | None = None,
    reason: str | None = None,
    source: Mapping[str, Any] | None = None,
    ended_at: datetime | str | None = None,
    now: datetime | None = None,
) -> MaintenanceEvent:
    """Create revision one with a new UUIDv4 and distinct event/capture times."""

    captured_at = now or utc_now()
    return MaintenanceEvent(
        schema_version=MAINTENANCE_SCHEMA_VERSION,
        maintenance_event_id=f"{MAINTENANCE_EVENT_ID_PREFIX}{uuid.uuid4()}",
        revision=1,
        occurred_at=_utc_iso(occurred_at, "occurred_at"),
        created_at=_utc_iso(captured_at, "created_at"),
        updated_at=None,
        category=category,
        title=title,
        description=description,
        affected_system=affected_system,
        module_number=module_number,
        module_serial=module_serial,
        cell_number=cell_number,
        action_taken=action_taken,
        previous_state=previous_state,
        result=result,
        reason=reason,
        source=dict(source if source is not None else {"kind": "manual"}),
        archived_at=None,
        ended_at=_utc_iso(ended_at, "ended_at") if ended_at is not None else None,
    )


class MaintenanceEventLog:
    """JSONL revision store; ordinary edits append, explicit deletion rewrites atomically."""

    def __init__(self, path: Path, *, append_backend=None):
        self.append_backend = append_backend
        self.mutation_backend = None
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self):
        """Serialize a repository read-check-append transaction.

        The separate lock file keeps the lock stable even before the JSONL file
        exists and avoids replacing or rewriting the append-only data file.
        """

        with self.lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def append(self, event: MaintenanceEvent) -> None:
        if self.mutation_backend is not None:self.mutation_backend.assert_writable()
        if not isinstance(event, MaintenanceEvent):
            raise TypeError("event must be a MaintenanceEvent")
        raw = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        if self.append_backend is not None:
            self.append_backend.append(self.path, raw)
            return
        with self.path.open("ab") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def request_retired(self, request_id: str) -> bool:
        if self.mutation_backend is not None:return self.mutation_backend.request_retired(request_id)
        path = self.path.with_suffix(self.path.suffix + ".retired-requests")
        if not path.exists():
            return False
        if self.append_backend is not None:
            from projection_repository import Unavailable
            raise Unavailable('confirmed retired-request ledger requires explicit preparation')
        digest = hashlib.sha256(request_id.encode("ascii")).hexdigest()
        return digest in path.read_text(encoding="ascii").splitlines()

    def remove_event(self, event_id: str, request_id: str | None = None) -> None:
        """Caller holds transaction lock; remove every revision atomically.

        Keep only an opaque request digest to reject delayed create retries.
        No event, payload, event ID, or restorable revision is retained there.
        """
        if self.mutation_backend is not None:
            return self.mutation_backend.delete(event_id,request_id)
        if self.append_backend is not None:
            from projection_repository import Unavailable
            raise Unavailable('confirmed maintenance deletion requires explicit source replacement support')
        if request_id:
            receipt = self.path.with_suffix(self.path.suffix + ".retired-requests")
            with receipt.open("ab") as handle:
                handle.write(hashlib.sha256(request_id.encode("ascii")).hexdigest().encode("ascii") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Make the retry receipt durable before committing event removal.
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".maintenance-delete-", delete=False) as target:
                temporary = Path(target.name)
                os.fchmod(target.fileno(), self.path.stat().st_mode & 0o777)
                with self.path.open("rb") as source:
                    for line in source:
                        if not line.strip() or json.loads(line)["maintenance_event_id"] != event_id:
                            target.write(line)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def read_all(self) -> list[MaintenanceEvent]:
        check_budget()
        if not self.path.exists():
            return []
        events: list[MaintenanceEvent] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                acquire_shared_file_lock(handle)
                try:
                    for line_number, line in enumerate(bounded_metadata_lines(handle), 1):
                        check_budget()
                        if not line.strip():
                            continue
                        try:
                            raw = json.loads(line)
                            events.append(MaintenanceEvent.from_dict(raw))
                        except (json.JSONDecodeError, MaintenanceValidationError) as exc:
                            raise MaintenanceStorageError(
                                f"invalid maintenance record at line {line_number}: {exc}"
                            ) from exc
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise MaintenanceStorageError(f"cannot read {self.path}: {exc}") from exc
        return events

    def __iter__(self) -> Iterable[MaintenanceEvent]:
        return iter(self.read_all())


def normalize_utc_timestamp(value: datetime | str, field: str = "timestamp") -> str:
    """Validate an external timestamp and return canonical UTC ISO-8601."""

    return _utc_iso(value, field)


def validate_maintenance_category(value: Any) -> str:
    """Validate and normalize a category key for API filters."""

    category = _required_text(
        value, "category", MAINTENANCE_TEXT_LIMITS["category"]
    )
    if not _CATEGORY_PATTERN.fullmatch(category):
        raise MaintenanceValidationError("category must be a lower-case slug")
    return category
