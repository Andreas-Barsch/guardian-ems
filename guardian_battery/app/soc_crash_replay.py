"""Explicit offline replay of saved PWR contexts; never called by the collector."""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from zoneinfo import ZoneInfo

from research_identity import ResearchIdentityResolver
from soc_crash_detector import SocCrashDetector, SocObservation
from soc_crash_events import DETECTOR_VERSION, SocCrashEventStore

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_INPUT_RECORDS = 250_000


class ReplayInputError(ValueError):
    """Input cannot establish usable common PWR observations."""


def _expected_positions(expected_modules):
    if expected_modules is None:
        return None
    expected = set(expected_modules)
    if len(expected) < 2 or any(type(n) is not int or not 1 <= n <= 6 for n in expected):
        raise ReplayInputError("optional explicit selection requires at least two positions in 1..6")
    return expected


def _usable_group(frame, expected):
    return len(frame) >= 2 and (expected is None or set(frame) == expected)


def _observation_summary(frames, accepted, events):
    """Describe observed membership; never infer physical removal or completeness."""
    changes = []
    prior = None
    for at, frame in sorted(frames.items()):
        members = sorted(frame)
        if prior is not None and members != prior:
            changes.append({"pwr_sample_at": at, "previous_modules": prior,
                            "observed_modules": members})
        prior = members
    daily = {}
    for name in ("UTC", "Europe/Berlin"):
        zone = ZoneInfo(name)
        rows = {}
        for at in sorted(frames):
            day = datetime.fromtimestamp(at, zone).date().isoformat()
            row = rows.setdefault(day, {"observed_groups": 0, "replayed_groups": 0,
                "skipped_groups": 0, "group_sizes": {}, "crashes": 0})
            row["observed_groups"] += 1
            row["replayed_groups" if at in accepted else "skipped_groups"] += 1
            size = str(len(frames[at]))
            row["group_sizes"][size] = row["group_sizes"].get(size, 0) + 1
        for event in events:
            day = datetime.fromisoformat(event["confirmed_at"]).astimezone(zone).date().isoformat()
            rows[day]["crashes"] += 1
        daily[name] = rows
    return {"historical_completeness": "unverified", "provisional": True,
        "group_compositions": dict(Counter(",".join(map(str, sorted(f))) for f in frames.values())),
        "membership_changes": changes, "daily": daily,
        "membership_semantics": "Observed groups only, no removal inference. The shared gap detector handles role changes; fewer than two modules suspend comparison without extending the original deadline."}


def replay_saved_pwr(paths, *, start, end, expected_modules=None, x_pp=5.0, y_seconds=300,
                     event_output=None, position_history=None):
    """Replay one explicitly bounded saved window; output is optional and local.

    Cell rows are grouped ONLY by their documented pwr_sample_at. Missing or
    conflicting PWR contexts block this bounded run. Fewer than two values
    suspend comparison, not the deadline. Optional positions only filter comparison. No interpolation,
    live sampling, identity filter or new crash condition is introduced.
    A restart repeats this same source window; the existing event store dedups.
    """
    started = time.perf_counter()
    first, last = datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00"))
    if first.tzinfo is None or last.tzinfo is None or not 0 < (last-first).total_seconds() <= 86400:
        raise ReplayInputError("explicit timezone-aware range must be positive and at most 24 hours")
    expected = _expected_positions(expected_modules)
    paths = [Path(p).resolve() for p in paths]
    if not 1 <= len(paths) <= 2 or len(set(paths)) != len(paths):
        raise ReplayInputError("select one or two distinct saved daily files")
    if sum(p.stat().st_size for p in paths) > MAX_INPUT_BYTES:
        raise ReplayInputError("input exceeds 64 MiB; narrow the saved input selection")
    if event_output is not None:
        output = Path(event_output).resolve()
        if (output.name != "soc_crash_events.jsonl" or output in paths or
                any(output.is_relative_to(p.parent) for p in paths) or
                any(output == Path(p) or output.is_relative_to(p)
                    for p in ("/share", "/data", "/config", "/backup", "/Volumes"))):
            raise ReplayInputError("write output must be a separate local soc_crash_events.jsonl test store")
    detector = SocCrashDetector(x_pp, y_seconds)
    frames, sources = {}, []
    resolver = None
    identity_source = None
    if position_history is not None:
        identity_path = Path(position_history).resolve()
        if identity_path.stat().st_size > 2 * 1024 * 1024:
            raise ReplayInputError("position history exceeds bounded metadata limit")
        raw = identity_path.read_bytes()
        resolver = ResearchIdentityResolver.from_path(identity_path)
        if identity_path.read_bytes() != raw:
            raise ReplayInputError("position history changed during read")
        identity_source = {"path": str(identity_path), "sha256": hashlib.sha256(raw).hexdigest()}
    identity_conflicts = 0
    rows = 0; duplicates = 0
    lower, upper = first.timestamp(), last.timestamp()
    for path in paths:
        before = path.stat(); digest = hashlib.sha256()
        with path.open("rb") as handle:
            for line in handle:
                rows += 1; digest.update(line)
                if rows > MAX_INPUT_RECORDS: raise ReplayInputError("input record limit exceeded")
                try:
                    record = json.loads(line)
                    cell_time = float(record["timestamp"])
                    if not math.isfinite(cell_time): raise ValueError("non-finite cell time")
                    pwr_time = record.get("pwr_sample_at")
                    if pwr_time is None:
                        if lower <= cell_time <= upper:
                            raise ReplayInputError("selected row has no original PWR timestamp")
                        continue
                    if isinstance(pwr_time, bool): raise ValueError("boolean PWR time")
                    pwr_time = float(pwr_time)
                    if not math.isfinite(pwr_time): raise ValueError("non-finite PWR time")
                    if not lower <= pwr_time <= upper: continue
                    if cell_time < pwr_time: raise ValueError("PWR context is from the future")
                    serial = record.get("module_serial")
                    if resolver is not None:
                        resolved = resolver.serial_at(record["module"], datetime.fromtimestamp(
                            pwr_time, timezone.utc).isoformat()).get("physical_serial")
                        if serial is None: serial = resolved
                        elif resolved is not None and serial != resolved: identity_conflicts += 1
                    sample = SocObservation(record["module"], record["soc_percent"], pwr_time, serial)
                    frame = frames.setdefault(pwr_time, {})
                    prior = frame.get(sample.module)
                    if prior is not None:
                        if prior != sample: raise ReplayInputError("conflicting saved PWR observations")
                        duplicates += 1
                    frame[sample.module] = sample
                except ReplayInputError:
                    raise
                except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                    raise ReplayInputError("invalid source record; cannot safely replay this file") from exc
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ReplayInputError("source changed during read")
        sources.append({"path": str(path), "bytes": before.st_size, "sha256": digest.hexdigest()})
    incomplete = [at for at, frame in frames.items()
                  if len(frame) >= 2 and expected is not None and set(frame) != expected]
    if incomplete:
        raise ReplayInputError(f"{len(incomplete)} incomplete/unexpected frames; no replay or writes performed; "
                               f"first PWR timestamp {min(incomplete)}")
    events = []; candidates = 0
    times = sorted(frames)
    for timestamp in times:
        before = detector.candidates_started
        events.extend(detector.observe(list(frames[timestamp].values())))
        candidates += detector.candidates_started - before
    stored = 0
    if event_output is not None:
        store = SocCrashEventStore(event_output)
        # Preflight immutable conflicts before the first append.
        for event in events:
            existing = store.get(event.event_id)
            if existing is not None and existing != event:
                raise ReplayInputError("existing event ID has different immutable payload")
        stored = sum(store.append(event) for event in events)
    gaps = [b-a for a,b in zip(times, times[1:])]
    return {"mode": "dry_run" if event_output is None else "local_test_store",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "measurement_range": {"from": first.isoformat(), "to": last.isoformat()},
        "detector_version": DETECTOR_VERSION, "x_pp": x_pp, "y_seconds": y_seconds,
        "source_kind": "saved_cell_history_pwr_contexts", "sources": sources,
        "expected_modules": sorted(expected) if expected is not None else None,
        "selection_policy": "explicit_positions" if expected is not None else "observed_min_two",
        **_observation_summary(frames, {t for t in times if len(frames[t]) >= 2}, [e.to_dict() for e in events]),
        "suspended_groups": sum(len(f) < 2 for f in frames.values()), "input_records": rows,
        "position_history_source": identity_source, "identity_conflicts": identity_conflicts,
        "frames": len(times), "duplicate_observations": duplicates,
        "first_frame": times[0] if times else None, "last_frame": times[-1] if times else None,
        "gap_seconds": {"min": min(gaps) if gaps else None, "max": max(gaps) if gaps else None},
        "candidates_started": candidates, "crashes": len(events), "appended": stored,
        "unresolved_serial_observations": sum(s.physical_serial is None for f in frames.values() for s in f.values()),
        "events": [event.to_dict() for event in events],
        "limitations": ["Saved PWR contexts are a sampled subset, not the complete live PWR sequence.",
            "Initial detector state begins at the first saved frame; earlier candidates are unknown.",
            "No interpolation or claim of absence of live crashes between frames.",
            "Repeated runs/restarts must use the same complete source window and parameters for deduplication.",
            "Historical replay provenance is this report; event timestamps remain measurement times."],
        "runtime_seconds": time.perf_counter()-started}


def audit_saved_history(directory, *, expected_modules=None, position_history=None, x_pp=5.0, y_seconds=300):
    """Replay common observed groups with at least two modules, without writes.

    Physical completeness remains unknown, not an extra detection condition.
    Membership changes use the shared gap detector. Fewer than two observations
    suspend comparison; conflicting/no-PWR data remain source boundaries.
    Explicit positions are optional for historical comparison runs only.
    """
    started = time.perf_counter()
    paths = sorted(Path(directory).glob("????-??-??.jsonl"))
    if not paths or len(paths) > 62 or sum(p.stat().st_size for p in paths) > 256*1024*1024:
        raise ReplayInputError("saved inventory requires 1..62 daily files, at most 256 MiB")
    expected = _expected_positions(expected_modules)
    SocCrashDetector(x_pp,y_seconds)  # validate parameters even with no usable frames
    resolver=None; identity_source=None
    if position_history:
        identity_path=Path(position_history).resolve()
        if identity_path.stat().st_size>2*1024*1024:raise ReplayInputError("position history exceeds bounded metadata limit")
        identity_bytes=identity_path.read_bytes()
        resolver=ResearchIdentityResolver.from_path(identity_path)
        if identity_path.read_bytes()!=identity_bytes:raise ReplayInputError("position history changed during read")
        identity_source={"path":str(identity_path),"sha256":hashlib.sha256(identity_bytes).hexdigest()}
    frames={}; conflicts=set(); sources=[]; missing=[]; rows=0; identity_conflicts=0
    for path in paths:
        before=path.stat(); digest=hashlib.sha256(); count=0
        with path.open("rb") as handle:
            for line in handle:
                rows+=1; count+=1; digest.update(line)
                if rows>500_000: raise ReplayInputError("saved inventory exceeds 500000 records")
                try:
                    record=json.loads(line); cell_time=float(record["timestamp"])
                    if not math.isfinite(cell_time): raise ValueError("non-finite cell time")
                    at=record.get("pwr_sample_at")
                    if at is None:
                        missing.append(cell_time); continue
                    if isinstance(at,bool): raise ValueError("boolean PWR time")
                    at=float(at)
                    if not math.isfinite(at) or cell_time<at: raise ValueError("invalid PWR time")
                    serial=record.get("module_serial")
                    if resolver:
                        known=resolver.serial_at(record["module"],datetime.fromtimestamp(at,timezone.utc).isoformat()).get("physical_serial")
                        if serial is None:serial=known
                        elif known is not None and known!=serial:identity_conflicts+=1
                    sample=SocObservation(record["module"],record["soc_percent"],at,serial)
                    frame=frames.setdefault(at,{})
                    if sample.module in frame and frame[sample.module]!=sample:conflicts.add(at)
                    frame[sample.module]=sample
                except (ValueError,TypeError,KeyError,UnicodeError) as exc:
                    raise ReplayInputError("invalid saved record; inventory stopped without event writes") from exc
        after=path.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
            raise ReplayInputError("saved source changed during inventory")
        sources.append({"path":str(path.resolve()),"rows":count,"bytes":before.st_size,"sha256":digest.hexdigest()})
    # Missing original PWR time cannot be placed precisely in PWR chronology.
    # Only the suffix after its latest cell time is reconstructable in this run.
    unavailable_until=max(missing) if missing else float('-inf')
    detector=None; events=[]; sections=[]; current=None; candidates=0; partial=0; unresolved=0; suspended=0
    ordered=sorted(frames)
    accepted=set(); event_context=[]
    for at in ordered:
        frame=frames[at]
        if at<=unavailable_until or at in conflicts or (len(frame) >= 2 and expected is not None and set(frame) != expected):
            partial+=1;detector=None;current=None;continue
        if len(frame) < 2:
            partial+=1;suspended+=1
            if detector is not None:
                detector.observe(list(frame.values()))  # suspend comparison; original clock continues
            continue
        if detector is None:
            detector=SocCrashDetector(x_pp,y_seconds)
            current={"first_frame":at,"last_frame":at,"frames":0,"candidates":0,"crashes":0,"largest_gap_seconds":None}
            sections.append(current)
        if current["frames"]:
            gap=at-current["last_frame"]
            current["largest_gap_seconds"]=max(current["largest_gap_seconds"] or 0,gap)
        accepted.add(at)
        before=detector.candidates_started
        detected=detector.observe(list(frame.values()))
        started_now=detector.candidates_started-before
        candidates+=started_now;current["candidates"]+=started_now
        unresolved+=sum(s.physical_serial is None for s in frame.values())
        events.extend(e.to_dict() for e in detected)
        event_context.extend({"event_id": e.event_id, "confirmation_modules": sorted(frame)} for e in detected)
        current["frames"]+=1;current["last_frame"]=at;current["crashes"]+=len(detected)
    gaps=[b-a for a,b in zip(ordered,ordered[1:])]
    def stamp(at):return datetime.fromtimestamp(at,timezone.utc).isoformat() if at is not None else None
    return {"mode":"inventory_dry_run","executed_at":datetime.now(timezone.utc).isoformat(),
        "detector_version":DETECTOR_VERSION,"x_pp":x_pp,"y_seconds":y_seconds,"expected_modules":sorted(expected) if expected is not None else None,
        "selection_policy":"explicit_positions" if expected is not None else "observed_min_two",
        **_observation_summary(frames, accepted, events), "event_observation_context":event_context,
        "sources":sources,"position_history_source":identity_source,"input_records":rows,"input_bytes":sum(s["bytes"] for s in sources),
        "missing_pwr_rows":len(missing),"missing_pwr_range":{"from":stamp(min(missing)) if missing else None,"to":stamp(max(missing)) if missing else None},
        "pwr_range":{"from":stamp(ordered[0]) if ordered else None,"to":stamp(ordered[-1]) if ordered else None},
        "pwr_groups":len(frames),"group_sizes":dict(Counter(map(len,frames.values()))),
        "conflicting_groups":len(conflicts),"incomplete_or_unusable_groups":partial,"suspended_groups":suspended,
        "frames_replayed":len(accepted),
        "complete_frames_replayed":len(accepted) if expected is not None else None,"sections":sections,
        "largest_pwr_gap_seconds":max(gaps) if gaps else None,
        "gaps_over_y":sum(g>y_seconds for g in gaps),"candidates_started":candidates,
        "crashes":len(events),"events":events,"appended":0,
        "identity_conflicts":identity_conflicts,"unresolved_serial_observations":unresolved,
        "runtime_seconds":time.perf_counter()-started,
        "limitations":["Only common observed contexts with at least two modules are replayed, never the full live PWR sequence.",
            "Missing module observations are not inferred to be physical removal; all results remain provisional.",
            "Membership changes use the shared gap detector. Fewer than two values suspend comparison, never extend Y; contradictory or untimed input bounds sections.",
            "No interpolation; no absence-of-live-crashes claim; no event persistence in inventory mode."]}
