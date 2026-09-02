"""Deterministic offline evidence for observed BMS management transitions.

The analyzer consumes already persisted passive RS485 and cell-history records.
It does not communicate with a BMS and does not assign causal meaning to the
observations it correlates.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

from rs485_evidence import decode_identity_record


SCHEMA_VERSION = 1
CAUSALITY = "not_determined"


@dataclass(frozen=True)
class EvidenceParameters:
    """Guardian analysis parameters; none are Pylontech thresholds."""

    cell_high_age_seconds: float = 15.0
    cell_medium_age_seconds: float = 60.0
    cell_low_age_seconds: float = 120.0
    transition_window_seconds: float = 15.0
    near_zero_current_a: float = 0.5
    stack_sync_seconds: float = 5.0
    peer_cycle_seconds: float = 10.0
    management_gap_seconds: float = 120.0
    daily_timezone: str = "Europe/Berlin"
    trend_min_days: int = 3
    trend_relative_tolerance: float = 0.05
    trend_min_management_coverage_seconds: float = 1.0


class BmsManagementEvidenceStore:
    """Explicit append-only event store plus replaceable derived aggregates.

    No production path is implicit: a scheduled/offline caller must supply both
    destinations. Existing RS485, cell and position histories are never opened.
    """

    def __init__(self, event_path: Path | str, aggregate_path: Path | str):
        self.event_path = Path(event_path)
        self.aggregate_path = Path(aggregate_path)

    def append_events(self, events: Iterable[Mapping]) -> int:
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.event_path.with_suffix(self.event_path.suffix + ".lock")
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                original = self.event_path.read_bytes() if self.event_path.exists() else b""
                if original and not original.endswith(b"\n"):
                    raise ValueError("event store has a crash-truncated final line")
                existing = set()
                for line in original.splitlines():
                    try:
                        existing.add(json.loads(line)["event_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
                pending, pending_ids = [], set()
                for item in events:
                    event_id = item.get("event_id")
                    if not event_id or event_id in existing or event_id in pending_ids:
                        continue
                    pending.append(dict(item))
                    pending_ids.add(event_id)
                if not pending:
                    return 0
                additions = b"".join((json.dumps(
                    item, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
                    ).encode("utf-8") for item in pending)
                temporary = self.event_path.with_suffix(self.event_path.suffix + ".tmp")
                with temporary.open("wb") as handle:
                    handle.write(original)
                    handle.write(additions)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.event_path)
                return len(pending)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def save_daily_aggregates(self, aggregates: Iterable[Mapping]) -> None:
        self.aggregate_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.aggregate_path.with_suffix(self.aggregate_path.suffix + ".tmp")
        payload = {"schema_version": SCHEMA_VERSION,
                   "aggregates": sorted((dict(item) for item in aggregates),
                                        key=lambda item: (item["day"], item["physical_serial"]))}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                        sort_keys=True), encoding="utf-8")
        temporary.replace(self.aggregate_path)


def _timestamp(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    raise ValueError("timestamp must be numeric, ISO text, or datetime")


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _median(values):
    return statistics.median(values) if values else None


def _observed_union_duration(events: Sequence[Mapping], *, day_bounds=None,
                             coverage_intervals=()) -> float:
    intervals = []
    for item in events:
        end_value = item.get("observed_end") or item.get("observed_through")
        if end_value is None:
            continue
        start, end = _timestamp(item["observed_start"]), _timestamp(end_value)
        if day_bounds is not None:
            start, end = max(start, day_bounds[0]), min(end, day_bounds[1])
        if end > start:
            intervals.append((start, end))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    if not coverage_intervals:
        return 0.0
    return sum(max(0.0, min(end, coverage_end) - max(start, coverage_start))
               for start, end in merged
               for coverage_start, coverage_end in coverage_intervals)


def _event_id(serial: str, timestamp: float) -> str:
    material = f"{SCHEMA_VERSION}|dcl_zero|{serial}|{_iso(timestamp)}".encode()
    return "BME-" + hashlib.sha256(material).hexdigest()[:24]


def _limit_event_id(event_type: str, serial: str, timestamp: float) -> str:
    if event_type == "dcl_zero":
        return _event_id(serial, timestamp)  # preserve V1 IDs
    material = f"{SCHEMA_VERSION}|{event_type}|{serial}|{_iso(timestamp)}".encode()
    return "BME-" + hashlib.sha256(material).hexdigest()[:24]


def _valid_frame(record: Mapping, command: int) -> bool:
    return (record.get("record_type") == "frame"
            and record.get("direction") == "response"
            and record.get("paired_command") == command
            and record.get("checksum_valid") is True
            and record.get("frame_complete") is True
            and record.get("request_matched") is True)


def _cell_metrics(record: Mapping) -> dict | None:
    try:
        voltages = [float(value) for value in record["voltages_mv"]]
        if len(voltages) != 15:
            return None
        median = float(statistics.median(voltages))
        minimum, maximum = min(voltages), max(voltages)
        deviations = [value - median for value in voltages]
        lowest_cells = [index + 1 for index, value in enumerate(voltages)
                        if value == minimum]
        worst_value = min(deviations)
        worst_cells = [index + 1 for index, value in enumerate(deviations)
                       if value == worst_value]
        lowest = lowest_cells[0]
        worst = worst_cells[0]
        timestamp = _timestamp(record["timestamp"])
    except (KeyError, TypeError, ValueError, statistics.StatisticsError):
        return None
    return {
        "timestamp": timestamp,
        "module": record.get("module"),
        "physical_serial": record.get("module_serial") or record.get("physical_serial"),
        "position_history_id": record.get("position_history_id"),
        "soc_percent": record.get("soc_percent"),
        "module_voltage_v": sum(voltages) / 1000.0,
        "module_current_a": record.get("current_a"),
        "cell_voltages_mv": voltages,
        "min_cell_voltage_mv": minimum,
        "min_cell_number": lowest,
        "min_cell_numbers": lowest_cells,
        "min_cell_is_unique": len(lowest_cells) == 1,
        "max_cell_voltage_mv": maximum,
        "max_cell_number": voltages.index(maximum) + 1,
        "spread_mv": maximum - minimum,
        "module_median_mv": median,
        "worst_negative_cell": worst,
        "worst_negative_cells": worst_cells,
        "worst_negative_is_unique": len(worst_cells) == 1,
        "worst_negative_deviation_mv": worst_value,
        "per_cell_deviation_mv": deviations,
        "temperatures_c": record.get("temperatures_c"),
        "balancing": record.get("balancing"),
        "_tie_breaker": hashlib.sha256(json.dumps(
            dict(record), sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest(),
    }


def _cell_quality(age: float, parameters: EvidenceParameters) -> str:
    if age <= parameters.cell_high_age_seconds:
        return "high"
    if age <= parameters.cell_medium_age_seconds:
        return "medium"
    if age <= parameters.cell_low_age_seconds:
        return "low"
    return "unavailable"


def _management_values(record: Mapping) -> dict | None:
    if not _valid_frame(record, 0x92):
        return None
    decoded = record.get("decoded")
    if not isinstance(decoded, Mapping):
        return None
    try:
        return {
            "dcl": float(decoded["discharge_current_limit_a"]),
            "discharge_enable": bool(decoded.get("discharge_enable")),
            "ccl": float(decoded["charge_current_limit_a"]),
            "charge_enable": bool(decoded.get("charge_enable")),
            "cvl": decoded.get("charge_voltage_limit_v"),
            "dvl": decoded.get("discharge_voltage_limit_v"),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _position(record: Mapping, serial: str, timestamp: float,
              resolver: Callable[[str, str], object] | None):
    if resolver is None:
        return record.get("position"), record.get("position_history_id")
    resolved = resolver(serial, _iso(timestamp))
    if isinstance(resolved, tuple):
        return (resolved + (None, None))[:2]
    if isinstance(resolved, Mapping):
        return resolved.get("position"), resolved.get("position_history_id")
    return resolved, None


class BmsManagementEvidenceAnalyzer:
    """Correlate passive observations without changing diagnostic state."""

    def __init__(self, parameters: EvidenceParameters | None = None):
        self.parameters = parameters or EvidenceParameters()

    def analyze(self, rs485_records: Iterable[Mapping], cell_records: Iterable[Mapping],
                *, position_resolver: Callable[[str, str], object] | None = None) -> dict:
        rs485 = self._prepare_rs485(rs485_records, position_resolver)
        cells = self._prepare_cells(cell_records)
        transitions = self._transitions_0x44(rs485["raw_44"])
        events = self._events(rs485["management"], cells, transitions)
        relative_limits = self._relative_limits(rs485["management"])
        aggregates = self.daily_aggregates(events, rs485["management"], relative_limits)
        return {
            "schema_version": SCHEMA_VERSION,
            "parameters": self.parameters.__dict__,
            "events": events,
            "daily_aggregates": aggregates,
            "relative_limits": relative_limits,
            "causality": CAUSALITY,
        }

    def _prepare_rs485(self, records, position_resolver):
        identities: dict[int, dict] = {}
        management, raw_44 = [], []
        sortable = []
        for record in records:
            try:
                sortable.append((_timestamp(record["timestamp"]), dict(record)))
            except (KeyError, TypeError, ValueError):
                continue
        for timestamp, record in sorted(sortable, key=lambda item: item[0]):
            identity = decode_identity_record(record)
            if identity is not None:
                identities[int(identity["adr"])] = identity
                continue
            try:
                adr = int(record["adr"])
            except (KeyError, TypeError, ValueError):
                continue
            serial = record.get("physical_serial")
            if not serial and adr in identities:
                serial = identities[adr]["serial_string"]
            if _valid_frame(record, 0x44):
                try:
                    raw_44.append({"timestamp": timestamp, "adr": adr,
                                   "info": bytes.fromhex(record["info_raw"]),
                                   "source_frame_reference": record.get("source_frame_reference")})
                except (KeyError, TypeError, ValueError):
                    pass
                continue
            values = _management_values(record)
            if values is None or not serial:
                continue
            position, history_id = _position(record, serial, timestamp, position_resolver)
            management.append({
                "timestamp": timestamp, "physical_serial": serial, "adr": adr,
                "position_at_time": position, "position_history_id": history_id,
                "source_frame_reference": record.get("source_frame_reference"),
                "source": record.get("source", "rs485_passive"), **values,
            })
        return {"management": management, "raw_44": raw_44}

    @staticmethod
    def _prepare_cells(records):
        result = defaultdict(list)
        for record in records:
            metrics = _cell_metrics(record)
            if metrics is not None and metrics["physical_serial"]:
                result[metrics["physical_serial"]].append(metrics)
        for rows in result.values():
            rows.sort(key=lambda item: (item["timestamp"], item["_tie_breaker"]))
        return result

    @staticmethod
    def _transitions_0x44(records):
        previous, result = {}, []
        for record in sorted(records, key=lambda item: item["timestamp"]):
            adr, current = record["adr"], record["info"]
            old = previous.get(adr)
            if old is not None:
                for offset in range(max(len(old["info"]), len(current))):
                    before = old["info"][offset] if offset < len(old["info"]) else None
                    after = current[offset] if offset < len(current) else None
                    if before != after:
                        result.append({"timestamp": record["timestamp"], "adr": adr,
                                       "offset": offset,
                                       "old_hex": None if before is None else f"{before:02X}",
                                       "new_hex": None if after is None else f"{after:02X}",
                                       "source_frame_reference": record["source_frame_reference"]})
            previous[adr] = record
        return result

    def _events(self, management, cells, transitions):
        by_serial = defaultdict(list)
        for item in management:
            by_serial[item["physical_serial"]].append(item)
        events = []
        for serial, rows in sorted(by_serial.items()):
            rows.sort(key=lambda item: item["timestamp"])
            previous = None
            open_events = {"ccl": [], "dcl": []}
            for row in rows:
                if previous is not None:
                    for limit in ("ccl", "dcl"):
                        magnitude = self._limit_magnitude(limit, row[limit])
                        for event in list(open_events[limit]):
                            if magnitude >= event["limit_before_magnitude_a"]:
                                event["observed_end"] = _iso(row["timestamp"])
                                event["observed_duration_seconds"] = (
                                    row["timestamp"] - _timestamp(event["observed_start"]))
                                event["observed_restricted_duration_seconds"] = event[
                                    "observed_duration_seconds"]
                                event["recovery_limit_a"] = row[limit]
                                event[f"recovery_{limit}"] = row[limit]
                                event["recovery_source_frame_reference"] = row["source_frame_reference"]
                                event["recovery_transition_0x44"] = self._transition_context(
                                    row["timestamp"], row["adr"], transitions)
                                open_events[limit].remove(event)
                        before = self._limit_magnitude(limit, previous[limit])
                        after = self._limit_magnitude(limit, row[limit])
                        if after < before:
                            event = self._new_event(previous, row, limit,
                                                    cells.get(serial, []), cells, transitions)
                            events.append(event)
                            open_events[limit].append(event)
                    for limit, pending in open_events.items():
                        magnitude = self._limit_magnitude(limit, row[limit])
                        for event in pending:
                            if magnitude <= event["limit_after_magnitude_a"]:
                                event["observed_poll_count"] += 1
                                event["observed_through"] = _iso(row["timestamp"])
                                event["observed_restricted_duration_seconds"] = (
                                    row["timestamp"] - _timestamp(event["observed_start"]))
                                if event["event_type"] == "dcl_zero":
                                    event["zero_poll_count"] += 1
                previous = row
        return sorted(events, key=lambda item: (item["timestamp"], item["physical_serial"]))

    @staticmethod
    def _limit_magnitude(limit, value):
        value = float(value)
        return abs(value) if limit == "dcl" else value

    def _new_event(self, previous, row, limit, serial_cells, all_cells, transitions):
        timestamp = row["timestamp"]
        before = [item for item in serial_cells if item["timestamp"] < timestamp]
        context = before[-1] if before else None
        context_age = timestamp - context["timestamp"] if context else None
        quality = (_cell_quality(context_age, self.parameters) if context is not None
                   else "unavailable")
        usable = context if quality != "unavailable" else None
        following = [item for item in serial_cells if item["timestamp"] > timestamp]
        current = self._current_context(timestamp, usable, following)
        transition = self._transition_context(timestamp, row["adr"], transitions)
        stack = self._stack_current(timestamp, all_cells)
        dynamics = self._dynamics(timestamp, usable, before)
        before_magnitude = self._limit_magnitude(limit, previous[limit])
        after_magnitude = self._limit_magnitude(limit, row[limit])
        event_type = f"{limit}_zero" if after_magnitude == 0 else f"{limit}_reduction"
        enable = row["discharge_enable"] if limit == "dcl" else row["charge_enable"]
        event = {
            "schema_version": SCHEMA_VERSION, "event_type": event_type,
            "limit_type": limit, "event_id": _limit_event_id(
                event_type, row["physical_serial"], timestamp),
            "physical_serial": row["physical_serial"], "adr": row["adr"],
            "position_at_time": row["position_at_time"],
            "position_history_id": row["position_history_id"],
            "timestamp": _iso(timestamp), "observed_start": _iso(timestamp),
            "observed_end": None, "observed_through": _iso(timestamp),
            "observed_duration_seconds": None,
            "observed_restricted_duration_seconds": 0.0,
            "event_duration_basis": "management_transition_endpoints",
            "zero_poll_count": 0, "observed_poll_count": 0,
            "recovery_limit_a": None, "recovery_dcl": None, "recovery_ccl": None,
            "recovery_transition_0x44": None,
            "limit_before_a": previous[limit], "limit_after_a": row[limit],
            "limit_before_magnitude_a": before_magnitude,
            "limit_after_magnitude_a": after_magnitude,
            "restriction_increase_a": before_magnitude - after_magnitude,
            "dcl_before": previous["dcl"], "dcl_after": row["dcl"],
            "ccl_before": previous["ccl"], "ccl_after": row["ccl"],
            "discharge_enable": row["discharge_enable"],
            "charge_enable": row["charge_enable"], "ccl": row["ccl"],
            "cvl": row["cvl"], "dvl": row["dvl"],
            "dcl_zero_despite_enable": (event_type == "dcl_zero"
                                         and row["discharge_enable"] is True),
            "ccl_zero_despite_enable": (event_type == "ccl_zero"
                                         and row["charge_enable"] is True),
            "ccl_reduced_despite_enable": (limit == "ccl" and enable is True),
            "cell_context_quality": quality,
            "cell_sample_timestamp": _iso(usable["timestamp"]) if usable else None,
            "cell_sample_age_seconds": context_age if usable else None,
            "cell_context": self._public_cell(usable), "cell_dynamics": dynamics,
            "transition_0x44": transition, "current_response": current,
            "reconstructed_stack_current": stack,
            "source": "passive_rs485_and_cell_history",
            "source_frame_reference": row["source_frame_reference"],
            "provenance": {"management": "stored_decoded_0x92",
                           "identity": "stored_or_redecoded_0x93",
                           "cell": "same_physical_serial_previous_sample_only",
                           "position": "position_at_event_time"},
            "causality": CAUSALITY,
        }
        return event

    def _relative_limits(self, management):
        """Compare each module with peers observed in the same bounded cycle."""
        rows = sorted(management, key=lambda item: item["timestamp"])
        cycles, current = [], []
        for row in rows:
            if current and (row["timestamp"] - current[0]["timestamp"]
                            > self.parameters.peer_cycle_seconds
                            or any(item["physical_serial"] == row["physical_serial"]
                                   for item in current)):
                cycles.append(current)
                current = []
            current.append(row)
        if current:
            cycles.append(current)
        result = []
        for cycle_number, cycle in enumerate(cycles, 1):
            # Retain only the latest value per physical serial inside the cycle.
            unique = {item["physical_serial"]: item for item in cycle}
            values = list(unique.values())
            cycle_span = (max(item["timestamp"] for item in values)
                          - min(item["timestamp"] for item in values))
            for row in values:
                peers = [item for item in values
                         if item["physical_serial"] != row["physical_serial"]]
                observation = {"schema_version": SCHEMA_VERSION,
                    "cycle": cycle_number, "timestamp": _iso(row["timestamp"]),
                    "physical_serial": row["physical_serial"], "adr": row["adr"],
                    "position_at_time": row["position_at_time"],
                    "peer_count": len(peers), "peer_cycle_span_seconds": cycle_span,
                    "peer_context_quality": "available" if peers else "unavailable",
                    "peer_cycle_limit_seconds": self.parameters.peer_cycle_seconds,
                    "causality": CAUSALITY}
                for limit in ("ccl", "dcl"):
                    observed = self._limit_magnitude(limit, row[limit])
                    peer_values = [self._limit_magnitude(limit, item[limit]) for item in peers]
                    peer_median = _median(peer_values)
                    observation[f"{limit}_observed_a"] = row[limit]
                    observation[f"{limit}_observed_magnitude_a"] = observed
                    observation[f"{limit}_peer_median_a"] = peer_median
                    observation[f"{limit}_peer_deviation_a"] = (
                        observed - peer_median if peer_median is not None else None)
                    observation[f"relative_{limit}_ratio"] = (
                        observed / peer_median if peer_median not in (None, 0) else None)
                    observation[f"relative_{limit}_percent"] = (
                        100.0 * observed / peer_median if peer_median not in (None, 0) else None)
                result.append(observation)
        return result

    @staticmethod
    def _public_cell(context):
        if context is None:
            return None
        return {key: value for key, value in context.items()
                if key not in {"timestamp", "_tie_breaker"}}

    def _transition_context(self, timestamp, adr, transitions):
        candidates = [item for item in transitions
                      if item["adr"] == adr and item["timestamp"] <= timestamp
                      and timestamp - item["timestamp"] <= self.parameters.transition_window_seconds]
        if not candidates:
            return None
        item = candidates[-1]
        return {**item, "timestamp": _iso(item["timestamp"]),
                "delta_t_seconds": timestamp - item["timestamp"]}

    def _current_context(self, timestamp, before, following):
        current_before = before.get("module_current_a") if before else None
        first = following[0] if following else None
        if first is not None and first["timestamp"] > timestamp + 120:
            first = None
        windows = {}
        for seconds in (60, 120):
            values = [float(item["module_current_a"]) for item in following
                      if item.get("module_current_a") is not None
                      and item["timestamp"] <= timestamp + seconds]
            windows[str(seconds)] = {
                "min_current_a": min(values) if values else None,
                "max_current_a": max(values) if values else None,
                "median_current_a": _median(values), "sample_count": len(values),
            }
        first_current = first.get("module_current_a") if first else None
        if first_current is None:
            category = "insufficient_data"
        elif abs(float(first_current)) <= self.parameters.near_zero_current_a:
            category = "near_zero_observed"
        elif float(first_current) < -self.parameters.near_zero_current_a:
            category = "continued_discharge_observed"
        else:
            category = "non_discharge_current_observed"
        return {
            "current_before_a": current_before,
            "current_after_first_a": first_current,
            "current_after_first_age_seconds": first["timestamp"] - timestamp if first else None,
            "delta_current_first_a": (float(first_current) - float(current_before)
                                      if first_current is not None and current_before is not None else None),
            "after_60s": windows["60"], "after_120s": windows["120"],
            "category": category,
            "near_zero_threshold_a": self.parameters.near_zero_current_a,
        }

    def _stack_current(self, timestamp, all_cells):
        selected = []
        for serial, rows in all_cells.items():
            candidates = [item for item in rows if item["timestamp"] <= timestamp
                          and item.get("module_current_a") is not None]
            if candidates:
                selected.append((serial, candidates[-1]))
        if not selected:
            return None
        sample_times = [item[1]["timestamp"] for item in selected]
        span = max(sample_times) - min(sample_times)
        if (span > self.parameters.stack_sync_seconds
                or timestamp - max(sample_times) > self.parameters.stack_sync_seconds):
            return None
        return {"value_a": sum(float(item[1]["module_current_a"]) for item in selected),
                "source": "reconstructed_from_module_currents",
                "module_count": len(selected), "max_sample_span_seconds": span}

    @staticmethod
    def _dynamics(timestamp, context, before):
        if context is None:
            return {}
        result = {}
        for seconds in (30, 60, 120):
            candidates = [item for item in before if item["timestamp"] <= timestamp - seconds]
            if not candidates:
                result[str(seconds)] = None
                continue
            old = candidates[-1]
            worst_cell = int(context["worst_negative_cell"])
            result[str(seconds)] = {
                "sample_timestamp": _iso(old["timestamp"]),
                "sample_distance_seconds": context["timestamp"] - old["timestamp"],
                "delta_min_cell_mv": context["min_cell_voltage_mv"] - old["min_cell_voltage_mv"],
                "delta_spread_mv": context["spread_mv"] - old["spread_mv"],
                "delta_worst_cell_mv": (context["cell_voltages_mv"][worst_cell - 1]
                                         - old["cell_voltages_mv"][worst_cell - 1]),
                "delta_module_current_a": (
                    float(context["module_current_a"]) - float(old["module_current_a"])
                    if context.get("module_current_a") is not None
                    and old.get("module_current_a") is not None else None),
            }
        return result

    def daily_aggregates(self, events: Sequence[Mapping], management: Sequence[Mapping],
                         relative_limits: Sequence[Mapping] = ()) -> list[dict]:
        zone = ZoneInfo(self.parameters.daily_timezone)
        local_day = lambda timestamp: datetime.fromtimestamp(timestamp, zone).date().isoformat()
        def bounds(day):
            start = datetime.combine(datetime.fromisoformat(day).date(), datetime_time(), zone)
            end = datetime.combine(start.date() + timedelta(days=1), datetime_time(), zone)
            return start.timestamp(), end.timestamp()
        management_groups = defaultdict(list)
        for item in management:
            day = local_day(item["timestamp"])
            management_groups[(item["physical_serial"], day)].append(item)
        coverage_groups = defaultdict(list)
        management_by_serial = defaultdict(list)
        for item in management:
            management_by_serial[item["physical_serial"]].append(item)
        for serial, serial_rows in management_by_serial.items():
            serial_rows.sort(key=lambda item: item["timestamp"])
            for first, second in zip(serial_rows, serial_rows[1:]):
                start, end = first["timestamp"], second["timestamp"]
                if end - start > self.parameters.management_gap_seconds:
                    continue
                cursor = datetime.fromtimestamp(start, zone).date()
                final = datetime.fromtimestamp(end, zone).date()
                while cursor <= final:
                    day = cursor.isoformat()
                    day_start, day_end = bounds(day)
                    segment = (max(start, day_start), min(end, day_end))
                    if segment[1] > segment[0]:
                        coverage_groups[(serial, day)].append(segment)
                    cursor += timedelta(days=1)
        event_groups = defaultdict(list)
        duration_groups = defaultdict(list)
        for event in events:
            start = _timestamp(event["observed_start"])
            day = local_day(start)
            event_groups[(event["physical_serial"], day)].append(event)
            end_value = event.get("observed_end") or event.get("observed_through")
            end = _timestamp(end_value) if end_value else start
            cursor = datetime.fromtimestamp(start, zone).date()
            final = datetime.fromtimestamp(end, zone).date()
            while cursor <= final:
                duration_groups[(event["physical_serial"], cursor.isoformat())].append(event)
                cursor += timedelta(days=1)
        relative_groups = defaultdict(list)
        for item in relative_limits:
            relative_groups[(item["physical_serial"], local_day(_timestamp(item["timestamp"])))].append(item)
        result = []
        for key in sorted(set(management_groups) | set(event_groups)
                          | set(duration_groups) | set(coverage_groups)):
            serial, day = key
            rows = sorted(management_groups[key], key=lambda item: item["timestamp"])
            all_items = event_groups[key]
            items = [item for item in all_items if item["event_type"] == "dcl_zero"]
            ccl_items = [item for item in all_items if item["limit_type"] == "ccl"]
            dcl_items = [item for item in all_items if item["limit_type"] == "dcl"]
            duration_items = duration_groups[key]
            day_bounds = bounds(day)
            coverage_intervals = coverage_groups[key]
            ccl_reduced_duration = _observed_union_duration(
                [item for item in duration_items if item["limit_type"] == "ccl"],
                day_bounds=day_bounds, coverage_intervals=coverage_intervals)
            durations = [float(item["observed_duration_seconds"]) for item in items
                         if item.get("observed_duration_seconds") is not None]
            observed = sum(end - start for start, end in coverage_intervals)
            zero_duration = _observed_union_duration(
                [item for item in duration_items if item["event_type"] == "dcl_zero"],
                day_bounds=day_bounds, coverage_intervals=coverage_intervals)
            contexts = [item["cell_context"] for item in items if item.get("cell_context")]
            lowest = Counter(cell for item in contexts
                             for cell in map(int, item["min_cell_numbers"]))
            unique_lowest = Counter(int(item["min_cell_number"]) for item in contexts
                                    if item["min_cell_is_unique"])
            worst = Counter(cell for item in contexts
                            for cell in map(int, item["worst_negative_cells"]))
            transition_keys = []
            transition_deltas = defaultdict(list)
            for item in items:
                transition = item.get("transition_0x44")
                if transition:
                    name = f"offset:{transition['offset']}:{transition['old_hex']}->{transition['new_hex']}"
                    transition_keys.append(name)
                    transition_deltas[name].append(float(transition["delta_t_seconds"]))
            transition_counts = Counter(transition_keys)
            dominant_lowest = (lowest.most_common(1)[0][0] if lowest else None)
            dominant_transition = (transition_counts.most_common(1)[0][0]
                                   if transition_counts else None)
            currents = [float(item["module_current_a"]) for item in contexts
                        if item.get("module_current_a") is not None]
            available = len(contexts)
            despite = sum(bool(item["dcl_zero_despite_enable"]) for item in items)
            ccl_values = [float(item["ccl"]) for item in rows]
            ccl_zero_items = [item for item in ccl_items if item["event_type"] == "ccl_zero"]
            dcl_zero_items = [item for item in dcl_items if item["event_type"] == "dcl_zero"]
            peer_rows = relative_groups.get(key, [])
            relative_ccl = [float(item["relative_ccl_ratio"]) for item in peer_rows
                            if item.get("relative_ccl_ratio") is not None]
            relative_dcl = [float(item["relative_dcl_ratio"]) for item in peer_rows
                            if item.get("relative_dcl_ratio") is not None]
            result.append({
                "schema_version": SCHEMA_VERSION, "day": day,
                "day_timezone": self.parameters.daily_timezone,
                "physical_serial": serial, "dcl_zero_count": len(items),
                "dcl_zero_total_observed_duration_seconds": zero_duration,
                "dcl_zero_max_duration_seconds": max(durations) if durations else None,
                "dcl_zero_median_duration_seconds": _median(durations),
                "observed_management_duration_seconds": observed,
                "management_gap_limit_seconds": self.parameters.management_gap_seconds,
                "duration_and_duty_cycle_basis":
                "restriction_intersection_with_gap_qualified_management_coverage",
                "management_coverage_ratio_of_day": observed / (day_bounds[1] - day_bounds[0]),
                "dcl_zero_duty_cycle": zero_duration / observed if observed > 0 else None,
                "ccl_reduction_event_count": len(ccl_items),
                "ccl_zero_event_count": len(ccl_zero_items),
                "ccl_min_a": min(ccl_values) if ccl_values else None,
                "ccl_median_a": _median(ccl_values),
                "ccl_reduced_duration_seconds": ccl_reduced_duration,
                "ccl_reduced_duty_cycle": (ccl_reduced_duration / observed
                                            if observed > 0 else None),
                "dcl_reduction_event_count": len(dcl_items),
                "dcl_zero_event_count": len(dcl_zero_items),
                "dcl_max_restriction_a": max(
                    [float(item["restriction_increase_a"]) for item in dcl_items],
                    default=None),
                "dcl_zero_duration_seconds": zero_duration,
                "peer_relative_ccl_observation_count": len(relative_ccl),
                "peer_relative_ccl_median_ratio": _median(relative_ccl),
                "peer_relative_ccl_min_ratio": min(relative_ccl) if relative_ccl else None,
                "peer_relative_dcl_observation_count": len(relative_dcl),
                "peer_relative_dcl_median_ratio": _median(relative_dcl),
                "peer_relative_dcl_min_ratio": min(relative_dcl) if relative_dcl else None,
                "dcl_zero_despite_enable_count": despite,
                "dcl_zero_despite_enable_ratio": despite / len(items) if items else None,
                "cell_context_available_count": available,
                "cell_context_coverage_ratio": available / len(items) if items else None,
                "lowest_cell_counts": {str(k): v for k, v in sorted(lowest.items())},
                "lowest_cell_ratios": {str(k): v / available for k, v in sorted(lowest.items())},
                "dominant_lowest_cell": dominant_lowest,
                "dominant_lowest_ratio": lowest[dominant_lowest] / available
                if dominant_lowest is not None else None,
                "median_deviation_per_cell": [
                    _median([float(item["per_cell_deviation_mv"][index]) for item in contexts])
                    for index in range(15)] if contexts else [],
                "worst_negative_deviation_per_cell": [
                    min(float(item["per_cell_deviation_mv"][index]) for item in contexts)
                    for index in range(15)] if contexts else [],
                "lowest_count_per_cell": [lowest[index] for index in range(1, 16)],
                "unique_lowest_count_per_cell": [unique_lowest[index]
                                                  for index in range(1, 16)],
                "worst_count_per_cell": [worst[index] for index in range(1, 16)],
                "median_spread_before_dcl_zero_mv": _median(
                    [float(item["spread_mv"]) for item in contexts]),
                "max_spread_before_dcl_zero_mv": max(
                    [float(item["spread_mv"]) for item in contexts], default=None),
                "median_min_cell_before_dcl_zero_mv": _median(
                    [float(item["min_cell_voltage_mv"]) for item in contexts]),
                "minimum_cell_before_dcl_zero_mv": min(
                    [float(item["min_cell_voltage_mv"]) for item in contexts], default=None),
                "median_module_current_before_a": _median(currents),
                "max_discharge_current_before_a": min(currents) if currents else None,
                "transition_0x44_counts": dict(transition_counts),
                "dominant_0x44_transition": dominant_transition,
                "dominant_0x44_ratio": (transition_counts[dominant_transition] / len(items)
                                         if dominant_transition and items else None),
                "transition_0x44_timing": ({"median_delta_t_seconds": _median(
                    transition_deltas[dominant_transition]),
                    "min_delta_t_seconds": min(transition_deltas[dominant_transition]),
                    "max_delta_t_seconds": max(transition_deltas[dominant_transition])}
                    if dominant_transition else None),
                "causality": CAUSALITY,
            })
        return result

    def negative_controls(self, management: Sequence[Mapping], cell_records: Iterable[Mapping],
                          *, limit: int = 7) -> list[dict]:
        """Return simple same-serial DCL-nonzero windows, without alarm semantics."""
        cells = self._prepare_cells(cell_records)
        controls, used = [], set()
        for row in sorted(management, key=lambda item: item["timestamp"]):
            if row["dcl"] == 0:
                continue
            candidates = [item for item in cells.get(row["physical_serial"], [])
                          if item["timestamp"] <= row["timestamp"]]
            if not candidates or candidates[-1]["timestamp"] in used:
                continue
            sample = candidates[-1]
            used.add(sample["timestamp"])
            controls.append({"physical_serial": row["physical_serial"],
                             "timestamp": _iso(row["timestamp"]), "dcl": row["dcl"],
                             "cell_context": self._public_cell(sample),
                             "matching": "same_serial_nonzero_dcl_observation",
                             "causality": CAUSALITY})
            if len(controls) >= limit:
                break
        return controls

    def trends(self, daily_aggregates: Sequence[Mapping], *, days: int,
               physical_serial: str | None = None) -> dict:
        """Classify bounded daily aggregate trends by first/second-half medians."""
        all_rows = list(daily_aggregates)
        serials = {item.get("physical_serial") for item in all_rows
                   if item.get("physical_serial") is not None}
        if physical_serial is None and len(serials) > 1:
            raise ValueError("physical_serial is required for multi-module trends")
        selected_serial = physical_serial or (next(iter(serials)) if serials else None)
        rows = [item for item in all_rows
                if selected_serial is None or item.get("physical_serial") == selected_serial]
        rows = sorted(rows, key=lambda item: item["day"])[-int(days):]
        usable_rows = [item for item in rows
                       if item.get("observed_management_duration_seconds", 1)
                       >= self.parameters.trend_min_management_coverage_seconds]
        fields = {
            "event_rate": "dcl_zero_count", "duty_cycle": "dcl_zero_duty_cycle",
            "worst_cell_deviation": "minimum_cell_before_dcl_zero_mv",
            "spread": "median_spread_before_dcl_zero_mv",
            "min_cell": "median_min_cell_before_dcl_zero_mv",
            "current_context": "median_module_current_before_a",
            "0x44_pattern_recurrence": "dominant_0x44_ratio",
            "dominant_lowest_cell_stability": "dominant_lowest_ratio",
        }
        result = {"window_days": int(days), "physical_serial": selected_serial,
                  "days_available": len(rows), "days_with_management_coverage": len(usable_rows),
                  "formula": "first-half median vs second-half median; relative tolerance",
                  "minimum_days": self.parameters.trend_min_days,
                  "minimum_management_coverage_seconds":
                  self.parameters.trend_min_management_coverage_seconds}
        for name, field in fields.items():
            values = [float(item[field]) for item in usable_rows if item.get(field) is not None]
            if len(values) < self.parameters.trend_min_days:
                result[name] = "insufficient_data"
                continue
            split = len(values) // 2
            first, second = _median(values[:split]), _median(values[split:])
            scale = max(abs(first), abs(second), 1e-9)
            change = (second - first) / scale
            if abs(change) <= self.parameters.trend_relative_tolerance:
                result[name] = "stable"
            else:
                result[name] = "increasing" if change > 0 else "decreasing"
        return result
