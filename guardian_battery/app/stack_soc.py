"""Identity-aware stack SOC peer comparison and cycle endpoint observations."""

from __future__ import annotations

import statistics
import math
from bisect import bisect_right
from datetime import datetime, timezone


STACK_SOC_METRICS = frozenset({"stack_soc_median", "soc_deviation"})


def current_stack_soc(modules) -> dict:
    """Return a peer median and signed percentage-point deviations."""

    valid = []
    for module in modules:
        try:
            value = float(module.soc_percent)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            valid.append((module, value))
    values = [value for _module, value in valid]
    median = statistics.median(values) if values else None
    return {
        "median": median,
        "deviations": ({int(module.module): value - median
                        for module, value in valid} if median is not None else {}),
        "module_count": len(values),
    }


def _identity_index(snapshots):
    ordered = sorted(snapshots, key=lambda item: (
        item.effective_at, item.created_at, item.position_history_id))
    return [item.effective_at for item in ordered], ordered


def _identity_at(record, index):
    explicit = record.get("module_serial")
    effective, snapshots = index
    timestamp = datetime.fromtimestamp(float(record["timestamp"]), timezone.utc).isoformat()
    selected = bisect_right(effective, timestamp) - 1
    if selected < 0:
        return None
    documented = snapshots[selected].positions.get(str(int(record["module"])))
    if not documented or (explicit and str(explicit).strip() != documented):
        return None
    return documented


def project_stack_soc(records, snapshots, *, acquisition_window_seconds=30.0) -> list[dict]:
    """Project historical peers without applying today's topology backwards."""

    index = _identity_index(snapshots)
    valid = []
    for record in records:
        try:
            identity = _identity_at(record, index)
            soc = float(record["soc_percent"])
            timestamp = float(record["timestamp"])
            module = int(record["module"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if identity and math.isfinite(soc) and math.isfinite(timestamp):
            valid.append((timestamp, module, identity, soc))
    valid.sort()

    groups, current = [], []
    for item in valid:
        duplicate_position = any(existing[1] == item[1] for existing in current)
        outside_window = current and item[0] - current[0][0] > acquisition_window_seconds
        if current and (duplicate_position or outside_window):
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)

    result = []
    for group in groups:
        median = statistics.median(item[3] for item in group)
        for timestamp, module, serial, soc in group:
            result.append({
                "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "_epoch": timestamp,
                "module": module,
                "module_serial": serial,
                "soc_percent": soc,
                "stack_soc_median": median,
                "soc_deviation_pp": soc - median,
                "active_module_count": len(group),
            })
    return result


def relative_cycle_endpoints(samples, options, *, minimum_samples=3,
                             maximum_gap_seconds=180.0) -> list[dict]:
    """Describe observed charge/discharge endings without claiming their cause."""

    def epoch(value):
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()

    ordered = sorted(samples, key=lambda item: epoch(item["timestamp"]))
    segments, current, axis = [], [], None
    option_for = options if callable(options) else lambda _timestamp: options
    for sample in ordered:
        sample_options = option_for(sample["timestamp"])
        if not sample_options:
            continue
        charge = float(sample_options["cell_diag_charge_current_a"])
        discharge = float(sample_options["cell_diag_discharge_current_a"])
        value = float(sample["current_a"])
        sample_axis = "charge" if value >= charge else "discharge" if value <= -discharge else None
        gap = epoch(sample["timestamp"]) - epoch(current[-1]["timestamp"]) if current else 0
        if sample_axis == axis and sample_axis and gap <= maximum_gap_seconds:
            current.append(sample)
            continue
        if current and len(current) >= minimum_samples:
            segments.append((axis, current))
        current = [sample] if sample_axis else []
        axis = sample_axis
    result = []
    for segment_axis, segment in segments:
        endpoint = segment[-1]
        endpoint_options = option_for(endpoint["timestamp"])
        soc = float(endpoint["soc_percent"])
        mean_mv = statistics.fmean(float(value) for value in endpoint["voltages_mv"])
        absolute = (soc <= float(endpoint_options["cell_diag_low_soc_percent"])
                    if segment_axis == "discharge" else
                    soc >= float(endpoint_options["cell_diag_high_soc_percent"]))
        result.append({
            "timestamp": endpoint["timestamp"],
            "kind": "relative_low_point" if segment_axis == "discharge" else "relative_high_point",
            "axis": segment_axis,
            "soc_percent": soc,
            "mean_cell_voltage_mv": round(mean_mv, 2),
            "sample_count": len(segment),
            "absolute_region_reached": absolute,
            "evidence_level": "observation",
            "causality": "not_determined",
            "bms_limit_confirmed": False,
        })
    return result
