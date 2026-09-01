"""Append-only provenance for physical module serials at stack positions."""

from __future__ import annotations

import fcntl
import json
import os
import time
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
            ordered = sorted(
                records,
                key=lambda item: (item.effective_at, item.created_at,
                                  item.position_history_id),
            )
            latest = ordered[-1] if ordered else None
            actual = latest.position_history_id if latest else None
            if expected_latest_snapshot_id != actual:
                raise PositionHistoryConflictError(expected_latest_snapshot_id, actual)
            same_time = [item for item in records if item.effective_at == effective]
            if any(item.positions != snapshot.positions for item in same_time):
                raise PositionHistoryValidationError(
                    "conflicting position mapping at identical effective_at"
                )
            if any(item.positions == snapshot.positions for item in same_time):
                return same_time[-1]
            previous = [item for item in ordered if item.effective_at < effective]
            if previous and previous[-1].positions == snapshot.positions:
                raise PositionHistoryValidationError(
                    "positions are unchanged at effective_at"
                )
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

    def last_documented_serials(self) -> dict[str, str | None]:
        """Retain documentary identity labels independently of current occupancy."""
        result = {str(position): None for position in STACK_POSITIONS}
        for snapshot in self.list():
            for position, serial in snapshot.positions.items():
                if serial is not None:
                    result[position] = serial
        return result

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

    def serial_histories(self) -> list[dict[str, Any]]:
        """Project immutable snapshots into physical-module position intervals."""
        snapshots = self.list()
        serials = sorted({serial for item in snapshots for serial in item.positions.values() if serial})
        result = []
        for serial in serials:
            intervals = []
            first_known = next(index for index, snapshot in enumerate(snapshots)
                               if serial in snapshot.positions.values())
            for index in range(first_known, len(snapshots)):
                snapshot = snapshots[index]
                position = next((int(key) for key, value in snapshot.positions.items() if value == serial), None)
                end = snapshots[index + 1].effective_at if index + 1 < len(snapshots) else None
                if intervals and intervals[-1]["position"] == position:
                    intervals[-1]["valid_to"] = end
                else:
                    intervals.append({"valid_from": snapshot.effective_at, "valid_to": end,
                                      "position": position,
                                      "maintenance_event_id": snapshot.maintenance_event_id})
            result.append({"serial": serial, "intervals": intervals})
        return result

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
_OBSERVATION_CANDIDATES: dict[int, tuple[str, int]] = {}
_PRESENCE_SOURCES: dict[int, dict[str, dict[str, Any]]] = {}
_MISSING_CANDIDATES: dict[int, tuple[int, float]] = {}
_COMMUNICATION_HEALTHY: bool | None = None
_EXPECTED_MODULE_COUNT = 6
OBSERVATION_CONFIRMATIONS = 3
PRESENCE_FRESHNESS_SECONDS = 90.0
ABSENCE_CONFIRMATIONS = 3
ABSENCE_MIN_SECONDS = 30.0


def missing_expected_positions(module_count: int, observed_positions) -> list[int]:
    """Compare live module positions only with the configured expected topology."""
    expected = set(range(1, max(1, min(6, int(module_count))) + 1))
    observed = {int(position) for position in observed_positions}
    return sorted(expected - observed)


def update_observed_stack(module_infos: Mapping[int, Mapping[str, Any]], *,
                          present_positions: set[int] | None = None,
                          communication_healthy: bool = True,
                          expected_module_count: int = 6,
                          observed_at: float | None = None) -> None:
    """Update current Console presence without treating cached INFO as live data."""
    global _OBSERVED_STACK, _OBSERVATION_CANDIDATES, _PRESENCE_SOURCES
    global _MISSING_CANDIDATES, _COMMUNICATION_HEALTHY, _EXPECTED_MODULE_COUNT
    now = time.time() if observed_at is None else float(observed_at)
    _COMMUNICATION_HEALTHY = bool(communication_healthy)
    _EXPECTED_MODULE_COUNT = max(1, min(6, int(expected_module_count)))
    if not communication_healthy:
        return
    positions = ({int(value) for value in present_positions}
                 if present_positions is not None else {int(value) for value in module_infos})
    observed_any = bool(positions)
    for position in positions:
        if position not in STACK_POSITIONS:
            continue
        info = module_infos.get(position, {})
        serial = str(info.get("barcode")).strip() if info.get("barcode") else None
        if not serial:
            continue
        _PRESENCE_SOURCES.setdefault(position, {})["console"] = {
            "identity": serial, "timestamp": now, "source": "console"
        }
        _MISSING_CANDIDATES.pop(position, None)
        candidate, count = _OBSERVATION_CANDIDATES.get(position, ("", 0))
        count = count + 1 if candidate == serial else 1
        _OBSERVATION_CANDIDATES[position] = (serial, count)
        if count >= OBSERVATION_CONFIRMATIONS:
            _OBSERVED_STACK[position] = serial
    if not observed_any:
        return
    for position in range(1, _EXPECTED_MODULE_COUNT + 1):
        if position in positions:
            continue
        count, since = _MISSING_CANDIDATES.get(position, (0, now))
        _MISSING_CANDIDATES[position] = (count + 1, since)
        if count + 1 >= ABSENCE_CONFIRMATIONS and now - since >= ABSENCE_MIN_SECONDS:
            _OBSERVED_STACK[position] = None


def update_rs485_observations(observations: Mapping[int, Mapping[str, Any]], *,
                              observed_at: float | None = None) -> None:
    """Merge time-aware 0x93 identities already resolved through position history."""
    global _OBSERVED_STACK, _OBSERVATION_CANDIDATES, _PRESENCE_SOURCES
    now = time.time() if observed_at is None else float(observed_at)
    for value in observations.values():
        position = value.get("position")
        serial = value.get("serial_string")
        timestamp = value.get("timestamp", now)
        if position not in STACK_POSITIONS or not isinstance(serial, str) or not serial:
            continue
        sources = _PRESENCE_SOURCES.setdefault(int(position), {})
        previous = sources.get("rs485_0x93")
        sample_is_new = (previous is None
                         or float(timestamp) > float(previous["timestamp"])
                         or serial != previous["identity"])
        if previous is not None and float(timestamp) < float(previous["timestamp"]):
            continue
        sources["rs485_0x93"] = {
            "identity": serial, "timestamp": float(timestamp), "source": "rs485_0x93"
        }
        if now - float(timestamp) <= PRESENCE_FRESHNESS_SECONDS:
            _MISSING_CANDIDATES.pop(int(position), None)
            if not sample_is_new:
                continue
            candidate, count = _OBSERVATION_CANDIDATES.get(int(position), ("", 0))
            count = count + 1 if candidate == serial else 1
            _OBSERVATION_CANDIDATES[int(position)] = (serial, count)
            if count >= OBSERVATION_CONFIRMATIONS:
                _OBSERVED_STACK[int(position)] = serial


def observed_stack() -> dict[int, str | None]:
    return dict(_OBSERVED_STACK)


def current_presence(*, now: float | None = None,
                     freshness_seconds: float = PRESENCE_FRESHNESS_SECONDS,
                     expected_module_count: int | None = None) -> dict[int, dict[str, Any]]:
    """Project expected topology and live observations without rewriting history."""
    target = time.time() if now is None else float(now)
    expected_count = (_EXPECTED_MODULE_COUNT if expected_module_count is None
                      else max(1, min(6, int(expected_module_count))))
    result = {}
    for position in STACK_POSITIONS:
        sources = list(_PRESENCE_SOURCES.get(position, {}).values())
        fresh = [item for item in sources
                 if target - float(item["timestamp"]) <= float(freshness_seconds)]
        expected = position <= expected_count
        confirmed_absent = _OBSERVED_STACK.get(position, object()) is None
        if fresh:
            latest = max(fresh, key=lambda item: float(item["timestamp"]))
            status, serial = "present", latest["identity"]
        elif not expected:
            status, serial = "not_expected", None
        elif _COMMUNICATION_HEALTHY is not True:
            status, serial = "unknown", None
        elif confirmed_absent:
            status, serial = "absent", None
        elif sources:
            status, serial = "stale", None
        else:
            status, serial = "unknown", None
        result[position] = {
            "position": position, "expected": expected, "status": status,
            "observed_serial": serial,
            "last_observed_at": max((float(item["timestamp"]) for item in sources), default=None),
            "sources": sorted({item["source"] for item in fresh}),
        }
    return result


def project_live_topology(documented: Mapping[str, str | None] | None = None, **kwargs) -> dict[int, dict[str, Any]]:
    """Combine documentary identity with the single current-presence truth."""
    serials = documented or {}
    return {
        position: {
            "position": position,
            "expected": value["expected"],
            "presence_status": value["status"],
            "physical_serial": serials.get(str(position)),
            "currently_observed_serial": value["observed_serial"],
            "last_seen": value["last_observed_at"],
            "source": value["sources"],
        }
        for position, value in current_presence(**kwargs).items()
    }


def stable_observed_changes(documented: Mapping[str, str | None] | None) -> dict[int, str | None]:
    """Return only confirmed additions, replacements, and removals."""
    expected = documented or {str(position): None for position in STACK_POSITIONS}
    return {position: serial for position, serial in _OBSERVED_STACK.items()
            if expected.get(str(position)) != serial}


def classify_stack_change(before: Mapping[str, str | None] | None,
                          after: Mapping[str, str | None], *,
                          confirmed_empty_positions: set[str] | None = None) -> dict[str, str]:
    """Describe a confirmed full-stack transition without guessing its cause."""
    previous = dict(before or {str(position): None for position in STACK_POSITIONS})
    current = dict(after)
    changed = [key for key in previous if previous[key] != current[key]]
    known_before = {serial for serial in previous.values() if serial}
    known_after = {serial for serial in current.values() if serial}
    added = known_after - known_before
    removed = known_before - known_after
    identified = [key for key in changed if previous[key] is None and current[key] is not None]

    proven_empty = confirmed_empty_positions or set()
    if identified and not removed and not all(key in proven_empty for key in identified):
        return {"kind": "initial_identification", "category": "module_identification",
                "title": "Erstidentifikation der Stackbelegung"}
    if known_before == known_after and changed:
        return {"kind": "position_change", "category": "module_position_change",
                "title": "Bestätigte Positionsänderung"}
    if added and removed:
        return {"kind": "module_replacement", "category": "module_replacement",
                "title": "Bestätigter Modulaustausch"}
    if added and identified and all(key in proven_empty for key in identified):
        return {"kind": "module_added", "category": "module_added",
                "title": "Modul hinzugefügt"}
    if removed:
        return {"kind": "module_removed", "category": "module_removed",
                "title": "Modul entfernt"}
    return {"kind": "stack_assignment_change", "category": "module_position_change",
            "title": "Bestätigte Änderung der Stackbelegung"}


def documented_identity_at(path: Path | str, position: int, timestamp: datetime | str) -> tuple[str | None, str | None]:
    """Resolve only documentary identity; legacy/unknown history stays unknown."""
    target = normalize_utc_timestamp(timestamp, "timestamp")
    snapshots = sorted(PositionHistoryLog(path).read_all(), key=lambda item: (item.effective_at, item.created_at, item.position_history_id))
    matches = [item for item in snapshots if item.effective_at <= target]
    if not matches: return None, None
    snapshot = matches[-1]
    return snapshot.positions[str(position)], snapshot.position_history_id


def documented_position_at(path: Path | str, serial: str,
                           timestamp: datetime | str) -> tuple[int | None, str | None]:
    """Resolve a physical serial only in the snapshot effective at timestamp."""
    target = normalize_utc_timestamp(timestamp, "timestamp")
    snapshots = sorted(PositionHistoryLog(path).read_all(),
                       key=lambda item: (item.effective_at, item.created_at,
                                         item.position_history_id))
    matches = [item for item in snapshots if item.effective_at <= target]
    if not matches:
        return None, None
    snapshot = matches[-1]
    positions = [int(position) for position, value in snapshot.positions.items()
                 if value == serial]
    if len(positions) != 1:
        return None, snapshot.position_history_id
    return positions[0], snapshot.position_history_id


def resolve_maintenance_event_identities(events, path: Path | str) -> list[dict[str, Any]]:
    """Resolve event identity at occurred_at without using today's position."""
    result = []
    for event in events:
        raw = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        serial = raw.get("module_serial")
        identity_status = "explicit" if serial else "unknown"
        if not serial and raw.get("module_number") is not None:
            try:
                serial, _snapshot_id = documented_identity_at(
                    path, int(raw["module_number"]), raw["occurred_at"]
                )
                identity_status = "position_history" if serial else "unknown"
            except Exception:
                serial = None
                identity_status = "unknown"
        raw["resolved_module_serial"] = serial
        raw["identity_status"] = identity_status
        result.append(raw)
    return result
