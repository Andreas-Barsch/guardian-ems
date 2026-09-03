"""Deterministic, read-only Guardian Cell Risk Ranking V2 analytics."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo


CELL_RISK_ALGORITHM_VERSION = "guardian_cell_risk_v2_1"
FORMULA_VERSION = "2.0.0"
CLASSIFICATION_VERSION = "1.0.0"
LOOKBACK_DAYS = 14


def cell_group(cell_number: int) -> str:
    if not 1 <= cell_number <= 15:
        raise ValueError("cell_number must be between 1 and 15")
    return f"G{(cell_number - 1) // 5 + 1}"


def percentile(values: Sequence[float], probability: float) -> float:
    """Linear percentile, matching the reference calculation."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires values")
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def ols_slope(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("OLS slope requires two values")
    centre = (len(values) - 1) / 2
    mean = statistics.fmean(values)
    denominator = sum((x - centre) ** 2 for x in range(len(values)))
    return sum((x - centre) * (value - mean)
               for x, value in enumerate(values)) / denominator


def risk_class(score: float) -> str:
    if not 0 <= score <= 100:
        raise ValueError("score must be between 0 and 100")
    if score <= 15:
        return "UNAUFFÄLLIG"
    if score <= 30:
        return "HINWEIS"
    if score <= 50:
        return "BEOBACHTEN"
    if score <= 75:
        return "DEUTLICH_AUFFÄLLIG"
    return "HOHES_RISIKO"


def score_components(median_delta_module: float, median_delta_group: float,
                     lowest_share: float, load_sensitivity: float,
                     trend_slope: float) -> dict[str, float]:
    weakness_raw = (.7 * max(0.0, -median_delta_module)
                    + .3 * max(0.0, -median_delta_group))
    weakness = min(100.0, weakness_raw / 50.0 * 100.0)
    qualified_lowest = lowest_share * 100.0 * min(1.0, weakness_raw / 15.0)
    load = min(100.0, load_sensitivity / 60.0 * 100.0)
    trend = min(100.0, max(0.0, -trend_slope) / 3.0 * 100.0)
    path_a = .60 * weakness + .40 * qualified_lowest
    path_b = .65 * weakness + .35 * load
    score = min(100.0, .75 * max(path_a, path_b)
                + .15 * min(path_a, path_b) + .10 * trend)
    return {"weakness_raw_mv": weakness_raw, "weakness_component": weakness,
            "qualified_lowest_component": qualified_lowest,
            "load_component": load, "trend_component": trend,
            "path_a": path_a, "path_b": path_b, "risk_score_v2": score}


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.timestamp()


def _maintenance_status(serial: str, cell_number: int,
                        records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    relevant = [record for record in records
                if record.get("module_serial") == serial
                and record.get("cell_number") in (None, cell_number)
                and ("balanc" in str(record.get("category", "")).lower()
                     or "balanc" in str(record.get("title", "")).lower()
                     or "balanc" in str(record.get("action_taken", "")).lower())]
    if not relevant:
        return {"status": "NO_DOCUMENTED_BALANCING", "event_count": 0,
                "evaluation": "not_applicable"}
    return {"status": "BALANCING_PERFORMED_EVALUATION_PENDING",
            "event_count": len(relevant), "evaluation": "insufficient_comparable_evidence",
            "latest_event_at": max(str(item.get("occurred_at", "")) for item in relevant)}


def analyze_cell_risk(records: Sequence[Mapping[str, Any]], *, diagnostic_date: str,
                      maintenance_records: Sequence[Mapping[str, Any]] = (),
                      position_resolver: Callable[[str, str], Any] | None = None,
                      timezone_name: str = "Europe/Berlin"
                      ) -> dict[str, Any]:
    """Return one compact deterministic record per physical cell."""
    observations: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    invalid = excluded = 0
    for record in records:
        try:
            serial = record.get("module_serial")
            values = record.get("voltages_mv")
            current = float(record["current_a"])
            timestamp = _timestamp(record["timestamp"])
            if not serial or not isinstance(values, list) or len(values) != 15 or current >= -.8:
                excluded += 1
                continue
            cells = [float(value) for value in values]
            module_median = statistics.median(cells)
            lowest_index = cells.index(min(cells))
            day = datetime.fromtimestamp(timestamp, ZoneInfo(timezone_name)).date().isoformat()
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        for index, value in enumerate(cells):
            number = index + 1
            start = (index // 5) * 5
            group_median = statistics.median(cells[start:start + 5])
            observations[(str(serial), number)].append({
                "timestamp": timestamp, "timestamp_iso": datetime.fromtimestamp(
                    timestamp, timezone.utc).isoformat(), "day": day,
                "delta_module": value - module_median,
                "delta_group": value - group_median,
                "lowest": index == lowest_index, "current": current,
            })

    rows = []
    for (serial, number), samples in sorted(observations.items()):
        samples.sort(key=lambda sample: sample["timestamp"])
        module_deltas = [sample["delta_module"] for sample in samples]
        group_deltas = [sample["delta_group"] for sample in samples]
        low = [sample["delta_module"] for sample in samples
               if .8 <= abs(sample["current"]) < 4]
        high = [sample["delta_module"] for sample in samples if abs(sample["current"]) >= 8]
        load_sensitivity = (max(0.0, statistics.median(low) - statistics.median(high))
                            if len(low) >= 30 and len(high) >= 30 else 0.0)
        by_day: dict[str, list[float]] = defaultdict(list)
        for sample in samples:
            by_day[sample["day"]].append(sample["delta_module"])
        daily = [(day, statistics.median(values)) for day, values in sorted(by_day.items())][-7:]
        trend_slope = ols_slope([value for _day, value in daily]) if len(daily) >= 3 else 0.0
        median_module = statistics.median(module_deltas)
        median_group = statistics.median(group_deltas)
        lowest_share = sum(sample["lowest"] for sample in samples) / len(samples)
        components = score_components(median_module, median_group, lowest_share,
                                      load_sensitivity, trend_slope)
        sample_quality = "SUFFICIENT" if len(samples) >= 300 else "INSUFFICIENT"
        load_quality = "SUFFICIENT" if len(low) >= 30 and len(high) >= 30 else "INSUFFICIENT"
        trend_quality = ("STRONG" if len(daily) >= 7 else
                         "SUFFICIENT" if len(daily) >= 3 else "INSUFFICIENT")
        confidence = ("HIGH" if sample_quality == load_quality == "SUFFICIENT"
                      and trend_quality == "STRONG" else
                      "MEDIUM" if sample_quality == "SUFFICIENT"
                      and (load_quality == "SUFFICIENT" or trend_quality != "INSUFFICIENT")
                      else "LOW")
        position = history_id = None
        if position_resolver:
            resolved = position_resolver(serial, samples[-1]["timestamp_iso"])
            position, history_id = resolved if isinstance(resolved, tuple) else (resolved, None)
        score = components["risk_score_v2"]
        rows.append({
            "schema_version": 1, "diagnostic_date": diagnostic_date,
            "cell_risk_algorithm_version": CELL_RISK_ALGORITHM_VERSION,
            "formula_version": FORMULA_VERSION,
            "classification_version": CLASSIFICATION_VERSION,
            "physical_serial": serial, "current_position": position,
            "position_history_id": history_id, "cell_number": number,
            "cell_group": cell_group(number), "sample_count": len(samples),
            "low_load_sample_count": len(low), "high_load_sample_count": len(high),
            "daily_median_count": len(daily), "daily_medians": [
                {"date": day, "median_delta_module_mv": value} for day, value in daily],
            "median_delta_module_mv": median_module,
            "p10_delta_module_mv": percentile(module_deltas, .10),
            "median_delta_group_mv": median_group, "lowest_share": lowest_share,
            "effective_lowest_persistence": components["qualified_lowest_component"] / 100,
            "low_load_median_delta_mv": statistics.median(low) if len(low) >= 30 else None,
            "high_load_median_delta_mv": statistics.median(high) if len(high) >= 30 else None,
            "load_sensitivity_mv": load_sensitivity,
            "trend7_slope_mv_per_day": trend_slope if len(daily) >= 3 else None,
            **components, "risk_class": risk_class(score),
            "sample_quality": sample_quality, "load_quality": load_quality,
            "trend_quality": trend_quality, "overall_confidence": confidence,
            "balancing": _maintenance_status(serial, number, maintenance_records),
            "causality": "not_determined", "score_semantics": "engineering_priority_not_soh",
        })
    rows.sort(key=lambda row: (-row["risk_score_v2"], row["physical_serial"],
                               row["cell_number"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return {"schema_version": 1, "diagnostic_date": diagnostic_date,
            "cell_risk_algorithm_version": CELL_RISK_ALGORITHM_VERSION,
            "formula_version": FORMULA_VERSION, "classification_version": CLASSIFICATION_VERSION,
            "cells": rows, "top10": rows[:10], "valid_cell_observations": sum(map(len, observations.values())),
            "valid_discharge_module_samples": sum(map(len, observations.values())) // 15,
            "invalid_records": invalid, "excluded_records": excluded}
