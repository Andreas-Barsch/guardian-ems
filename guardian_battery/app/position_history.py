"""Append-only provenance for physical module serials at stack positions."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from maintenance import normalize_utc_timestamp


POSITION_HISTORY_SCHEMA_VERSION = 1
POSITION_HISTORY_ID_PREFIX = "PHS-"
DEFAULT_POSITION_HISTORY_FILE = Path("/share/guardian_battery/position_history.jsonl")
STACK_POSITIONS = tuple(range(1, 7))


class PositionHistoryError(RuntimeError):
    pass


class PositionHistoryValidationError(ValueError):
    pass


class PositionHistoryConflictError(PositionHistoryError):
    def __init__(self, expected: str | None, actual: str | None):
        self.expected = expected
        self.actual = actual
        super().__init__(f"position history conflict: expected {expected!r}, actual {actual!r}")


def _positions(value: Any) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        raise PositionHistoryValidationError("positions must be an object")
    expected = {str(item) for item in STACK_POSITIONS}
    if set(value) != expected:
        raise PositionHistoryValidationError("positions must contain exactly positions 1 through 6")
    result: dict[str, str | None] = {}
    seen: set[str] = set()
    for key in sorted(expected, key=int):
        serial = value[key]
        if serial is not None:
            if not isinstance(serial, str) or not serial.strip():
                raise PositionHistoryValidationError(f"position {key} serial must be text or null")
            serial = serial.strip()
            if len(serial) > 200:
                raise PositionHistoryValidationError(f"position {key} serial exceeds 200 characters")
            if serial in seen:
                raise PositionHistoryValidationError("one physical serial cannot occupy two positions")
            seen.add(serial)
        result[key] = serial
    return result


@dataclass(frozen=True)
class PositionSnapshot:
    schema_version: int
    position_history_id: str
    effective_at: str
    created_at: str
    maintenance_event_id: str
    positions: dict[str, str | None]

    def __post_init__(self) -> None:
        if self.schema_version != POSITION_HISTORY_SCHEMA_VERSION:
            raise PositionHistoryValidationError("unsupported position history schema")
        if not isinstance(self.position_history_id, str) or not self.position_history_id.startswith(POSITION_HISTORY_ID_PREFIX):
            raise PositionHistoryValidationError("invalid position_history_id")
        try:
            canonical = str(uuid.UUID(self.position_history_id.removeprefix(POSITION_HISTORY_ID_PREFIX)))
        except ValueError as exc:
            raise PositionHistoryValidationError("invalid position_history_id") from exc
        if self.position_history_id != POSITION_HISTORY_ID_PREFIX + canonical:
            raise PositionHistoryValidationError("invalid position_history_id")
        object.__setattr__(self, "effective_at", normalize_utc_timestamp(self.effective_at, "effective_at"))
        object.__setattr__(self, "created_at", normalize_utc_timestamp(self.created_at, "created_at"))
        if not isinstance(self.maintenance_event_id, str) or not self.maintenance_event_id.startswith("MEV-"):
            raise PositionHistoryValidationError("maintenance_event_id is required")
        object.__setattr__(self, "positions", _positions(self.positions))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "PositionSnapshot":
        if not isinstance(value, Mapping):
            raise PositionHistoryValidationError("position history record must be an object")
        try:
            return cls(**dict(value))
        except TypeError as exc:
            raise PositionHistoryValidationError("position history record has invalid fields") from exc


class PositionHistoryLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> list[PositionSnapshot]:
        if not self.path.exists():
            return []
        result = []
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if line.strip():
                        try:
                            result.append(PositionSnapshot.from_dict(json.loads(line)))
                        except (json.JSONDecodeError, PositionHistoryValidationError) as exc:
                            raise PositionHistoryError(f"invalid position history line {line_number}") from exc
        except OSError as exc:
            raise PositionHistoryError("position history is unavailable") from exc
        ids = [item.position_history_id for item in result]
        if len(ids) != len(set(ids)):
            raise PositionHistoryError("duplicate position history ID")
        return result

    def append(self, snapshot: PositionSnapshot) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PositionHistoryError("position history is unavailable") from exc


class PositionHistoryService:
    def __init__(self, log: PositionHistoryLog, maintenance_service, *, clock: Callable[[], datetime] | None = None):
        self.log = log
        self.maintenance_service = maintenance_service
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def list(self) -> list[PositionSnapshot]:
        return sorted(self.log.read_all(), key=lambda item: (item.effective_at, item.created_at, item.position_history_id))

    def current(self) -> PositionSnapshot | None:
        items = self.list()
        return items[-1] if items else None

    def at(self, timestamp: str) -> PositionSnapshot | None:
        target = normalize_utc_timestamp(timestamp, "at")
        items = [item for item in self.list() if item.effective_at <= target]
        return items[-1] if items else None

    def record(self, *, effective_at: str, maintenance_event_id: str,
               positions: Mapping[str, str | None], expected_latest_snapshot_id: str | None) -> PositionSnapshot:
        event = self.maintenance_service.get(maintenance_event_id)
        if event.archived_at is not None:
            raise PositionHistoryValidationError("maintenance event must be active")
        existing_records = self.log.read_all()
        effective = normalize_utc_timestamp(effective_at, "effective_at")
        now = self.clock()
        if not existing_records:
            effective_time = datetime.fromisoformat(effective)
            if abs((effective_time - now.astimezone(timezone.utc)).total_seconds()) > 300:
                raise PositionHistoryValidationError(
                    "initial documentation must be effective at capture time, not retrospectively"
                )
        snapshot = PositionSnapshot(
            schema_version=POSITION_HISTORY_SCHEMA_VERSION,
            position_history_id=POSITION_HISTORY_ID_PREFIX + str(uuid.uuid4()),
            effective_at=effective,
            created_at=now.isoformat(),
            maintenance_event_id=maintenance_event_id,
            positions=dict(positions),
        )
        with self.log.transaction():
            records = self.log.read_all()
            latest_append = records[-1] if records else None
            actual = latest_append.position_history_id if latest_append else None
            if expected_latest_snapshot_id != actual:
                raise PositionHistoryConflictError(expected_latest_snapshot_id, actual)
            self.log.append(snapshot)
        return snapshot

    def position_at(self, position: int, timestamp: str) -> str | None:
        if position not in STACK_POSITIONS:
            raise PositionHistoryValidationError("position must be between 1 and 6")
        snapshot = self.at(timestamp)
        return snapshot.positions[str(position)] if snapshot else None

    def serial_at(self, serial: str, timestamp: str) -> int | None:
        snapshot = self.at(timestamp)
        if not snapshot:
            return None
        return next((int(position) for position, value in snapshot.positions.items() if value == serial), None)

    def known_serials(self, position: int) -> list[str]:
        if position not in STACK_POSITIONS:
            raise PositionHistoryValidationError("position must be between 1 and 6")
        return sorted({item.positions[str(position)] for item in self.list() if item.positions[str(position)] is not None})

    def serial_options(self, position: int, timestamp: str) -> list[dict[str, str]]:
        """Classify unique documented serials relative to one event timestamp."""
        if position not in STACK_POSITIONS:
            raise PositionHistoryValidationError("position must be between 1 and 6")
        target = normalize_utc_timestamp(timestamp, "at")
        snapshots = self.list()
        effective = self.position_at(position, target)
        starts: dict[str, list[str]] = {}
        previous = object()
        for snapshot in snapshots:
            serial = snapshot.positions[str(position)]
            if serial != previous and serial is not None:
                starts.setdefault(serial, []).append(snapshot.effective_at)
            previous = serial

        result = []
        if effective is not None:
            result.append({"serial": effective, "relationship": "effective"})
        for serial in sorted(starts):
            if serial == effective:
                continue
            earlier = any(value <= target for value in starts[serial])
            later = any(value > target for value in starts[serial])
            relationship = "earlier_and_later" if earlier and later else "earlier" if earlier else "later"
            result.append({"serial": serial, "relationship": relationship})
        order = {"effective": 0, "earlier": 1, "earlier_and_later": 2, "later": 3}
        return sorted(result, key=lambda item: (order[item["relationship"]], item["serial"]))

    def divergence(self, observed: Mapping[int | str, str | None]) -> list[dict[str, Any]]:
        current = self.current()
        documented = current.positions if current else {str(item): None for item in STACK_POSITIONS}
        result = []
        for position in STACK_POSITIONS:
            if position not in observed and str(position) not in observed:
                continue
            actual = observed.get(position, observed.get(str(position)))
            actual = actual.strip() if isinstance(actual, str) and actual.strip() else None
            expected = documented[str(position)]
            if actual != expected:
                result.append({"module_number": position, "documented_serial": expected, "observed_serial": actual})
        return result


_OBSERVED_STACK: dict[int, str | None] = {}


def update_observed_stack(module_infos: Mapping[int, Mapping[str, Any]]) -> None:
    """Update ephemeral observations; this never writes documentary history."""
    global _OBSERVED_STACK
    _OBSERVED_STACK = {
        position: (str(info.get("barcode")).strip() if info.get("barcode") else None)
        for position, info in module_infos.items() if position in STACK_POSITIONS
    }


def observed_stack() -> dict[int, str | None]:
    return dict(_OBSERVED_STACK)
