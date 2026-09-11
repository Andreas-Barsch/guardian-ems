"""Single-pass Guardian JSONL reads and bounded extrema-preserving projection."""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from position_history import DEFAULT_POSITION_HISTORY_FILE, PositionHistoryLog
from stack_soc import STACK_SOC_METRICS, project_stack_soc
from history_block_index import index_signature, selected_ranges

DEFAULT_CELL_HISTORY_DIR = Path("/share/guardian_battery/cell_history")
SERIES_METRICS = frozenset({"soc", "current", "cell_voltage", "cell_temperature",
                            *STACK_SOC_METRICS})
DEFAULT_MAX_DISPLAY_POINTS = 6000
_MODULE_TOKEN = re.compile(r'"module"\s*:\s*(\d+)')
_MODULE_TOKEN_BYTES = re.compile(rb'"module"\s*:\s*(\d+)')


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

    def query_bundles(self, *, requests, timestamp_from, timestamp_to, module_number=None,
                      module_numbers=None, max_points=DEFAULT_MAX_DISPLAY_POINTS,
                      include_all_module_soc=False, timing=None):
        """Project several metrics from one JSONL scan and one shared sample set."""
        selected_modules = tuple(sorted(set(module_numbers or (
            (module_number,) if module_number is not None else ()))))
        if not selected_modules:
            raise ValueError("at least one module is required")
        if any(not 1 <= number <= 6 for number in selected_modules):
            raise ValueError("module numbers must be between 1 and 6")
        primary_module = module_number if module_number in selected_modules else selected_modules[0]
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
        stage = timing.stage if timing else nullcontext
        with stage("discovery"):
            considered = len(list(self.directory.glob("*.jsonl"))) if self.directory.exists() else 0
            paths = self._paths(timestamp_from, timestamp_to)
        try:
            signature = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns,
                               index_signature(path)) for path in paths)
            position_signature = ((self.position_history_path.stat().st_size,
                                   self.position_history_path.stat().st_mtime_ns)
                                  if any(item[0] in STACK_SOC_METRICS for item in normalized)
                                  and self.position_history_path.exists() else None)
        except OSError as exc:
            raise SeriesHistoryError("cell history is unavailable") from exc
        key = (signature, position_signature, tuple(normalized), timestamp_from, timestamp_to,
               selected_modules, primary_module, max_points, include_all_module_soc)
        if key in self._cache:
            self._cache.move_to_end(key)
            if timing:
                timing.counts(cache_hit=True, cache_miss=False,
                              cell_files_considered=considered, cell_files_opened=0)
                timing.not_executed("cell_read_parse_filter", "downsampling")
            return {**self._cache[key], "cache_hit": True}

        started = time.perf_counter()
        start_epoch = datetime.fromisoformat(timestamp_from).timestamp()
        end_epoch = datetime.fromisoformat(timestamp_to).timestamp()
        collectors = []
        for metric, cell_number, selected_cells in normalized:
            group_count = len(selected_modules) * (len(selected_cells) if selected_cells else (
                15 if metric in {"cell_voltage", "cell_temperature"}
                and cell_number is None else 1))
            collectors.append((max(4, max_points // group_count), {}))
        samples, raw_records, raw_file_records, stack_records = [], 0, 0, []
        soc_collectors = ({number: _ExtremaCollector(
            max(4, max_points // max(1, len(selected_modules))), start_epoch, end_epoch)
                           for number in selected_modules} if include_all_module_soc else {})
        raw_points = [0] * len(normalized)
        soc_metric_indexes = [index for index, item in enumerate(normalized)
                              if item[0] == "soc"]
        needs_stack_context = any(metric in STACK_SOC_METRICS
                                  for metric, _, _ in normalized)
        rejected = parsed = in_window = bytes_read = opened = 0
        seek_modes = set(); skipped_bytes = 0
        parse_errors = invalid_records = 0
        try:
          with stage("cell_read_parse_filter"):
            for path in paths:
                try:
                    ranges, seek = selected_ranges(
                        path, start_epoch, end_epoch,
                        timestamp_field="timestamp", iso_timestamp=False)
                    seek_modes.add(seek["mode"]); skipped_bytes += seek["skipped_bytes"]
                except Exception:
                    ranges = ((0, path.stat().st_size),); seek_modes.add("full_scan")
                with path.open("rb") as handle:
                    opened += 1
                    for range_start, range_end in ranges:
                        handle.seek(range_start)
                        while handle.tell() < range_end:
                            line = handle.readline(); bytes_read += len(line)
                            if not line.strip():
                                continue
                            raw_file_records += 1
                            module_token = _MODULE_TOKEN_BYTES.search(line)
                            if (not needs_stack_context and module_token is not None
                                    and int(module_token.group(1)) not in selected_modules):
                                rejected += 1
                                continue
                            try:
                                record = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                parse_errors += 1
                                raise
                            parsed += 1
                            if record.get("schema_version") != 1:
                                invalid_records += 1
                                raise ValueError("schema")
                            epoch = float(record["timestamp"])
                            if not start_epoch <= epoch <= end_epoch:
                                continue
                            in_window += 1
                            record_module = int(record["module"])
                            if include_all_module_soc and record_module in soc_collectors:
                                timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                                point = self._points(record, "soc", None, timestamp, epoch)[0]
                                point["module_number"] = record_module
                                point["source"] = "pylontech"
                                soc_collectors[record_module].add(point)
                                for index in soc_metric_indexes:
                                    raw_points[index] += 1
                            if needs_stack_context:
                                stack_records.append(record)
                            if record_module not in selected_modules:
                                continue
                            timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                            raw_records += 1
                            if record_module == primary_module:
                                samples.append({"timestamp": timestamp,
                                                "current_a": float(record["current_a"]),
                                                "soc_percent": float(record["soc_percent"]),
                                                "voltages_mv": [float(value) for value in record["voltages_mv"]],
                                                "module_serial": record.get("module_serial")})
                            for index, (metric, cell_number, selected_cells) in enumerate(normalized):
                                per_group, metric_collectors = collectors[index]
                                if metric in STACK_SOC_METRICS:
                                    continue
                                if metric == "soc" and include_all_module_soc:
                                    continue
                                for point in self._points(record, metric, cell_number, timestamp,
                                                          epoch, selected_cells):
                                    if len(selected_modules) > 1:
                                        point["module_number"] = record_module
                                    group = (record_module, point.get("cell_number", 0))
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
                        if item["module"] not in selected_modules:
                            continue
                        point = {"timestamp": item["timestamp"], "_epoch": item["_epoch"],
                                 "value": item[key_name],
                                 "module_serial": item["module_serial"],
                                 "active_module_count": item["active_module_count"]}
                        if len(selected_modules) > 1:
                            point["module_number"] = item["module"]
                        metric_collectors.setdefault(
                            (item["module"], 0),
                            _ExtremaCollector(per_group, start_epoch, end_epoch)).add(point)
                        raw_points[index] += 1
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
            if timing:
                timing.counts(cell_files_considered=considered,
                    cell_files_opened=opened, cell_bytes_read=bytes_read,
                    cell_raw_lines=raw_file_records,
                    cell_lines_rejected_before_json=rejected,
                    cell_parsed_records=parsed, cell_parse_errors=parse_errors,
                    cell_invalid_records=invalid_records,
                    cell_records_in_window=in_window,
                    cell_records_for_requested_modules=raw_records,
                    cell_seek_mode=(next(iter(seek_modes)) if len(seek_modes) == 1 else "mixed"),
                    cell_skipped_bytes=skipped_bytes)
            raise SeriesHistoryError(f"cell history is invalid: {exc}") from exc

        scan_seconds = time.perf_counter() - started
        downsample_started = time.perf_counter(); downsample_cpu = time.thread_time()
        projected = []
        for index, (metric, cell_number, selected_cells) in enumerate(normalized):
            if metric == "soc" and include_all_module_soc:
                points = [point for collector in soc_collectors.values()
                          for point in collector.points()]
                if len(selected_modules) == 1:
                    points = [{key: value for key, value in point.items()
                               if key not in {"module_number", "source"}}
                              for point in points]
            else:
                points = [point for collector in collectors[index][1].values()
                          for point in collector.points()]
            points.sort(key=lambda point: (point["timestamp"], point.get("cell_number", 0)))
            projected.append({"metric": metric, "cell_number": cell_number,
                              "cell_numbers": list(selected_cells or ()), "points": points,
                              "raw_points": raw_points[index]})
        downsample_seconds = time.perf_counter() - downsample_started
        if timing:
            timing.record("downsampling", downsample_seconds,
                          time.thread_time() - downsample_cpu, accounting=True)
            timing.counts(cache_hit=False, cache_miss=True,
                cell_files_considered=considered, cell_files_opened=opened,
                cell_bytes_read=bytes_read, cell_raw_lines=raw_file_records,
                cell_lines_rejected_before_json=rejected, cell_parsed_records=parsed,
                cell_parse_errors=parse_errors, cell_invalid_records=invalid_records,
                cell_records_in_window=in_window,
                cell_records_for_requested_modules=raw_records,
                cell_seek_mode=(next(iter(seek_modes)) if len(seek_modes) == 1 else "mixed"),
                cell_skipped_bytes=skipped_bytes,
                series_points_before_downsampling=sum(raw_points),
                series_points_after_downsampling=sum(len(x["points"]) for x in projected))
        soc_module_series = [
            {"metric": "soc", "label": f"Modul {number} SOC", "unit": "%",
             "source": "pylontech", "module_number": number,
             "points": collector.points()}
            for number, collector in soc_collectors.items() if collector.points()
        ]
        result = {"series": projected, "samples": samples, "raw_records": raw_records,
                  "raw_file_records": raw_file_records,
                  "file_count": len(paths),
                  "selected_modules": list(selected_modules),
                  "soc_module_series": soc_module_series,
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
