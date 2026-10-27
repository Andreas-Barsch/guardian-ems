"""Deterministic history ordering; keep conflicts and provenance explicit."""
from datetime import datetime
import json
import math
from research_budget import check_budget


def normalize_series(series, start, end):
    lower, upper = datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp()
    valid, rejected, seen = [], [], set()
    interrupted = set()
    for original in series.get("points", ()):
        check_budget()
        point = dict(original)
        identity = (point.get("physical_serial", point.get("module_serial")),
                    point.get("identity_epoch_id", point.get("position_history_id")), point.get("source", series.get("source")),
                    point.get("module_number", series.get("module_number")), point.get("cell_number"))
        try:
            epoch = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00")).timestamp()
            if not math.isfinite(epoch) or not math.isfinite(float(point["value"])):
                raise ValueError("non-finite observation")
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected.append(point)
            interrupted.add(identity)
            continue
        if not lower <= epoch <= upper:
            continue
        if identity in interrupted:
            point["_segment_start"] = True
            interrupted.remove(identity)
        exact = json.dumps(point, sort_keys=True, separators=(",", ":"))
        if exact in seen:
            continue
        seen.add(exact)
        identity = (point.get("physical_serial", point.get("module_serial")),
                    point.get("identity_epoch_id", point.get("position_history_id")), point.get("source", series.get("source")),
                    point.get("module_number", series.get("module_number")), point.get("cell_number"))
        valid.append((epoch, exact, identity, point))
    valid.sort(key=lambda row: (row[0], row[3].get("module_number", 0), row[3].get("cell_number", 0), row[1]))
    groups = {}
    for epoch, exact, identity, point in valid:
        groups.setdefault((identity, epoch), []).append(point)
    for points in groups.values():
        if len({json.dumps(p["value"]) for p in points}) > 1:
            for point in points:
                point["conflict"] = True
    gaps = series.get("coverage", {}).get("missing_intervals", ())
    previous = {}
    previous_point = {}
    for epoch, exact, identity, point in valid:
        prior = previous.get(identity)
        before=previous_point.get(identity)
        if before and (before.get("_coverage_to") or point.get("_coverage_from")):
            left=datetime.fromisoformat(before.get("_coverage_to",before["timestamp"])).timestamp()
            right=datetime.fromisoformat(point.get("_coverage_from",point["timestamp"])).timestamp()
            cadences=[v for v in (before.get("_expected_cadence"),point.get("_expected_cadence")) if isinstance(v,(int,float)) and v>0]
            if right>left and (not cadences or right-left>3*max(cadences)):
                point["_segment_start"]=True
        previous_point[identity]=point
        if prior is not None:
            for gap in gaps:
                left = datetime.fromisoformat(gap["from"]).timestamp()
                right = datetime.fromisoformat(gap["to"]).timestamp()
                if prior <= left < right <= epoch:
                    point["_segment_start"] = True
                    break
        previous[identity] = epoch
    result = {**series, "points": list({json.dumps(row[3],sort_keys=True):row[3] for row in valid}.values())}
    if rejected:
        result["invalid_points"] = rejected
    result["series_contract"] = "ordered_identity_coverage_v1"
    return result
