"""Projection-preferred reader for interactive history display series.

Full-resolution JSONL remains authoritative.  This reader validates and reads
each UTC day independently and delegates only missing or invalid days to the
existing full-resolution reader.  It never builds projection data.
"""
from __future__ import annotations

import gzip
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from display_history_projection import (
    AGGREGATION_ALGORITHM_VERSION, CHANNEL_VALUE_FIELDS,
    DISPLAY_PROJECTION_SCHEMA_VERSION, RESOLUTIONS, _unpack,
)
from history_series import (DEFAULT_MAX_DISPLAY_POINTS, SERIES_METRICS,
                            SeriesHistoryError, _ExtremaCollector)


DISPLAY_SOURCE_MODES = frozenset({
    "full_resolution", "display_projection", "display_plus_tail",
    "mixed_display_full", "mixed_display_full_plus_tail",
})
DETAIL_WINDOW_SECONDS = 15 * 60
_ORDER = tuple(RESOLUTIONS)


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _days(timestamp_from: str, timestamp_to: str) -> list[str]:
    first = datetime.fromisoformat(timestamp_from).astimezone(timezone.utc).date()
    last = datetime.fromisoformat(timestamp_to).astimezone(timezone.utc).date()
    return [(first + timedelta(days=index)).isoformat()
            for index in range((last - first).days + 1)]


def _channel_count(requests, modules, include_all_module_soc):
    total = 0
    for request in requests:
        metric = request["metric"]
        if metric not in {"soc", "current", "cell_voltage", "cell_temperature"}:
            return None
        cells = request.get("cell_numbers")
        if metric in {"cell_voltage", "cell_temperature"}:
            count = len(cells) if cells else (1 if request.get("cell_number") else 15)
        else:
            count = 1
        total += len(modules) * count
    if include_all_module_soc and not any(r["metric"] == "soc" for r in requests):
        total += len(modules)
    return max(1, total)


def select_resolution(*, timestamp_from, timestamp_to, requests, module_numbers,
                      max_points=DEFAULT_MAX_DISPLAY_POINTS,
                      include_all_module_soc=False):
    """Choose the finest duration-policy resolution that fits the point budget."""
    duration = max(0.0, _epoch(timestamp_to) - _epoch(timestamp_from))
    channels = _channel_count(requests, module_numbers, include_all_module_soc)
    if duration <= DETAIL_WINDOW_SECONDS or channels is None:
        return None
    if duration <= 86400:
        start = 0
    elif duration <= 7 * 86400:
        start = 1
    elif duration <= 30 * 86400:
        start = 2
    else:
        start = 3
    for resolution in _ORDER[start:]:
        expected = math.ceil(duration / RESOLUTIONS[resolution]) * channels * 4
        if expected <= max_points or resolution == _ORDER[-1]:
            return resolution
    return "60m"


def expand_bucket(bucket: dict) -> list[dict]:
    """Expand first/min/max/last in true timestamp order without aliases."""
    candidates = []
    for order, prefix in enumerate(("first", "min", "max", "last")):
        candidates.append((bucket[f"{prefix}_timestamp"], order,
                           float(bucket[f"{prefix}_value"])))
    candidates.sort(key=lambda item: (_epoch(item[0]), item[1]))
    result, seen = [], set()
    for timestamp, _order, value in candidates:
        key = (timestamp, value)
        if key in seen:
            continue
        seen.add(key)
        point = {"timestamp": timestamp, "value": value,
                 "display_source": f"display_{bucket['resolution']}",
                 "derived": True}
        if bucket.get("physical_serial"):
            point["module_serial"] = bucket["physical_serial"]
        if bucket.get("identity_quality"):
            point["identity_quality"] = bucket["identity_quality"]
        if bucket.get("cell_number"):
            point["cell_number"] = bucket["cell_number"]
        if bucket.get("module_position"):
            point["module_number"] = bucket["module_position"]
        result.append(point)
    return result


class DisplayHistoryReader:
    def __init__(self, display_directory, cell_directory, hycube_directory,
                 full_series, full_hycube=None):
        self.display_directory = Path(display_directory)
        self.cell_directory = Path(cell_directory)
        self.hycube_directory = Path(hycube_directory)
        self.full_series = full_series
        self.full_hycube = full_hycube

    def _paths(self, resolution, day):
        directory = self.display_directory / resolution
        return (directory / f"{day}.jsonl.gz",
                directory / f"{day}.meta.json")

    def _valid(self, resolution, day):
        data, meta_path = self._paths(resolution, day)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (meta["display_projection_schema_version"] !=
                    DISPLAY_PROJECTION_SCHEMA_VERSION or
                    meta["aggregation_algorithm_version"] !=
                    AGGREGATION_ALGORITHM_VERSION or
                    meta["resolution"] != resolution or meta["day"] != day or
                    meta["status"] not in {"complete", "open"} or
                    not data.is_file()):
                return None
            for kind, expected in meta.get("source_signatures", {}).items():
                source_dir = self.cell_directory if kind == "cell" else self.hycube_directory
                source = source_dir / expected["filename"]
                stat = source.stat()
                if (stat.st_size != expected["size"] or
                        stat.st_mtime_ns != expected["mtime_ns"] or
                        expected.get("source_schema_version") != 1):
                    return None
            return meta
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read(self, resolution, day):
        data, _meta = self._paths(resolution, day)
        try:
            with gzip.open(data, "rt", encoding="utf-8") as handle:
                packed = [json.loads(line) for line in handle if line.strip()]
            return _unpack(packed), data.stat().st_size
        except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError,
                KeyError, TypeError, ValueError) as exc:
            raise SeriesHistoryError("display history is invalid") from exc

    @staticmethod
    def _requested(bucket, requests, modules):
        if bucket.get("source") != "cell_history":
            return False
        if bucket.get("module_position") not in modules:
            return False
        for request in requests:
            if bucket.get("metric") != request["metric"]:
                continue
            if bucket["metric"] in {"cell_voltage", "cell_temperature"}:
                cells = request.get("cell_numbers")
                cell = request.get("cell_number")
                if cells and bucket.get("cell_number") not in cells:
                    continue
                if cell and bucket.get("cell_number") != cell:
                    continue
            return True
        return False

    def query_bundles(self, *, requests, timestamp_from, timestamp_to,
                      module_number=None, module_numbers=None,
                      max_points=DEFAULT_MAX_DISPLAY_POINTS,
                      include_all_module_soc=False, timing=None):
        modules = tuple(sorted(set(module_numbers or (module_number,))))
        resolution = select_resolution(timestamp_from=timestamp_from,
            timestamp_to=timestamp_to, requests=requests, module_numbers=modules,
            max_points=max_points, include_all_module_soc=include_all_module_soc)
        if resolution is None:
            started=time.perf_counter();cpu=time.thread_time()
            result = self.full_series.query_bundles(requests=requests,
                timestamp_from=timestamp_from, timestamp_to=timestamp_to,
                module_number=module_number, module_numbers=modules,
                max_points=max_points, include_all_module_soc=include_all_module_soc,
                timing=timing)
            if timing:
                timing.not_executed("display_projection_discovery",
                                    "display_projection_read",
                                    "display_projection_expand", "current_day_tail")
                timing.record("full_resolution_fallback",time.perf_counter()-started,
                              time.thread_time()-cpu,accounting=False)
            result["display_observability"] = {"display_resolution": None,
                "display_days": 0, "display_bytes": 0, "display_buckets": 0,
                "display_points_expanded": 0, "fallback_days": len(_days(timestamp_from,timestamp_to)),
                "fallback_bytes": result.get("bytes_read", 0), "tail_records": 0,
                "history_source_mode": "full_resolution"}
            return result

        start_epoch, end_epoch = _epoch(timestamp_from), _epoch(timestamp_to)
        display_records=[]; display_bytes=display_buckets=expanded_count=0
        fallback_days=[]; tails=[]; display_days=[]
        discovery_started=time.perf_counter(); discovery_cpu=time.thread_time()
        decisions=[]
        for day in _days(timestamp_from,timestamp_to):
            if not (self.cell_directory / f"{day}.jsonl").is_file():
                continue
            meta=self._valid(resolution,day)
            decisions.append((day,meta))
        if timing: timing.record("display_projection_discovery",
            time.perf_counter()-discovery_started,time.thread_time()-discovery_cpu)
        read_started=time.perf_counter(); read_cpu=time.thread_time()
        for day,meta in decisions:
            if meta is None:
                fallback_days.append(day);continue
            try: records,size=self._read(resolution,day)
            except SeriesHistoryError:
                fallback_days.append(day);continue
            relevant=[r for r in records if self._requested(r,requests,modules)]
            if not relevant:
                fallback_days.append(day);continue
            display_days.append(day);display_bytes+=size;display_buckets+=len(relevant)
            # Open days expose only immutable closed buckets in the gzip file.
            boundary=max((_epoch(r["bucket_end"]) for r in relevant),default=None)
            display_records.extend(r for r in relevant if
                                   _epoch(r["bucket_end"])>=start_epoch and
                                   _epoch(r["bucket_start"])<=end_epoch)
            if meta.get("status")=="open":
                tails.append((day,boundary))
        if timing: timing.record("display_projection_read",
            time.perf_counter()-read_started,time.thread_time()-read_cpu)

        expand_started=time.perf_counter();expand_cpu=time.thread_time()
        grouped={metric:[] for metric in [r["metric"] for r in requests]}
        for bucket in display_records:
            points=[p for p in expand_bucket(bucket)
                    if start_epoch<=_epoch(p["timestamp"])<=end_epoch]
            points=[{key:value for key,value in point.items()
                     if key not in {"display_source","derived"}} for point in points]
            grouped[bucket["metric"]].extend(points);expanded_count+=len(points)
        if timing: timing.record("display_projection_expand",
            time.perf_counter()-expand_started,time.thread_time()-expand_cpu)

        fallback_results=[]; fallback_bytes=tail_records=0
        def full_day(day, lower=None):
            nonlocal fallback_bytes,tail_records
            day_start=datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            lo=max(start_epoch,day_start.timestamp(),lower or float("-inf"))
            hi=min(end_epoch,(day_start+timedelta(days=1)).timestamp()-1e-6)
            if lo>hi:return None
            result=self.full_series.query_bundles(requests=requests,
                timestamp_from=datetime.fromtimestamp(lo,timezone.utc).isoformat(),
                timestamp_to=datetime.fromtimestamp(hi,timezone.utc).isoformat(),
                module_number=module_number,module_numbers=modules,max_points=max_points,
                include_all_module_soc=include_all_module_soc,timing=None)
            fallback_bytes+=result.get("bytes_read",0)
            if lower is not None:tail_records+=result["raw_records"]
            return result
        fallback_started=time.perf_counter();fallback_cpu=time.thread_time()
        for day in fallback_days:
            result=full_day(day)
            if result:fallback_results.append(result)
        if timing: timing.record("full_resolution_fallback",
            time.perf_counter()-fallback_started,time.thread_time()-fallback_cpu)
        tail_started=time.perf_counter();tail_cpu=time.thread_time()
        for day,boundary in tails:
            result=full_day(day,boundary)
            if result:fallback_results.append(result)
        if timing: timing.record("current_day_tail",
            time.perf_counter()-tail_started,time.thread_time()-tail_cpu)

        for result in fallback_results:
            for item in result["series"]:grouped[item["metric"]].extend(item["points"])
        projected=[]
        for request in requests:
            metric=request["metric"]; points=grouped[metric]
            # Exact duplicates at the display/tail boundary collapse; distinct evidence remains.
            unique={json.dumps(p,sort_keys=True,separators=(",",":")):p for p in points}
            points=sorted(unique.values(),key=lambda p:(p["timestamp"],p.get("module_number",0),p.get("cell_number",0)))
            groups={}
            group_count=max(1,len({(point.get("module_number",modules[0]),
                                   point.get("cell_number",0)) for point in points}))
            for point in points:
                group=(point.get("module_number",modules[0]),point.get("cell_number",0))
                collector=groups.setdefault(group,_ExtremaCollector(
                    max(4,max_points//group_count),start_epoch,end_epoch))
                collector.add({**point,"_epoch":_epoch(point["timestamp"])})
            points=sorted([point for collector in groups.values()
                           for point in collector.points()],key=lambda p:(
                           p["timestamp"],p.get("module_number",0),p.get("cell_number",0)))
            if len(modules) == 1:
                points=[{key:value for key,value in point.items()
                         if key != "module_number"} for point in points]
            projected.append({"metric":metric,"cell_number":request.get("cell_number"),
                "cell_numbers":list(request.get("cell_numbers") or ()),"points":points,
                "raw_points":sum(len(i["points"]) for result in fallback_results
                                  for i in result["series"] if i["metric"]==metric)
                             + sum(1 for r in display_records if r["metric"]==metric)})
        has_display=bool(display_days);has_full=bool(fallback_days);has_tail=bool(tails)
        mode=("mixed_display_full_plus_tail" if has_display and has_full and has_tail else
              "mixed_display_full" if has_display and has_full else
              "display_plus_tail" if has_display and has_tail else
              "display_projection" if has_display else "full_resolution")
        obs={"display_resolution":resolution,"display_days":len(display_days),
             "display_bytes":display_bytes,"display_buckets":display_buckets,
             "display_points_expanded":expanded_count,"fallback_days":len(fallback_days),
             "fallback_bytes":fallback_bytes,"tail_records":tail_records,
             "history_source_mode":mode}
        if timing:timing.counts(**obs)
        soc_module_series=[]
        if include_all_module_soc:
            soc_points=grouped.get("soc",[])
            for number in modules:
                selected=[p for p in soc_points if p.get("module_number",number)==number]
                if selected:
                    soc_module_series.append({"metric":"soc","label":f"Modul {number} SOC",
                        "unit":"%","source":"pylontech","module_number":number,
                        "points":selected})
        return {"series":projected,"samples":[],
            "raw_records":sum(r["raw_records"] for r in fallback_results),
            "raw_file_records":sum(r.get("raw_file_records",r["raw_records"]) for r in fallback_results),
            "file_count":len(display_days)+len(fallback_days),"selected_modules":list(modules),
            "soc_module_series":soc_module_series,"read_seconds":0.0,"downsample_seconds":0.0,
            "cache_hit":False,"display_observability":obs}

    def query_hycube(self, *, timestamp_from, timestamp_to,
                     max_points=DEFAULT_MAX_DISPLAY_POINTS, timing=None):
        resolution=select_resolution(timestamp_from=timestamp_from,timestamp_to=timestamp_to,
            requests=({"metric":"soc"},),module_numbers=(1,),max_points=max_points)
        if resolution is None:return None
        start,end=_epoch(timestamp_from),_epoch(timestamp_to);points=[];raw=files=bytes_read=0
        used_display=used_full=used_tail=False
        for day in _days(timestamp_from,timestamp_to):
            if not (self.hycube_directory/f"{day}.jsonl").is_file():
                if self.full_hycube is None:continue
                day_start=datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                day_end=min(end,(day_start+timedelta(days=1)).timestamp()-1e-6)
                fallback=self.full_hycube.query(timestamp_from=datetime.fromtimestamp(
                    max(start,day_start.timestamp()),timezone.utc).isoformat(),
                    timestamp_to=datetime.fromtimestamp(day_end,timezone.utc).isoformat(),
                    max_points=max_points,timing=None)
                points.extend(fallback["points"]);raw+=fallback["raw_records"]
                used_full=True;continue
            meta=self._valid(resolution,day)
            day_start=datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            day_end=min(end,(day_start+timedelta(days=1)).timestamp()-1e-6)
            def fallback_day(lower=None):
                if self.full_hycube is None:return False
                fallback=self.full_hycube.query(timestamp_from=datetime.fromtimestamp(
                    max(start,day_start.timestamp(),lower or float("-inf")),timezone.utc).isoformat(),
                    timestamp_to=datetime.fromtimestamp(day_end,timezone.utc).isoformat(),
                    max_points=max_points,timing=None)
                points.extend(fallback["points"]);return fallback["raw_records"]
            if meta is None or "hycube" not in meta.get("source_signatures",{}):
                if self.full_hycube is None:return None
                raw+=fallback_day();used_full=True;continue
            try:records,size=self._read(resolution,day)
            except SeriesHistoryError:
                if self.full_hycube is None:return None
                raw+=fallback_day();used_full=True;continue
            if meta is None:
                continue
            files+=1;bytes_read+=size;day_points=[];boundary=None;used_display=True
            for bucket in records:
                if bucket.get("source")!="hycube" or bucket.get("metric")!="hycube_soc":continue
                raw+=1;boundary=max(boundary or float("-inf"),_epoch(bucket["bucket_end"]))
                for point in expand_bucket(bucket):
                    if start<=_epoch(point["timestamp"])<=end:
                        day_points.append({**{key:value for key,value in point.items()
                                             if key not in {"display_source","derived"}},
                            "source":"hycube","source_field":"BatteryCapacity"})
            points.extend(day_points)
            if meta.get("status")=="open" and self.full_hycube is not None and boundary:
                raw+=fallback_day(boundary);used_tail=True
        points.sort(key=lambda p:p["timestamp"])
        collector=_ExtremaCollector(max_points,start,end)
        for point in points:collector.add({**point,"_epoch":_epoch(point["timestamp"])})
        points=collector.points()
        source_mode=("mixed_display_full_plus_tail" if used_display and used_full and used_tail
                     else "mixed_display_full" if used_display and used_full
                     else "display_plus_tail" if used_display and used_tail
                     else f"display_{resolution}" if used_display else "full_resolution")
        return {"metric":"hycube_battery_capacity","label":"Hycube BatteryCapacity",
            "unit":"%","source":"hycube","points":points,"raw_records":raw,"file_count":files,
            "read_seconds":0.0,"downsample_seconds":0.0,"cache_hit":False,
            "source_mode":source_mode,"display_bytes":bytes_read}
