"""Single-pass Guardian JSONL reads and bounded extrema-preserving projection."""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from position_history import DEFAULT_POSITION_HISTORY_FILE, PositionHistoryLog
from stack_soc import STACK_SOC_METRICS, project_stack_soc

DEFAULT_CELL_HISTORY_DIR = Path("/share/guardian_battery/cell_history")
SERIES_METRICS = frozenset({"soc", "current", "cell_voltage", "cell_temperature",
                            *STACK_SOC_METRICS})
DEFAULT_MAX_DISPLAY_POINTS = 6000


class SeriesHistoryError(RuntimeError):
    pass


class _ExtremaCollector:
    """Keep exact short series, then switch to fixed time-bucket min/max."""
    def __init__(self, limit: int, start_epoch: float, end_epoch: float):
        self.limit = max(4, limit)
        self.start = start_epoch
        self.span = max(1.0, end_epoch - start_epoch)
        self.exact: list[dict] | None = []
        self.first = None
        self.last = None
        self.buckets: dict[int, tuple[dict, dict]] = {}
        self.bucket_count = max(1, (self.limit - 2) // 2)

    def add(self, point: dict) -> None:
        self.first = self.first or point
        self.last = point
        if self.exact is not None:
            self.exact.append(point)
            if len(self.exact) <= self.limit:
                return
            exact, self.exact = self.exact, None
            for existing in exact[1:-1]:
                self._bucket(existing)
            return
        self._bucket(point)

    def _bucket(self, point: dict) -> None:
        index = min(self.bucket_count - 1, max(0, int(
            (point["_epoch"] - self.start) / self.span * self.bucket_count)))
        low, high = self.buckets.get(index, (point, point))
        if point["value"] < low["value"]:
            low = point
        if point["value"] > high["value"]:
            high = point
        self.buckets[index] = (low, high)

    def points(self) -> list[dict]:
        if self.exact is not None:
            result = self.exact
        else:
            result = [self.first]
            for low, high in self.buckets.values():
                result.extend(sorted({id(low): low, id(high): high}.values(),
                                     key=lambda item: item["_epoch"]))
            result.append(self.last)
        unique = {id(point): point for point in result if point is not None}
        return [{key: value for key, value in point.items() if key != "_epoch"}
                for point in sorted(unique.values(), key=lambda item: item["_epoch"])][:self.limit]


class CellHistorySeries:
    def __init__(self, directory=DEFAULT_CELL_HISTORY_DIR, cache_size=24,
                 position_history_path=DEFAULT_POSITION_HISTORY_FILE):
        self.directory = Path(directory)
        self.position_history_path = Path(position_history_path)
        self.cache_size = cache_size
        self._cache = OrderedDict()

    def _paths(self, start, end):
        if not self.directory.exists():
            return []
        first = datetime.fromisoformat(start).astimezone(timezone.utc).date().isoformat()
        last = datetime.fromisoformat(end).astimezone(timezone.utc).date().isoformat()
        return sorted(path for path in self.directory.glob("*.jsonl") if first <= path.stem <= last)

    def query_bundle(self, *, metric, timestamp_from, timestamp_to, module_number,
                     cell_number=None, cell_numbers=None, max_points=DEFAULT_MAX_DISPLAY_POINTS):
        result = self.query_bundles(
            requests=({"metric": metric, "cell_number": cell_number,
                       "cell_numbers": cell_numbers},),
            timestamp_from=timestamp_from, timestamp_to=timestamp_to,
            module_number=module_number, max_points=max_points,
        )
        bundle = result["series"][0]
        return {
            **bundle,
            "samples": result["samples"],
            "raw_records": result["raw_records"],
            "read_seconds": result["read_seconds"],
            "downsample_seconds": result["downsample_seconds"],
            "cache_hit": result["cache_hit"],
        }

    def query_bundles(self, *, requests, timestamp_from, timestamp_to, module_number,
                      max_points=DEFAULT_MAX_DISPLAY_POINTS):
        """Project several metrics from one JSONL scan and one shared sample set."""
        normalized = []
        for request in requests:
            metric = request["metric"]
            if metric not in SERIES_METRICS:
                raise ValueError("unsupported series metric")
            cell_number = request.get("cell_number")
            cell_numbers = request.get("cell_numbers")
            selected_cells = tuple(sorted(cell_numbers)) if cell_numbers else None
            normalized.append((metric, cell_number, selected_cells))
        if not normalized:
            raise ValueError("at least one series metric is required")
        if len({metric for metric, _, _ in normalized}) != len(normalized):
            raise ValueError("series metrics must be unique")
        paths = self._paths(timestamp_from, timestamp_to)
        try:
            signature = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
            position_signature = ((self.position_history_path.stat().st_size,
                                   self.position_history_path.stat().st_mtime_ns)
                                  if any(item[0] in STACK_SOC_METRICS for item in normalized)
                                  and self.position_history_path.exists() else None)
        except OSError as exc:
            raise SeriesHistoryError("cell history is unavailable") from exc
        key = (signature, position_signature, tuple(normalized), timestamp_from, timestamp_to, module_number,
               max_points)
        if key in self._cache:
            self._cache.move_to_end(key)
            return {**self._cache[key], "cache_hit": True}

        started = time.perf_counter()
        start_epoch = datetime.fromisoformat(timestamp_from).timestamp()
        end_epoch = datetime.fromisoformat(timestamp_to).timestamp()
        collectors = []
        for metric, cell_number, selected_cells in normalized:
            group_count = len(selected_cells) if selected_cells else (
                15 if metric in {"cell_voltage", "cell_temperature"}
                and cell_number is None else 1)
            collectors.append((max(4, max_points // group_count), {}))
        samples, raw_records, stack_records = [], 0, []
        raw_points = [0] * len(normalized)
        try:
            for path in paths:
                with path.open(encoding="utf-8") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        if record.get("schema_version") != 1:
                            raise ValueError("schema")
                        epoch = float(record["timestamp"])
                        if not start_epoch <= epoch <= end_epoch:
                            continue
                        if any(metric in STACK_SOC_METRICS for metric, _, _ in normalized):
                            stack_records.append(record)
                        if int(record["module"]) != module_number:
                            continue
                        timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                        raw_records += 1
                        samples.append({"timestamp": timestamp, "current_a": float(record["current_a"]),
                                        "soc_percent": float(record["soc_percent"]),
                                        "voltages_mv": [float(value) for value in record["voltages_mv"]],
                                        "module_serial": record.get("module_serial")})
                        for index, (metric, cell_number, selected_cells) in enumerate(normalized):
                            per_group, metric_collectors = collectors[index]
                            if metric in STACK_SOC_METRICS:
                                continue
                            for point in self._points(record, metric, cell_number, timestamp,
                                                      epoch, selected_cells):
                                group = point.get("cell_number", 0)
                                metric_collectors.setdefault(
                                    group,
                                    _ExtremaCollector(per_group, start_epoch, end_epoch),
                                ).add(point)
                                raw_points[index] += 1
            if stack_records:
                snapshots = PositionHistoryLog(self.position_history_path).read_all()
                projected_soc = project_stack_soc(stack_records, snapshots)
                for index, (metric, _cell_number, _selected_cells) in enumerate(normalized):
                    if metric not in STACK_SOC_METRICS:
                        continue
                    per_group, metric_collectors = collectors[index]
                    key_name = "stack_soc_median" if metric == "stack_soc_median" else "soc_deviation_pp"
                    for item in projected_soc:
                        if item["module"] != module_number:
                            continue
                        point = {"timestamp": item["timestamp"], "_epoch": item["_epoch"],
                                 "value": item[key_name],
                                 "module_serial": item["module_serial"],
                                 "active_module_count": item["active_module_count"]}
                        metric_collectors.setdefault(
                            0, _ExtremaCollector(per_group, start_epoch, end_epoch)).add(point)
                        raw_points[index] += 1
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise SeriesHistoryError(f"cell history is invalid: {exc}") from exc

        scan_seconds = time.perf_counter() - started
        downsample_started = time.perf_counter()
        projected = []
        for index, (metric, cell_number, selected_cells) in enumerate(normalized):
            points = [point for collector in collectors[index][1].values()
                      for point in collector.points()]
            points.sort(key=lambda point: (point["timestamp"], point.get("cell_number", 0)))
            projected.append({"metric": metric, "cell_number": cell_number,
                              "cell_numbers": list(selected_cells or ()), "points": points,
                              "raw_points": raw_points[index]})
        downsample_seconds = time.perf_counter() - downsample_started
        result = {"series": projected, "samples": samples, "raw_records": raw_records,
                  "read_seconds": scan_seconds,
                  "downsample_seconds": downsample_seconds,
                  "cache_hit": False}
        self._cache[key] = result
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return result

    def query(self, **kwargs):
        return self.query_bundle(**kwargs)["points"]

    def samples(self, *, timestamp_from, timestamp_to, module_number):
        return self.query_bundle(metric="soc", timestamp_from=timestamp_from,
                                 timestamp_to=timestamp_to, module_number=module_number)["samples"]

    @staticmethod
    def _points(record, metric, cell, timestamp, epoch, selected_cells=None):
        provenance = {key: record[key] for key in
                      ("module_serial", "position_history_id", "identity_source")
                      if record.get(key) is not None}
        common = {"timestamp": timestamp, "_epoch": epoch, **provenance}
        if metric == "soc":
            return [{**common, "value": float(record["soc_percent"])}]
        if metric == "current":
            return [{**common, "value": float(record["current_a"])}]
        values = record["voltages_mv" if metric == "cell_voltage" else "temperatures_c"]
        if cell is not None:
            return [{**common, "value": float(values[cell - 1])}]
        return [{**common, "value": float(value), "cell_number": index}
                for index, value in enumerate(values, 1)
                if selected_cells is None or index in selected_cells]
