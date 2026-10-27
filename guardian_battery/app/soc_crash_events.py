"""Small immutable SOC events and append-only, idempotent JSONL persistence."""
from __future__ import annotations

from research_budget import ResearchQueryError, check_budget, budget_lock, acquire_shared_file_lock

import fcntl
import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import InitVar, asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DEFAULT_SOC_CRASH_EVENTS = Path("/share/guardian_battery/soc_crash_events.jsonl")
LEGACY_DETECTOR_VERSION = "guardian_soc_crash_simple_v1"
DETECTOR_VERSION = "guardian_soc_gap_v1"
EVENT_COLLECTION_VERSION = "guardian_soc_events_v2"
MAX_EVENT_BYTES = 4096
LOG = logging.getLogger("guardian_battery.soc_crash_events")


def utc_timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds")


def _event_id(values):
    # Identity metadata cannot rename an existing observation.
    keys = ("detector_version", "observation_started_at", "candidate_module",
            "reference_module", "candidate_soc_at_start", "x_pp", "y_seconds")
    raw = json.dumps({key: values[key] for key in keys}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return "SCS-" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SocCrashEvent:
    event_id: str
    schema_version: int
    detector_version: str
    observation_started_at: str
    confirmed_at: str
    candidate_module: int
    candidate_physical_serial: str | None
    reference_module: int | None
    reference_physical_serial: str | None
    candidate_soc_at_start: float
    candidate_soc_at_confirmation: float
    observed_drop_pp: float
    x_pp: float
    y_seconds: int
    causality: str
    _persisted: InitVar[bool] = False

    def __post_init__(self, _persisted=False):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported event schema")
        if self.detector_version != LEGACY_DETECTOR_VERSION or self.causality != "not_determined":
            raise ValueError("invalid detector provenance or causality")
        modules = (self.candidate_module,) if self.reference_module is None else (
            self.candidate_module, self.reference_module)
        for module in modules:
            if type(module) is not int or not 1 <= module <= 6:
                raise ValueError("invalid event module")
        if self.candidate_module == self.reference_module:
            raise ValueError("candidate and reference must differ")
        if self.reference_module is None and self.reference_physical_serial is not None:
            raise ValueError("no reference serial without a reference module")
        for serial in (self.candidate_physical_serial, self.reference_physical_serial):
            if serial is not None and (not isinstance(serial, str) or not serial.strip()
                                       or len(serial) > 128):
                raise ValueError("invalid event serial")
        for value in (self.candidate_soc_at_start, self.candidate_soc_at_confirmation,
                      self.observed_drop_pp, self.x_pp):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("invalid event number")
        if not all(0 <= value <= 100 for value in (
                self.candidate_soc_at_start, self.candidate_soc_at_confirmation)):
            raise ValueError("invalid event SOC")
        if not 0.01 <= self.x_pp <= 100 or type(self.y_seconds) is not int or not 1 <= self.y_seconds <= 86400:
            raise ValueError("invalid event parameters")
        start, end = (datetime.fromisoformat(value) for value in (
            self.observation_started_at, self.confirmed_at))
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("event timestamps require timezone")
        if not 0 <= (end - start).total_seconds() <= self.y_seconds:
            raise ValueError("event outside observation window")
        drop = float(Decimal(str(self.candidate_soc_at_start)) -
                     Decimal(str(self.candidate_soc_at_confirmation)))
        if (drop != self.observed_drop_pp or drop < self.x_pp
                or (not _persisted and drop == self.x_pp)):
            raise ValueError("event does not meet SOC threshold")
        if self.event_id != _event_id(asdict(self)):
            raise ValueError("event ID mismatch")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_persisted(cls, values):
        """Read the historical >= contract; never reinterpret it as new detection.

        The keyword is supplied here, not accepted from the stored payload.
        All other validation and the original ID check remain active.
        """
        return cls(**values, _persisted=True)


def make_event(start, reference, current, x_pp, y_seconds):
    values = dict(schema_version=1, detector_version=LEGACY_DETECTOR_VERSION,
        observation_started_at=utc_timestamp(start.timestamp),
        confirmed_at=utc_timestamp(current.timestamp), candidate_module=start.module,
        candidate_physical_serial=start.physical_serial,
        reference_module=reference.module if reference is not None else None,
        reference_physical_serial=reference.physical_serial if reference is not None else None,
        candidate_soc_at_start=float(start.soc), candidate_soc_at_confirmation=float(current.soc),
        observed_drop_pp=float(Decimal(str(start.soc)) - Decimal(str(current.soc))),
        x_pp=float(x_pp), y_seconds=y_seconds, causality="not_determined")
    return SocCrashEvent(event_id=_event_id(values), **values)


def _gap_event_id(values):
    # One immutable event per versioned candidate episode; moving references and
    # documentary identity do not rename that episode.
    keys = ("detector_version", "observation_started_at", "candidate_module",
            "candidate_soc_at_start", "x_pp", "y_seconds")
    raw = json.dumps({key: values[key] for key in keys}, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return "SCS-" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SocGapEvent:
    event_id: str
    schema_version: int
    detector_version: str
    observation_started_at: str
    confirmed_at: str
    candidate_module: int
    candidate_physical_serial: str | None
    previous_weakest_module: int | None
    previous_weakest_physical_serial: str | None
    candidate_soc_at_start: float
    candidate_soc_at_confirmation: float
    reference_modules_at_confirmation: tuple[int, ...]
    reference_serials_at_confirmation: tuple[str | None, ...]
    second_lowest_soc_at_confirmation: float
    observed_gap_pp: float
    x_pp: float
    y_seconds: int
    causality: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("unsupported gap event schema")
        if self.detector_version != DETECTOR_VERSION or self.causality != "not_determined":
            raise ValueError("invalid gap detector provenance or causality")
        refs = self.reference_modules_at_confirmation
        if (not isinstance(refs, tuple) or not 1 <= len(refs) <= 5
                or any(type(m) is not int or not 1 <= m <= 6 for m in refs)
                or tuple(sorted(set(refs))) != refs):
            raise ValueError("reference modules must be unique sorted positions")
        modules = (self.candidate_module,) if self.previous_weakest_module is None else (
            self.candidate_module, self.previous_weakest_module)
        for m in modules:
            if type(m) is not int or not 1 <= m <= 6:
                raise ValueError("invalid gap event module")
        if self.candidate_module in refs or self.candidate_module == self.previous_weakest_module:
            raise ValueError("candidate cannot be its own reference")
        serials = self.reference_serials_at_confirmation
        if not isinstance(serials, tuple) or len(serials) != len(refs):
            raise ValueError("reference identities must match reference modules")
        for serial in (self.candidate_physical_serial, self.previous_weakest_physical_serial, *serials):
            if serial is not None and (not isinstance(serial, str) or not serial.strip() or len(serial) > 128):
                raise ValueError("invalid documented serial")
        if self.previous_weakest_module is None and self.previous_weakest_physical_serial is not None:
            raise ValueError("no previous reference serial without a module")
        for value in (self.candidate_soc_at_start, self.candidate_soc_at_confirmation,
                      self.second_lowest_soc_at_confirmation, self.observed_gap_pp, self.x_pp):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("invalid gap event number")
        if not all(0 <= value <= 100 for value in (self.candidate_soc_at_start,
                self.candidate_soc_at_confirmation, self.second_lowest_soc_at_confirmation)):
            raise ValueError("invalid event SOC")
        if not 0.01 <= self.x_pp <= 100 or type(self.y_seconds) is not int or not 1 <= self.y_seconds <= 86400:
            raise ValueError("invalid gap event parameters")
        start, end = (datetime.fromisoformat(v) for v in (self.observation_started_at, self.confirmed_at))
        if start.tzinfo is None or end.tzinfo is None or not 0 <= (end-start).total_seconds() <= self.y_seconds:
            raise ValueError("event outside observation window")
        gap = float(Decimal(str(self.second_lowest_soc_at_confirmation)) -
                    Decimal(str(self.candidate_soc_at_confirmation)))
        if gap != self.observed_gap_pp or Decimal(str(gap)) < Decimal(str(self.x_pp)):
            raise ValueError("event does not meet SOC gap threshold")
        if self.event_id != _gap_event_id(asdict(self)):
            raise ValueError("event ID mismatch")

    def to_dict(self):
        values = asdict(self)
        for key in ("reference_modules_at_confirmation", "reference_serials_at_confirmation"):
            values[key] = list(values[key])
        return values

    @classmethod
    def from_persisted(cls, values):
        values = dict(values)
        for key in ("reference_modules_at_confirmation", "reference_serials_at_confirmation"):
            if not isinstance(values.get(key), list):
                raise ValueError("reference arrays required")
            values[key] = tuple(values[key])
        return cls(**values)


def make_gap_event(start, previous, current, references, x_pp, y_seconds):
    references = sorted(references, key=lambda sample: sample.module)
    if not references or current.module != start.module:
        raise ValueError("current candidate and references required")
    second_soc = references[0].soc
    if any(sample.timestamp != current.timestamp or sample.soc != second_soc for sample in references):
        raise ValueError("references require one timestamp and the same second minimum")
    values = dict(schema_version=2, detector_version=DETECTOR_VERSION,
        observation_started_at=utc_timestamp(start.timestamp), confirmed_at=utc_timestamp(current.timestamp),
        candidate_module=start.module, candidate_physical_serial=start.physical_serial,
        previous_weakest_module=previous.module if previous else None,
        previous_weakest_physical_serial=previous.physical_serial if previous else None,
        candidate_soc_at_start=float(start.soc), candidate_soc_at_confirmation=float(current.soc),
        reference_modules_at_confirmation=tuple(s.module for s in references),
        reference_serials_at_confirmation=tuple(s.physical_serial for s in references),
        second_lowest_soc_at_confirmation=float(second_soc),
        observed_gap_pp=float(Decimal(str(second_soc))-Decimal(str(current.soc))),
        x_pp=float(x_pp), y_seconds=y_seconds, causality="not_determined")
    return SocGapEvent(event_id=_gap_event_id(values), **values)


def read_soc_event(values):
    if not isinstance(values, dict):
        raise ValueError("event object required")
    if values.get("schema_version") == 1:
        return SocCrashEvent.from_persisted(values)
    if values.get("schema_version") == 2:
        return SocGapEvent.from_persisted(values)
    raise ValueError("unsupported event schema")


class SocCrashEventStore:
    """Index only this small event log, incrementally; never open raw history.

    Construction and reads create no files. Complete invalid lines are preserved
    and skipped with a warning. An incomplete tail is invisible until terminated;
    append terminates it without changing any existing byte. Conflicting valid
    revisions fail closed. A separate lock serializes cross-process appends.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._events: dict[str, SocCrashEvent | SocGapEvent] = {}
        self._offset = 0
        self._inode = None

    def _refresh(self):
        check_budget()
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            if self._offset:
                raise OSError("SOC event log disappeared")
            return
        with handle:
            acquire_shared_file_lock(handle)
            stat = os.fstat(handle.fileno())
            inode = (stat.st_dev, stat.st_ino)
            if self._inode not in (None, inode) or stat.st_size < self._offset:
                raise OSError("SOC event log replaced or truncated")
            self._inode = inode
            handle.seek(self._offset)
            while True:
                check_budget()
                raw = handle.readline(MAX_EVENT_BYTES + 1)
                if len(raw) > MAX_EVENT_BYTES:
                    raise OSError("SOC event record exceeds size limit")
                if not raw or not raw.endswith(b"\n"):
                    break
                try:
                    event = read_soc_event(json.loads(raw))
                except ResearchQueryError:
                    raise
                except (TypeError, ValueError, UnicodeError):
                    LOG.warning("Invalid SOC event record ignored at byte %s", self._offset)
                else:
                    existing = self._events.get(event.event_id)
                    if existing is not None and existing != event:
                        raise OSError("conflicting SOC event revision")
                    self._events[event.event_id] = event
                self._offset = handle.tell()

    def append(self, event: SocCrashEvent | SocGapEvent) -> bool:
        if not isinstance(event, (SocCrashEvent, SocGapEvent)):
            raise TypeError("versioned SOC event required")
        # Historical records may be read, but cannot bypass the new write rule.
        event.__post_init__()
        raw = (json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode()
        if len(raw) > MAX_EVENT_BYTES:
            raise ValueError("SOC event record exceeds size limit")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_name(self.path.name + ".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                # Recover a complete JSON object whose newline was lost before
                # deduplication, without rewriting any existing byte.
                with self.path.open("a+b") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    handle.seek(0, os.SEEK_END)
                    if handle.tell():
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n":
                            handle.write(b"\n")
                            handle.flush()
                            os.fsync(handle.fileno())
                self._refresh()
                existing = self._events.get(event.event_id)
                if existing is not None:
                    if existing != event:
                        raise ValueError("SOC event ID already has a different immutable payload")
                    # Also make an append with an earlier uncertain fsync durable.
                    with self.path.open("rb") as handle:
                        os.fsync(handle.fileno())
                    return False
                with self.path.open("a+b") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._refresh()
                return True

    def get(self, event_id):
        check_budget()
        with budget_lock(self._lock):
            self._refresh()
            return self._events.get(event_id)

    def list(self, start, end, max_records):
        check_budget()
        with budget_lock(self._lock):
            self._refresh()
            events = sorted((event for event in self._events.values()
                if start <= datetime.fromisoformat(event.confirmed_at) <= end),
                key=lambda event: (event.confirmed_at, event.event_id))
            return events[:max_records], len(events) > max_records
