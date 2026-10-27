"""Bounded serial-centric reads of authoritative Guardian cell history."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextvars import ContextVar

from maintenance import normalize_utc_timestamp
from research_budget import budgeted, ResearchQueryError, check_budget
from history_block_index import bounded_ranges, range_lines

MAX_RECORDS = 10_000
MAX_POINTS = 6_000
MAX_CELLS = 15
FULL_DEFAULT_SECONDS = 86400
FULL_MAX_SECONDS = 7 * 86400
DISPLAY_MAX_SECONDS = 90 * 86400


METRIC_UNITS = {
    "soc": "%", "module_voltage": "V", "module_current": "A",
    "module_temperature": "degC", "cell_voltage": "mV",
    "cell_temperature": "degC", "cell_deviation": "mV", "cell_spread": "mV",
}


def observation_coverage(start, end, timestamps):
    """Observed support only: no extrapolation across either requested edge."""
    epochs = sorted(set(timestamps))
    begin, finish = (datetime.fromisoformat(value).timestamp() for value in budgeted((start, end)))
    gaps = [right - left for left, right in budgeted(zip(epochs, epochs[1:]))]
    cadence = statistics.median(gaps) if gaps else None
    threshold = cadence * 3 if cadence else None
    def stamp(epoch): return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
    missing, covered = [], []
    if epochs:
        if epochs[0] > begin:
            missing.append({"from": start, "to": stamp(epochs[0]), "kind": "leading"})
        first = epochs[0]
        for left, right in zip(epochs, epochs[1:]):
            check_budget()
            if threshold is not None and right - left > threshold:
                covered.append({"from": stamp(first), "to": stamp(left)})
                missing.append({"from": stamp(left), "to": stamp(right), "kind": "internal"})
                first = right
        covered.append({"from": stamp(first), "to": stamp(epochs[-1])})
        if epochs[-1] < finish:
            missing.append({"from": stamp(epochs[-1]), "to": end, "kind": "trailing"})
    else:
        missing.append({"from": start, "to": end, "kind": "entire_range"})
    return {"requested_range": {"from": start, "to": end},
        "covered_intervals": covered, "missing_intervals": missing,
        "first_observation": stamp(epochs[0]) if epochs else None,
        "last_observation": stamp(epochs[-1]) if epochs else None,
        "sample_count": len(epochs), "expected_cadence_seconds": cadence,
        "largest_gap_seconds": max(gaps) if gaps else None,
        "internal_gap_threshold_seconds": threshold,
        "boundary_policy": "no_extrapolation", "coverage_basis": "finite_metric_observations",
        "quality": "complete" if epochs and not missing else "partial" if epochs else "absent"}


class CursorCodec:
    """Signed opaque cursor bound to the complete normalized query."""
    def __init__(self, secret): self.secret = secret
    def encode(self, query_hash, offset):
        raw = json.dumps({"q": query_hash, "o": offset}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(
            raw + hmac.new(self.secret, raw, hashlib.sha256).digest()).decode().rstrip("=")
    def decode(self, token, query_hash):
        try:
            blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            raw, signature = blob[:-32], blob[-32:]
            if not hmac.compare_digest(signature, hmac.new(self.secret, raw, hashlib.sha256).digest()):
                raise ValueError
            value = json.loads(raw)
            if value["q"] != query_hash or type(value["o"]) is not int or value["o"] < 0:
                raise ValueError
            return value["o"]
        except Exception as exc:
            raise ResearchQueryError("cursor_invalid", "cursor is invalid or belongs to another query") from exc


class ResearchTimeseriesService:
    METRICS = frozenset({"soc", "module_voltage", "module_current",
        "module_temperature", "cell_voltage", "cell_temperature", "cell_deviation",
        "cell_spread"})

    def __init__(self, directory, identity_resolver, cursor_codec, *, window_backend=None):
        self.directory = Path(directory)
        self.identity = identity_resolver
        self.cursor = cursor_codec
        self.window_backend = window_backend
        self._window_rows = ContextVar("research_window_rows", default=None)

    @staticmethod
    def normalize_range(timestamp_from, timestamp_to):
        try:
            start = normalize_utc_timestamp(timestamp_from, "from")
            end = normalize_utc_timestamp(timestamp_to, "to")
        except Exception as exc:
            raise ResearchQueryError("invalid_argument", "from/to must be timezone-aware ISO-8601") from exc
        if datetime.fromisoformat(start) > datetime.fromisoformat(end):
            raise ResearchQueryError("invalid_argument", "from must not exceed to")
        return start, end

    def _paths(self, start, end):
        # CellHistoryWriter names daily files using the runtime local timezone.
        day = datetime.fromisoformat(start).astimezone().date()
        last = datetime.fromisoformat(end).astimezone().date()
        while day <= last:
            path = self.directory / f"{day.isoformat()}.jsonl"
            if path.is_file(): yield path
            day += timedelta(days=1)

    def _records(self, start, end, deadline=None):
        """Use existing validated indexes only; never create/rebuild an index."""
        if self.window_backend is not None:
            active_rows = self._window_rows.get()
            if active_rows is not None:
                for record, ref in active_rows:
                    check_budget(deadline)
                    yield record, ref.timestamp
            else:
                with self.window_backend.read(start, end) as rows:
                    for record, ref in rows:
                        check_budget(deadline)
                        yield record, ref.timestamp
            return
        first, last = (datetime.fromisoformat(v).timestamp() for v in budgeted((start, end)))
        for path in self._paths(start, end):
            ranges, plan = bounded_ranges(path, first, last,
                timestamp_field="timestamp", iso_timestamp=False)
            with path.open("rb") as handle:
                for line in range_lines(handle, ranges, unindexed_offset=plan["unindexed_offset"]):
                    check_budget(deadline)
                    try:
                        record=json.loads(line); epoch=float(record["timestamp"])
                    except (ValueError,TypeError,KeyError,UnicodeError):
                        continue
                    if math.isfinite(epoch) and first<=epoch<=last:
                        yield record,epoch
            continue

    def _matches(self, record, serial, timestamp):
        observed = record.get("module_serial")
        if observed is not None:
            return observed == serial
        position = record.get("module")
        return (type(position) is int and 1 <= position <= 6 and
                self.identity.serial_at(position, timestamp).get("physical_serial") == serial)

    def coverage(self, metrics, physical_serial, timestamp_from, timestamp_to, deadline=None):
        """One scan, no point objects/identity epochs/downsampling per metric."""
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        if (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() > DISPLAY_MAX_SECONDS:
            raise ResearchQueryError("range_too_large", "coverage range exceeds 90 days")
        times = {metric: [] for metric in budgeted(metrics)}
        for record, epoch in self._records(start, end, deadline):
            timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
            if not self._matches(record, physical_serial, timestamp): continue
            for metric in times:
                if self._finite_values(record, metric, ()):
                    times[metric].append(epoch)
        return {metric: {**observation_coverage(start, end, values),
                         "unit": METRIC_UNITS[metric], "source": "guardian.cell_history",
                         "resolution": "full"} for metric, values in budgeted(times.items())}

    @classmethod
    def _finite_values(cls, record, metric, cells):
        try:
            values = cls._values(record, metric, cells)
            return [(cell, float(value)) for cell, value in budgeted(values)
                    if value is not None and not isinstance(value, bool) and math.isfinite(float(value))]
        except ResearchQueryError:
            raise
        except (ValueError, TypeError, OverflowError):
            return []

    def soc_current_by_serial(self, physical_serials, timestamp_from, timestamp_to,
                              deadline=None):
        """Read SOC/current for several identities in one authoritative scan."""
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        start_epoch = datetime.fromisoformat(start).timestamp()
        end_epoch = datetime.fromisoformat(end).timestamp()
        wanted = set(physical_serials)
        result = {serial: [] for serial in budgeted(wanted)}
        for path in self._paths(start, end):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    check_budget(deadline)
                    try:
                        record = json.loads(line); epoch = float(record["timestamp"])
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                        continue
                    if not start_epoch <= epoch <= end_epoch: continue
                    timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                    position = int(record.get("module", 0))
                    observed = record.get("module_serial")
                    if observed is None and 1 <= position <= 6:
                        observed = self.identity.serial_at(position, timestamp).get("physical_serial")
                    if observed not in wanted: continue
                    identity = self.identity.position_at(observed, timestamp)
                    try:
                        soc, current = float(record["soc_percent"]), float(record["current_a"])
                    except (ValueError, TypeError, KeyError):
                        continue
                    voltages = record.get("voltages_mv") or []
                    result[observed].append({"timestamp": timestamp, "soc": soc,
                        "current": current, "identity_epoch_id": identity.get("identity_epoch_id"),
                        "position_at_time": identity.get("position_at_time"),
                        "lowest_cell": voltages.index(min(voltages)) + 1 if voltages else None,
                        "cell_spread_mv": max(voltages) - min(voltages) if voltages else None})
        for rows in result.values(): rows.sort(key=lambda item: item["timestamp"])
        return result

    @staticmethod
    def _values(record, metric, cells):
        if metric == "soc": return ((None, record.get("soc_percent")),)
        if metric == "module_current": return ((None, record.get("current_a")),)
        if metric == "module_voltage":
            value = record.get("module_voltage_v")
            if value is None and record.get("voltages_mv"):
                value = sum(float(item) for item in budgeted(record["voltages_mv"])) / 1000
            return ((None, value),)
        if metric == "module_temperature":
            values = record.get("temperatures_c") or ()
            return ((None, max(values) if values else None),)
        if metric == "cell_spread":
            values = [float(item) for item in budgeted(record.get("voltages_mv", ()))]
            return ((None, max(values) - min(values) if values else None),)
        values = record.get("voltages_mv" if metric != "cell_temperature" else "temperatures_c") or ()
        selected = cells or tuple(range(1, len(values) + 1))
        median = statistics.median(float(item) for item in budgeted(values)) if values else None
        return tuple((cell, (float(values[cell - 1]) - median
                            if metric == "cell_deviation" else values[cell - 1]))
                     for cell in budgeted(selected) if 1 <= cell <= len(values))

    def query(self, **kwargs):
        if self.window_backend is None:
            return self._query(**kwargs)
        start, end = self.normalize_range(kwargs['timestamp_from'], kwargs['timestamp_to'])
        with self.window_backend.read(start, end) as rows:
            token = self._window_rows.set(rows)
            try:
                result = self._query(**kwargs)
                result['data_state'] = self.window_backend.metrics
                return result
            finally:
                self._window_rows.reset(token)

    def _query(self, *, metric, physical_serial, timestamp_from, timestamp_to,
              resolution="auto", max_points=MAX_POINTS, cells=(), cursor=None,
              deadline=None):
        if metric not in self.METRICS:
            raise ResearchQueryError("invalid_argument", "metric is unsupported")
        if not physical_serial:
            raise ResearchQueryError("invalid_argument", "physical_serial is required")
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        if resolution not in {"auto", "full", "display"}:
            raise ResearchQueryError("invalid_argument", "resolution must be auto, full, or display")
        selected_resolution = ("full" if span <= FULL_DEFAULT_SECONDS else "display") \
            if resolution == "auto" else resolution
        if span > (FULL_MAX_SECONDS if selected_resolution == "full" else DISPLAY_MAX_SECONDS):
            raise ResearchQueryError("range_too_large", "requested range exceeds resolution limit")
        if type(max_points) is not int or not 1 <= max_points <= MAX_POINTS:
            raise ResearchQueryError("invalid_argument", f"max_points must be 1..{MAX_POINTS}")
        cells = tuple(sorted(set(cells)))
        if len(cells) > MAX_CELLS or any(not 1 <= cell <= 15 for cell in budgeted(cells)):
            raise ResearchQueryError("invalid_argument", "cell numbers must be 1..15")
        if self.window_backend is not None:
            source_signature = self.window_backend.metrics['source_fingerprint']
        else:
            selected_paths = list(self._paths(start, end))
            source_signature = [(path.name, path.stat().st_size, path.stat().st_mtime_ns)
                                for path in budgeted(selected_paths)]
        query_hash = hashlib.sha256(json.dumps((metric, physical_serial, start, end,
            selected_resolution, max_points, cells, source_signature),
            separators=(",", ":")).encode()).hexdigest()
        offset = self.cursor.decode(cursor, query_hash) if cursor else 0
        points, observation_times, signatures = [], [], []
        signatures = source_signature
        for record, epoch in self._records(start, end, deadline):
            timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
            if not self._matches(record, physical_serial, timestamp): continue
            values = self._finite_values(record, metric, cells)
            if not values: continue
            identity = self.identity.position_at(physical_serial, timestamp)
            observation_times.append(epoch)
            for cell, value in values:
                point = {"timestamp": timestamp, "value": value,
                    "physical_serial": physical_serial,
                    "position_at_time": identity.get("position_at_time"),
                    "position_history_id": identity.get("position_history_id"),
                    "identity_epoch_id": identity.get("identity_epoch_id"),
                    "identity_resolved": identity.get("resolved", False),
                    "pwr_sample_at": record.get("pwr_sample_at"),
                    "cell_sample_at": record.get("cell_sample_at", epoch),
                    "pwr_age_seconds": record.get("pwr_age_seconds"),
                    "pwr_age_quality": record.get("pwr_age_quality", "unavailable"),
                    "received_at": record.get("received_at"),
                    "received_at_quality": "observed" if record.get("received_at") is not None else "unavailable",
                    "balancing": ((record.get("balancing") or [])[cell-1]
                        if cell is not None and cell<=len(record.get("balancing") or []) else None),
                    "bms_protection": None,
                    "bms_protection_quality": "not_recorded_in_cell_history",
                    "identity_source": ("record_module_serial" if record.get("module_serial") is not None
                                        else "position_history")}
                if cell is not None: point["cell_number"] = cell
                points.append(point)
        check_budget(deadline)
        points.sort(key=lambda item: (item["timestamp"], item.get("cell_number", 0)))
        check_budget(deadline)
        source_points = len(points)
        if selected_resolution == "display" and len(points) > max_points:
            if max_points == 1: points = points[:1]
            else:
                step = (len(points) - 1) / (max_points - 1)
                points = [points[round(index * step)] for index in budgeted(range(max_points))]
        page_limit = min(max_points, MAX_RECORDS)
        page = points[offset:offset + page_limit]
        truncated = offset + len(page) < len(points)
        coverage = observation_coverage(start, end, observation_times)
        check_budget(deadline)
        return {"metric": metric, "unit": METRIC_UNITS[metric], "source": "guardian.cell_history",
            "physical_serial": physical_serial, "points": page,
            "point_count": len(page), "source_point_count": source_points,
            "resolution": selected_resolution, "coverage": coverage, "truncated": truncated,
            "next_cursor": self.cursor.encode(query_hash, offset + len(page)) if truncated else None,
            "source_fingerprint": hashlib.sha256(json.dumps(signatures).encode()).hexdigest()}
