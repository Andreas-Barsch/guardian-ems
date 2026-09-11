"""Rebuildable continuous phase timeline derived from cell history.

Raw cell history remains authoritative.  This module is deliberately isolated
from acquisition and can be rebuilt or discarded at any time.
"""
from __future__ import annotations

import gzip
import copy
import hashlib
import json
import os
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path

from cell_diagnostics import classify_phases
from position_history import PositionHistoryLog


SCHEMA_VERSION = 1
SEMANTICS_VERSION = "guardian_canonical_phase_v2"
ALGORITHM_VERSION = "guardian_phase_stream_v2_1"
DEFAULT_CANONICAL_PHASE_DIR = Path("/share/guardian_battery/canonical_phase_v2")
DEFAULT_PARAMETERS = {"minimum_duration_seconds": 180.0,
                      "current_hysteresis_a": 0.2,
                      "short_gap_seconds": 120.0}


class CanonicalPhaseError(ValueError):
    pass


class IdentityEpochResolver:
    """Resolve documented occupancy epochs without treating position as identity."""
    def __init__(self, position_history_path=None):
        self.snapshots=[]
        if position_history_path is not None:
            try:self.snapshots=sorted(PositionHistoryLog(position_history_path).read_all(),
                key=lambda item:(item.effective_at,item.created_at,item.position_history_id))
            except Exception:self.snapshots=[]
    def resolve(self, record, timestamp):
        serial=record.get("module_serial")
        if not serial:
            return f"unresolved-position-{int(record['module'])}",None
        present=False;start="observed";seen=False
        for snapshot in self.snapshots:
            if _epoch(snapshot.effective_at)>timestamp:break
            occupied=serial in snapshot.positions.values()
            if occupied and not present and seen:start=snapshot.effective_at
            present=occupied;seen=True
        return f"{serial}@{start}",serial


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _iso(value):
    return datetime.fromtimestamp(_epoch(value), timezone.utc).isoformat()


def _mean_mv(sample):
    values = [float(value) for value in sample["voltages_mv"]]
    return sum(values) / len(values)


def _atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")),
                         encoding="utf-8")
    os.replace(temporary, path)


def _atomic_gzip(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode())
    os.replace(temporary, path)


def _signature(path):
    path = Path(path); digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024): digest.update(chunk)
    stat = path.stat()
    return {"filename": path.name, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


class CanonicalPhaseMachine:
    """Streaming state machine with a JSON-safe exact continuation state."""

    def __init__(self, identity_epoch_id, physical_serial=None, *, parameters=None):
        self.identity_epoch_id = str(identity_epoch_id)
        self.physical_serial = physical_serial
        self.parameters = {**DEFAULT_PARAMETERS, **(parameters or {})}
        self.axis_previous = "rest"
        self.stable_axis = None
        self.candidate_axis = None
        self.candidate_started_at = None
        self.pending_diagnostic = None
        self.visual_segments = []
        self.final_diagnostic = []
        self.final_visual = []
        self.endpoints = []
        self.endpoint_state = None
        self.last_timestamp = None
        self.last_order = None
        self.config_revision = None
        self.source_cursor = None
        self.source_signature = None
        self.current_position = None

    def _axis(self, current, options):
        charge = float(options["cell_diag_charge_current_a"])
        discharge = float(options["cell_diag_discharge_current_a"])
        hysteresis = float(self.parameters["current_hysteresis_a"])
        if self.axis_previous == "charge" and current >= charge-hysteresis:
            return "charge"
        if self.axis_previous == "discharge" and current <= -discharge+hysteresis:
            return "discharge"
        if current >= charge+hysteresis: return "charge"
        if current <= -discharge-hysteresis: return "discharge"
        return "rest"

    @staticmethod
    def _interval(start, phase, phases, config_id, position):
        return {"canonical_from": start, "canonical_to": None,
                "phase": phase, "phases": list(phases),
                "canonical_sample_count": 0, "count_timestamps": [],
                "config_id": config_id, "module_position": position}

    def _diagnostic(self, timestamp, phases, config_id, position):
        key = "+".join(phases)
        current = self.pending_diagnostic
        if current and (current["phase"] != key or current.get("config_id") != config_id or
                        current.get("module_position") != position):
            current["canonical_to"] = timestamp
            self.final_diagnostic.append(current)
            current = None
        if current is None:
            current = self._interval(timestamp, key, phases, config_id, position)
            self.pending_diagnostic = current
        current["canonical_sample_count"] += 1
        current["count_timestamps"].append(_epoch(timestamp))

    def _append_visual(self, timestamp, phase, phases, config_id, position):
        current = self.visual_segments[-1] if self.visual_segments else None
        if (current and current["phase"] == phase and current.get("config_id") == config_id
                and current.get("module_position") == position):
            current["canonical_sample_count"] += 1
            current["count_timestamps"].append(_epoch(timestamp))
            return
        if current: current["canonical_to"] = timestamp
        item = self._interval(timestamp, phase, phases, config_id, position)
        item["canonical_sample_count"] = 1
        item["count_timestamps"] = [_epoch(timestamp)]
        self.visual_segments.append(item)
        self._reduce_visual()

    def _reduce_visual(self):
        gap = float(self.parameters["short_gap_seconds"])
        changed = True
        while changed and len(self.visual_segments) >= 3:
            changed = False
            for index in range(len(self.visual_segments)-2):
                left, middle, right = self.visual_segments[index:index+3]
                duration = (_epoch(middle["canonical_to"])-
                            _epoch(middle["canonical_from"]))
                if (left["phase"] == right["phase"] and duration <= gap and
                        left.get("config_id") == right.get("config_id") and
                        left.get("module_position") == right.get("module_position")):
                    merged = dict(left)
                    merged["canonical_to"] = right["canonical_to"]
                    merged["canonical_sample_count"] = sum(
                        item["canonical_sample_count"] for item in (left, middle, right))
                    merged["count_timestamps"] = [value for item in
                        (left, middle, right) for value in item["count_timestamps"]]
                    self.visual_segments[index:index+3] = [merged]
                    changed = True
                    break
        while len(self.visual_segments) > 2:
            self.final_visual.append(self.visual_segments.pop(0))

    def _relative_endpoint(self, sample, timestamp, options):
        current = float(sample["current_a"])
        charge = float(options["cell_diag_charge_current_a"])
        discharge = float(options["cell_diag_discharge_current_a"])
        axis = "charge" if current >= charge else "discharge" if current <= -discharge else None
        state = self.endpoint_state
        gap = _epoch(timestamp)-_epoch(state["last_timestamp"]) if state else 0
        if state and (axis != state["axis"] or gap > 180.0):
            if state["sample_count"] >= 3:
                absolute = (state["last_soc_percent"] <= state["low_soc_percent"]
                            if state["axis"] == "discharge" else
                            state["last_soc_percent"] >= state["high_soc_percent"])
                self.endpoints.append({"timestamp": state["last_timestamp"],
                    "kind": "relative_low_point" if state["axis"] == "discharge" else "relative_high_point",
                    "axis": state["axis"], "soc_percent": state["last_soc_percent"],
                    "mean_cell_voltage_mv": round(state["last_mean_cell_voltage_mv"], 2),
                    "sample_count": state["sample_count"],
                    "absolute_region_reached": absolute,
                    "evidence_level": "observation", "causality": "not_determined",
                    "bms_limit_confirmed": False,
                    "module_position": state.get("module_position")})
            state = None
        if axis:
            if state is None:
                state = {"axis": axis, "sample_count": 0}
            state.update(sample_count=state["sample_count"]+1,
                last_timestamp=timestamp, last_soc_percent=float(sample["soc_percent"]),
                last_mean_cell_voltage_mv=_mean_mv(sample),
                low_soc_percent=float(options["cell_diag_low_soc_percent"]),
                high_soc_percent=float(options["cell_diag_high_soc_percent"]))
            state["module_position"]=int(sample["module"]) if sample.get("module") is not None else None
        self.endpoint_state = state

    def process(self, sample, options, *, config_id=None, source_cursor=None,
                source_signature=None):
        timestamp = _iso(sample["timestamp"])
        order = tuple(source_cursor.get(key) for key in ("source_day", "source_offset")) \
            if isinstance(source_cursor, dict) else None
        if self.last_timestamp is not None and (_epoch(timestamp), order or ()) < (
                _epoch(self.last_timestamp), self.last_order or ()):
            raise CanonicalPhaseError("samples are not in canonical order")
        phases = classify_phases(sample, options) if options else ["unknown"]
        position=int(sample["module"]) if sample.get("module") is not None else None
        self._diagnostic(timestamp, phases, config_id, position)
        if options:
            axis = self._axis(float(sample["current_a"]), options)
            self.axis_previous = axis
            soc = "low" if "low" in phases else "high" if "high" in phases else None
        else:
            axis = "unknown"; self.axis_previous = axis; soc = None
        if self.stable_axis is None: self.stable_axis = axis
        if axis == self.stable_axis:
            self.candidate_axis = self.candidate_started_at = None
        elif axis != self.candidate_axis:
            self.candidate_axis = axis; self.candidate_started_at = timestamp
        elif _epoch(timestamp)-_epoch(self.candidate_started_at) >= float(
                self.parameters["minimum_duration_seconds"]):
            self.stable_axis = self.candidate_axis
            self.candidate_axis = self.candidate_started_at = None
        visual_phases = [self.stable_axis] + ([soc] if soc else [])
        self._append_visual(timestamp, "+".join(visual_phases), visual_phases, config_id,
                            position)
        if options: self._relative_endpoint(sample, timestamp, options)
        self.last_timestamp = timestamp; self.last_order = order
        self.config_revision = config_id; self.source_cursor = source_cursor
        self.source_signature = source_signature
        self.current_position = position

    def snapshot(self):
        return {"schema_version": SCHEMA_VERSION, "semantics_version": SEMANTICS_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "checkpoint_timestamp": self.last_timestamp,
            "identity_epoch_id": self.identity_epoch_id,
            "physical_serial": self.physical_serial,
            "source_signature": self.source_signature, "source_cursor": self.source_cursor,
            "last_sample_timestamp": self.last_timestamp, "tie_break_ordinal": self.last_order,
            "config_revision": self.config_revision,
            "current_position": self.current_position,
            "visual_state": {"axis_previous": self.axis_previous,
                "stable_axis": self.stable_axis, "candidate_axis": self.candidate_axis,
                "candidate_started_at": self.candidate_started_at},
            "pending_diagnostic_interval": self.pending_diagnostic,
            "pending_visual_suffix": self.visual_segments,
            "relative_endpoint_state": self.endpoint_state}

    @classmethod
    def restore(cls, value, *, parameters=None):
        if (value.get("schema_version") != SCHEMA_VERSION or
                value.get("semantics_version") != SEMANTICS_VERSION or
                value.get("algorithm_version") != ALGORITHM_VERSION):
            raise CanonicalPhaseError("invalid canonical phase checkpoint")
        machine = cls(value["identity_epoch_id"], value.get("physical_serial"),
                      parameters=parameters)
        visual = value["visual_state"]
        machine.axis_previous = visual["axis_previous"]
        machine.stable_axis = visual["stable_axis"]
        machine.candidate_axis = visual.get("candidate_axis")
        machine.candidate_started_at = visual.get("candidate_started_at")
        machine.pending_diagnostic = value.get("pending_diagnostic_interval")
        machine.visual_segments = value.get("pending_visual_suffix", [])
        machine.endpoint_state = value.get("relative_endpoint_state")
        machine.last_timestamp = value.get("last_sample_timestamp")
        order = value.get("tie_break_ordinal")
        machine.last_order = tuple(order) if order is not None else None
        machine.config_revision = value.get("config_revision")
        machine.source_cursor = value.get("source_cursor")
        machine.source_signature = value.get("source_signature")
        machine.current_position = value.get("current_position")
        return machine

    def materialized(self, *, window_to=None):
        diagnostic = list(self.final_diagnostic)
        if self.pending_diagnostic:
            item = dict(self.pending_diagnostic)
            item["canonical_to"] = None
            diagnostic.append(item)
        visual = list(self.final_visual) + [dict(item) for item in self.visual_segments]
        for item in visual:
            if item["canonical_to"] is None and window_to is not None:
                item["open_to"] = _iso(window_to)
        return {"diagnostic_intervals": diagnostic, "visual_intervals": visual,
                "relative_endpoints": list(self.endpoints), "checkpoint": self.snapshot()}


def clip_interval(item, timestamp_from, timestamp_to):
    start, end = _epoch(timestamp_from), _epoch(timestamp_to)
    canonical_start = _epoch(item["canonical_from"])
    canonical_end = (_epoch(item["canonical_to"]) if item.get("canonical_to") else end)
    if canonical_end <= start or canonical_start > end: return None
    visible_from = max(start, canonical_start); visible_to = min(end, canonical_end)
    timestamps = item.get("count_timestamps", [])
    visible_count = bisect_right(timestamps, end)-bisect_left(timestamps, start)
    return {**{key: value for key, value in item.items() if key != "count_timestamps"},
        "from": _iso(visible_from), "to": _iso(visible_to),
        "canonical_from": item["canonical_from"],
        "canonical_to": item.get("canonical_to"), "sample_count": visible_count,
        "canonical_sample_count": item["canonical_sample_count"],
        "semantics_version": SEMANTICS_VERSION}


class CanonicalPhaseStore:
    """Atomic, daily, disposable storage for canonical phase artifacts."""

    def __init__(self, directory): self.directory = Path(directory)
    def path(self, day): return self.directory / f"{day}.json.gz"
    def write_day(self, day, artifact): _atomic_gzip(self.path(day), artifact)
    def read_day(self, day):
        try:
            with gzip.open(self.path(day), "rt", encoding="utf-8") as handle: value=json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise CanonicalPhaseError("corrupt canonical phase artifact") from exc
        if value.get("schema_version") != SCHEMA_VERSION: raise CanonicalPhaseError("invalid_schema")
        if value.get("semantics_version") != SEMANTICS_VERSION: raise CanonicalPhaseError("invalid_semantics_version")
        if value.get("algorithm_version") != ALGORITHM_VERSION: raise CanonicalPhaseError("invalid_algorithm_version")
        return value


class CanonicalPhaseProjection:
    """Bounded explicit builder. It never performs hardware or network access."""

    def __init__(self, cell_directory, output_directory, config_history, *,
                 position_history_path=None, clock=time.time):
        self.cell_directory=Path(cell_directory); self.store=CanonicalPhaseStore(output_directory)
        self.config_history=config_history; self.clock=clock
        self.identity_resolver=IdentityEpochResolver(position_history_path)

    def _configs(self):
        records=self.config_history.records()
        return records, [record["timestamp"] for record in records]

    def _current_state_path(self): return self.store.directory / ".current.json"

    @staticmethod
    def _drain(machine):
        result={"diagnostic_intervals":machine.final_diagnostic,
                "visual_intervals":machine.final_visual,
                "relative_endpoints":machine.endpoints}
        machine.final_diagnostic=[];machine.final_visual=[];machine.endpoints=[]
        return result

    def update_current(self, day=None):
        """Consume only the append tail of today's authoritative cell file."""
        day=day or datetime.fromtimestamp(self.clock(),timezone.utc).date().isoformat()
        source=self.cell_directory/f"{day}.jsonl"
        if not source.is_file(): return {"day":day,"samples":0,"bytes":0,"available":False}
        state={"schema_version":SCHEMA_VERSION,"semantics_version":SEMANTICS_VERSION,
               "algorithm_version":ALGORITHM_VERSION,"day":day,"offset":0,
               "machines":{},"emitted":{}}
        try:
            loaded=json.loads(self._current_state_path().read_text(encoding="utf-8"))
            if (loaded.get("schema_version")==SCHEMA_VERSION and loaded.get("day")==day and
                    loaded.get("semantics_version")==SEMANTICS_VERSION and
                    loaded.get("algorithm_version")==ALGORITHM_VERSION and
                    int(loaded.get("offset",0))<=source.stat().st_size): state=loaded
        except (FileNotFoundError,json.JSONDecodeError,OSError,TypeError,ValueError): pass
        machines={key:CanonicalPhaseMachine.restore(value)
                  for key,value in state["machines"].items()}
        if not machines and state["offset"]==0:
            previous=sorted(path for path in self.store.directory.glob("*.json.gz")
                            if path.stem.split(".")[0]<day)
            if previous:
                try:
                    artifact=self.store.read_day(previous[-1].name.removesuffix(".json.gz"))
                    for key,value in artifact.get("identities",{}).items():
                        if value.get("eod_checkpoint"):
                            machines[key]=CanonicalPhaseMachine.restore(value["eod_checkpoint"])
                except CanonicalPhaseError: pass
        started=time.perf_counter();cpu=time.thread_time();records=[]
        old_offset=int(state["offset"])
        with source.open("rb") as handle:
            handle.seek(old_offset)
            while True:
                offset=handle.tell();raw=handle.readline()
                if not raw:break
                if raw.strip():
                    record=json.loads(raw);timestamp=float(record["timestamp"])
                    identity,serial=self.identity_resolver.resolve(record,timestamp)
                    records.append((timestamp,day,offset,identity,serial,record))
            new_offset=handle.tell()
        records.sort(key=lambda item:(item[0],item[1],item[2]))
        configs,times=self._configs()
        emitted=state["emitted"]
        for timestamp,source_day,offset,identity,serial,record in records:
            machine=machines.setdefault(identity,CanonicalPhaseMachine(identity,serial))
            options,config_id=self._resolve(configs,times,_iso(timestamp))
            machine.process(record,options,config_id=config_id,
                source_cursor={"source_day":source_day,"source_offset":offset},
                source_signature={"filename":source.name,"size":source.stat().st_size,
                                  "mtime_ns":source.stat().st_mtime_ns,"sha256":None})
            target=emitted.setdefault(identity,{"diagnostic_intervals":[],
                "visual_intervals":[],"relative_endpoints":[],"module_positions":[]})
            if int(record["module"]) not in target["module_positions"]:
                target["module_positions"].append(int(record["module"]))
            drained=self._drain(machine)
            for name,values in drained.items():target[name].extend(values)
        signature={"filename":source.name,"size":source.stat().st_size,
                   "mtime_ns":source.stat().st_mtime_ns,"sha256":None}
        identities={}
        for identity,machine in machines.items():
            pending=machine.materialized();base=emitted.get(identity,{})
            identities[identity]={"identity_epoch_id":identity,
                "physical_serial":machine.physical_serial,
                "module_positions":sorted(base.get("module_positions",[])),
                "diagnostic_intervals":base.get("diagnostic_intervals",[])+pending["diagnostic_intervals"],
                "visual_intervals":base.get("visual_intervals",[])+pending["visual_intervals"],
                "relative_endpoints":base.get("relative_endpoints",[])+pending["relative_endpoints"],
                "eod_checkpoint":machine.snapshot()}
        config_signature=(_signature(self.config_history.path)
                          if self.config_history.path.is_file() else None)
        artifact={"schema_version":SCHEMA_VERSION,"semantics_version":SEMANTICS_VERSION,
            "algorithm_version":ALGORITHM_VERSION,"day":day,"complete":False,
            "source_signatures":{day:signature},"config_signature":config_signature,
            "identities":identities}
        self.store.write_day(day,artifact)
        state.update(offset=new_offset,machines={key:value.snapshot() for key,value in machines.items()},
                     emitted=emitted,source_signature=signature)
        _atomic_json(self._current_state_path(),state)
        return {"day":day,"samples":len(records),"bytes":new_offset-old_offset,
                "available":True,"wall_seconds":time.perf_counter()-started,
                "thread_cpu_seconds":time.thread_time()-cpu}

    @staticmethod
    def _resolve(records, times, timestamp):
        index=bisect_right(times,timestamp)-1
        return (records[index]["parameters"], records[index]["config_id"]) if index>=0 else (None,None)

    def _progress_path(self): return self.store.directory/".rebuild.json"

    @staticmethod
    def _clear_daily_counts(machine):
        if machine.pending_diagnostic is not None:
            machine.pending_diagnostic["count_timestamps"]=[]
        for item in machine.visual_segments:item["count_timestamps"]=[]

    def build(self, days=None, *, stop_after_days=None):
        """Process one source day at a time and publish a resumable EOD state."""
        started=time.perf_counter();cpu=time.thread_time()
        paths=sorted(self.cell_directory.glob("*.jsonl"))
        # Today's append-only file belongs exclusively to update_current().  A
        # historical rebuild must never publish a partial day as complete.
        current_day=datetime.fromtimestamp(self.clock()).date().isoformat()
        paths=[path for path in paths if path.stem<current_day]
        if days is not None:
            selected=set(days);paths=[path for path in paths if path.stem in selected]
        configs,times=self._configs()
        config_signature=(_signature(self.config_history.path)
                          if self.config_history.path.is_file() else None)
        signatures={path.stem:_signature(path) for path in paths}
        machines={};positions={};start_index=0;days_reused=0
        # Reuse only a contiguous, fully validated prefix. Its EOD checkpoint is
        # the exact continuation state for the first day that must be rebuilt.
        for index,path in enumerate(paths):
            try:artifact=self.store.read_day(path.stem)
            except (FileNotFoundError,CanonicalPhaseError):break
            if (artifact.get("source_signatures",{}).get(path.stem)!=signatures[path.stem]
                    or artifact.get("config_signature")!=config_signature
                    or not artifact.get("complete")):break
            machines={key:CanonicalPhaseMachine.restore(value["eod_checkpoint"])
                      for key,value in artifact["identities"].items()
                      if value.get("eod_checkpoint")}
            positions={key:set(value.get("module_positions",()))
                       for key,value in artifact["identities"].items()}
            start_index=index+1;days_reused+=1
        processed=peak_samples=bytes_written=interval_count=total_samples=0
        previous_max=None;propagation_stopped=False
        for path in paths[start_index:]:
            records=[]
            with path.open("rb") as handle:
                while True:
                    offset=handle.tell();raw=handle.readline()
                    if not raw:break
                    if not raw.strip():continue
                    record=json.loads(raw);timestamp=float(record["timestamp"])
                    # This is exactly CellHistoryWriter's source-day contract.
                    if datetime.fromtimestamp(timestamp).date().isoformat()!=path.stem:
                        raise CanonicalPhaseError("record violates cell source-day contract")
                    identity,serial=self.identity_resolver.resolve(record,timestamp)
                    records.append((timestamp,path.stem,offset,identity,serial,record))
            records.sort(key=lambda item:(item[0],item[1],item[2]))
            peak_samples=max(peak_samples,len(records))
            total_samples+=len(records)
            if records and previous_max is not None and records[0][0]<previous_max:
                raise CanonicalPhaseError("cross-day canonical ordering violation")
            if records:previous_max=records[-1][0]
            old_artifact=None
            try:old_artifact=self.store.read_day(path.stem)
            except (FileNotFoundError,CanonicalPhaseError):pass
            for timestamp,day,offset,identity,serial,record in records:
                machine=machines.setdefault(identity,CanonicalPhaseMachine(identity,serial))
                positions.setdefault(identity,set()).add(int(record["module"]))
                options,config_id=self._resolve(configs,times,_iso(timestamp))
                machine.process(record,options,config_id=config_id,
                    source_cursor={"source_day":day,"source_offset":offset},
                    source_signature=signatures[day])
            identities={}
            for identity,machine in machines.items():
                materialized=machine.materialized()
                # Persist only the records produced or still pending at this EOD.
                diagnostic=copy.deepcopy(materialized["diagnostic_intervals"])
                visual=copy.deepcopy(materialized["visual_intervals"])
                endpoints=copy.deepcopy(materialized["relative_endpoints"])
                machine.final_diagnostic=[];machine.final_visual=[];machine.endpoints=[]
                self._clear_daily_counts(machine)
                identities[identity]={"physical_serial":machine.physical_serial,
                    "identity_epoch_id":identity,
                    "module_positions":sorted(positions.get(identity,())),
                    "diagnostic_intervals":diagnostic,"visual_intervals":visual,
                    "relative_endpoints":endpoints,
                    "eod_checkpoint":copy.deepcopy(machine.snapshot())}
            artifact={"schema_version":SCHEMA_VERSION,
                "semantics_version":SEMANTICS_VERSION,"algorithm_version":ALGORITHM_VERSION,
                "day":path.stem,"source_signatures":{path.stem:signatures[path.stem]},
                "config_signature":config_signature,"complete":True,"identities":identities}
            self.store.write_day(path.stem,artifact)
            size=self.store.path(path.stem).stat().st_size;bytes_written+=size
            interval_count+=sum(len(value["diagnostic_intervals"])+
                len(value["visual_intervals"]) for value in identities.values())
            processed+=1
            _atomic_json(self._progress_path(),{"schema_version":SCHEMA_VERSION,
                "semantics_version":SEMANTICS_VERSION,"algorithm_version":ALGORITHM_VERSION,
                "last_completed_day":path.stem,"source_signature":signatures[path.stem],
                "config_signature":config_signature})
            # A crash after artifact publication but before this progress write is
            # harmless: the deterministic day is simply reused or overwritten.
            if (old_artifact is not None and json.dumps(
                    old_artifact.get("identities"),sort_keys=True,separators=(",",":"))==
                    json.dumps(identities,sort_keys=True,separators=(",",":"))):
                propagation_stopped=True;break
            if stop_after_days is not None and processed>=int(stop_after_days):break
            records.clear()
        return {"days":len(paths),"days_rebuilt":processed,"days_reused":days_reused,
            "propagation_stopped":propagation_stopped,"samples":total_samples,
            "peak_sample_objects":peak_samples,"intervals":interval_count,
            "bytes":bytes_written,"wall_seconds":time.perf_counter()-started,
            "thread_cpu_seconds":time.thread_time()-cpu}


class CanonicalPhaseReader:
    def __init__(self, directory, source_directory=None, config_history_path=None):
        self.store=CanonicalPhaseStore(directory)
        self.source_directory=Path(source_directory) if source_directory is not None else None
        self.config_history_path=(Path(config_history_path)
                                  if config_history_path is not None else None)
    @staticmethod
    def _days(start,end):
        first=datetime.fromisoformat(start).astimezone(timezone.utc).date()
        last=datetime.fromisoformat(end).astimezone(timezone.utc).date(); result=[]
        while first<=last: result.append(first.isoformat()); first=first.fromordinal(first.toordinal()+1)
        return result
    def query(self, timestamp_from, timestamp_to, *, physical_serial=None, module_number=None,
              timing=None):
        days=self._days(timestamp_from,timestamp_to); artifacts=[]; bytes_read=0
        started=time.perf_counter();cpu=time.thread_time()
        try:
            for day in days:
                path=self.store.path(day);bytes_read+=path.stat().st_size
                artifact=self.store.read_day(day)
                if self.source_directory is not None:
                    source=self.source_directory/f"{day}.jsonl"
                    expected=artifact.get("source_signatures",{}).get(day)
                    if not source.is_file() or not expected:
                        raise CanonicalPhaseError("source_signature_mismatch")
                    stat=source.stat()
                    if (stat.st_size!=expected.get("size") or
                            stat.st_mtime_ns!=expected.get("mtime_ns")):
                        raise CanonicalPhaseError("source_signature_mismatch")
                if self.config_history_path is not None:
                    expected_config=artifact.get("config_signature")
                    if ((self.config_history_path.is_file()) != bool(expected_config)):
                        raise CanonicalPhaseError("config_mismatch")
                    if expected_config:
                        stat=self.config_history_path.stat()
                        if (stat.st_size!=expected_config.get("size") or
                                stat.st_mtime_ns!=expected_config.get("mtime_ns")):
                            raise CanonicalPhaseError("config_mismatch")
                artifacts.append(artifact)
        except (FileNotFoundError,CanonicalPhaseError) as exc:
            reason=str(exc) if isinstance(exc,CanonicalPhaseError) else "missing"
            return {"available":False,"reason":reason,"days":days}
        if timing: timing.record("canonical_phase_read",time.perf_counter()-started,time.thread_time()-cpu)
        diagnostic=[];visual=[];endpoints=[];identities=set()
        for artifact in artifacts:
            for identity,value in artifact["identities"].items():
                if physical_serial and value.get("physical_serial")!=physical_serial:continue
                if not physical_serial and module_number is not None and module_number not in value.get("module_positions",[]):continue
                identities.add(identity)
                diagnostic.extend(item for item in value["diagnostic_intervals"]
                    if module_number is None or item.get("module_position")==module_number)
                visual.extend(item for item in value["visual_intervals"]
                    if module_number is None or item.get("module_position")==module_number)
                endpoints.extend(item for item in value["relative_endpoints"]
                    if module_number is None or item.get("module_position")==module_number)
        def unique(items):
            values={json.dumps(i,sort_keys=True,separators=(",",":")):i for i in items}
            return list(values.values())
        def coalesce(items):
            groups={}
            for item in items:
                key=(item.get("canonical_from"),item.get("phase"),item.get("config_id"),
                     item.get("module_position"))
                group=groups.setdefault(key,[]);group.append(item)
            result=[]
            for values in groups.values():
                selected=max(values,key=lambda item:(item.get("canonical_to") is not None,
                    item.get("canonical_sample_count",0)))
                selected=copy.deepcopy(selected)
                selected["count_timestamps"]=sorted(set(
                    timestamp for item in values for timestamp in item.get("count_timestamps",())))
                result.append(selected)
            # A later A-B-A reduction supersedes previously persisted fragments.
            reduced=[]
            for item in result:
                start=_epoch(item["canonical_from"])
                end=_epoch(item["canonical_to"]) if item.get("canonical_to") else float("inf")
                superseded=any(other is not item and other.get("phase")==item.get("phase") and
                    _epoch(other["canonical_from"])<=start and
                    (_epoch(other["canonical_to"]) if other.get("canonical_to") else float("inf"))>=end and
                    other.get("canonical_sample_count",0)>item.get("canonical_sample_count",0)
                    for other in result)
                if not superseded:reduced.append(item)
            return reduced
        if not identities:
            return {"available":False,"reason":"identity_mismatch","days":days}
        clip_started=time.perf_counter();clip_cpu=time.thread_time()
        diagnostic=[value for item in coalesce(diagnostic)
                    if (value:=clip_interval(item,timestamp_from,timestamp_to))]
        visual=[value for item in coalesce(visual)
                if (value:=clip_interval(item,timestamp_from,timestamp_to))]
        endpoints=[item for item in unique(endpoints)
                   if _epoch(timestamp_from)<=_epoch(item["timestamp"])<=_epoch(timestamp_to)]
        if timing: timing.record("canonical_phase_clip",time.perf_counter()-clip_started,time.thread_time()-clip_cpu)
        return {"available":True,"reason":None,"days":days,"bytes":bytes_read,
            "identity_epochs":sorted(identities),"diagnostic_intervals":diagnostic,
            "visual_intervals":visual,"relative_endpoints":endpoints,
            "semantics_version":SEMANTICS_VERSION,"source_mode":"canonical_phase_v2"}


class CanonicalPhaseWorker(threading.Thread):
    def __init__(self, projection, *, interval_seconds=60):
        super().__init__(name="guardian-canonical-phase",daemon=True)
        self.projection=projection;self.interval_seconds=float(interval_seconds)
        self.stop_event=threading.Event();self.last_result=None;self.last_error=None
        self.rebuild_event=threading.Event();self.rebuild_active=False
    def request_historical_rebuild(self):
        if self.rebuild_event.is_set() or self.rebuild_active:return False
        self.rebuild_event.set();return True
    def status(self):
        return {"enabled":True,"active":self.is_alive(),"rebuild_active":self.rebuild_active,
                "rebuild_requested":self.rebuild_event.is_set(),
                "last_result":self.last_result,"last_error":self.last_error,
                "semantics_version":SEMANTICS_VERSION,"algorithm_version":ALGORITHM_VERSION}
    def run(self):
        while not self.stop_event.is_set():
            try:
                if self.rebuild_event.is_set():
                    self.rebuild_active=True;self.rebuild_event.clear()
                    self.last_result=self.projection.build()
                else:self.last_result=self.projection.update_current()
                self.last_error=None
            except Exception as exc:self.last_error=f"{type(exc).__name__}: {exc}"
            finally:self.rebuild_active=False
            self.stop_event.wait(self.interval_seconds)
    def stop(self,timeout=5):self.stop_event.set();self.join(timeout);return not self.is_alive()


def migration_report(legacy, canonical):
    """Return deterministic structural evidence; it never labels changes regressions."""
    def intervals(value,name): return list(value.get(name,()))
    def shifts(old,new):
        result=[]
        for left,right in zip(old,new):
            result.extend((abs(_epoch(left["from"])-_epoch(right["from"])),
                           abs(_epoch(left["to"])-_epoch(right["to"]))))
        return result
    report={"semantics_version":SEMANTICS_VERSION,"classification":"A",
        "visual":{"legacy_count":len(intervals(legacy,"visual_intervals")),
                  "canonical_count":len(intervals(canonical,"visual_intervals"))},
        "diagnostic":{"legacy_count":len(intervals(legacy,"diagnostic_intervals")),
                      "canonical_count":len(intervals(canonical,"diagnostic_intervals"))},
        "relative_endpoints":{"legacy_count":len(intervals(legacy,"relative_endpoints")),
                              "canonical_count":len(intervals(canonical,"relative_endpoints"))}}
    values=shifts(intervals(legacy,"visual_intervals"),
                  intervals(canonical,"visual_intervals"))
    report["boundary_shift_seconds"]={"maximum":max(values,default=0),
        "median":sorted(values)[len(values)//2] if values else 0}
    report["visual_phase_type_changes"]=sum(
        left.get("phase")!=right.get("phase") for left,right in zip(
            intervals(legacy,"visual_intervals"),intervals(canonical,"visual_intervals")))
    report["diagnostic_phase_type_changes"]=sum(
        left.get("phase")!=right.get("phase") for left,right in zip(
            intervals(legacy,"diagnostic_intervals"),intervals(canonical,"diagnostic_intervals")))
    return report
