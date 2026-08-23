"""Deterministic, quality-gated evidence methods for Guardian cell diagnostics.

The module deliberately produces relative evidence.  It does not estimate cell
SOH, remaining life, failure probability, absolute cell capacity or absolute
resistance.  Every method either documents its data basis or fails closed with
``NICHT BEWERTBAR`` and a reason.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from config_history import config_id, diagnostic_parameters
from version import DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION


PHASES = ("discharge", "low", "charge", "high")
TREND_LABELS = {"stable": "stabil", "improving": "verbessernd",
                "worsening": "verschlechternd", "unclear": "unklar"}
QUALITY_DEFAULTS = {
    "cell_diag_trend_min_days": 3,
    "cell_diag_trend_min_rank_change": 0.5,
    "cell_diag_trend_min_deviation_change_mv": 2,
    "cell_diag_resistance_min_delta_current_a": 5,
    "cell_diag_resistance_max_step_seconds": 90,
    "cell_diag_resistance_min_events": 3,
    "cell_diag_resistance_window_samples": 2,
    "cell_diag_resistance_max_current_span_a": 0.5,
    "cell_diag_resistance_max_relative_mad": 0.25,
    "cell_diag_quality_max_temperature_change_c": 2,
    "cell_diag_sequence_max_gap_seconds": 120,
    "cell_diag_sequence_min_samples": 10,
    "cell_diag_sequence_min_duration_seconds": 600,
    "cell_diag_sequence_min_charge_ah": 0.2,
    "cell_diag_sequence_min_segments": 2,
    "cell_diag_rest_max_current_a": 0.3,
    "cell_diag_rest_min_duration_seconds": 900,
    "cell_diag_balancing_min_active_samples": 3,
    "cell_diag_ica_min_samples": 60,
    "cell_diag_ica_max_current_cv": 0.1,
    "cell_diag_ica_min_voltage_steps": 20,
    "cell_diag_maintenance_context_window_hours": 72,
    "cell_diag_relative_trend_change_percent": 20,
    "cell_diag_capacity_boundary_fraction": 0.9,
    "cell_diag_capacity_max_crossing_mad_fraction": 0.05,
    "cell_diag_curve_grid_points": 21,
    "cell_diag_curve_max_rms_mad_mv": 5,
}


def _period(samples):
    if not samples:
        return {"from": None, "to": None, "seconds": 0}
    start = float(samples[0]["timestamp"])
    end = float(samples[-1]["timestamp"])
    return {
        "from": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "to": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "seconds": round(max(0.0, end - start), 1),
    }


def _not_assessable(reason, samples=0, period=None):
    return {
        "status": "NICHT BEWERTBAR",
        "quality": "LOW",
        "valid_data": samples,
        "observation_period": period or {"from": None, "to": None, "seconds": 0},
        "reason": reason,
    }


def _ranks(voltages):
    ordered = sorted(set(voltages), reverse=True)
    result = {}
    position = 1
    for voltage in ordered:
        count = voltages.count(voltage)
        result[voltage] = (position + position + count - 1) / 2
        position += count
    return [result[value] for value in voltages]


def _slope(values):
    """Least-squares slope per observation, without third-party dependencies."""
    if len(values) < 2:
        return None
    mean_x = (len(values) - 1) / 2
    mean_y = statistics.fmean(values)
    denominator = sum((index - mean_x) ** 2 for index in range(len(values)))
    if not denominator:
        return 0.0
    return sum((index - mean_x) * (value - mean_y)
               for index, value in enumerate(values)) / denominator


def _relative_trend(values, minimum_percent):
    if len(values) < 2:
        return "unklar"
    middle = max(1, len(values) // 2)
    first = statistics.median(values[:middle])
    last = statistics.median(values[middle:])
    baseline = max(abs(first), 1e-9)
    change = 100 * (last - first) / baseline
    if change >= minimum_percent:
        return "verschlechternd"
    if change <= -minimum_percent:
        return "verbessernd"
    return "stabil"


class EvidenceDiagnostics:
    """One bounded in-memory pass over a module's already persisted samples."""

    def __init__(self, phase_classifier):
        self.phase_classifier = phase_classifier

    @staticmethod
    def _family(name, members):
        assessable = [(member_name, value) for member_name, value in members.items()
                      if value.get("status") not in {"NICHT BEWERTBAR", None}
                      and value.get("quality", "LOW") in {"MEDIUM", "HIGH"}]
        if not assessable:
            return {"family": name, "status": "NICHT BEWERTBAR", "quality": "LOW",
                    "direction": "unklar", "members": list(members),
                    "reason": "Keine Methode dieser Evidenzfamilie erfüllt die Quality Gates."}
        directions = [value.get("trend", "unklar") for _, value in assessable]
        direction = ("verschlechternd" if "verschlechternd" in directions else
                     "verbessernd" if "verbessernd" in directions and "stabil" not in directions else
                     "stabil" if "stabil" in directions else "unklar")
        quality = min((value.get("quality", "LOW") for _, value in assessable),
                      key=lambda value: {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[value])
        periods = [value.get("observation_period", {}) for _, value in assessable]
        starts = [item.get("from") for item in periods if item.get("from")]
        ends = [item.get("to") for item in periods if item.get("to")]
        return {"family": name, "status": "BEWERTBAR", "quality": quality,
                "direction": direction, "members": [item[0] for item in assessable],
                "data_basis": sum(int(item[1].get("valid_data", 0)) for item in assessable),
                "observation_period": {"from": min(starts) if starts else None,
                                       "to": max(ends) if ends else None,
                                       "seconds": max((float(item.get("seconds", 0)) for item in periods), default=0)}}

    @staticmethod
    def _trend_risk_confidence(families, ranking, period, options):
        qualified = [value for value in families.values() if value["status"] == "BEWERTBAR"]
        independent = len(qualified)
        high_quality = sum(value["quality"] == "HIGH" for value in qualified)
        duration = max([float(period.get("seconds", 0))]
                       + [float(value.get("observation_period", {}).get("seconds", 0))
                          for value in qualified])
        coverage = max((phase.get("coverage_percent", 0)
                        for phase in ranking.get("phases", {}).values()), default=0)
        event_units = sum(1 for value in qualified if value.get("data_basis", 0) > 0)
        minimum_duration = int(options["cell_diag_trend_min_days"]) * 86400
        if independent >= 4 and high_quality >= 2 and duration >= 2 * minimum_duration and event_units >= 4:
            level = "HIGH"
        elif independent >= 2 and duration >= minimum_duration and event_units >= 2:
            level = "MEDIUM"
        else:
            level = "LOW"
        return level, {
            "independent_families": independent, "high_quality_families": high_quality,
            "observation_seconds": round(duration, 1), "data_coverage_percent": coverage,
            "qualified_data_families": event_units,
            "rule": "HIGH: ≥4 Familien, ≥2 HIGH, ≥2× Mindestdauer; MEDIUM: ≥2 Familien und Mindestdauer; sonst LOW",
        }

    def analyse(self, samples, options, base_cells, maintenance_events=(), aggregate_records=()):
        options = {**QUALITY_DEFAULTS, **options}
        valid = sorted(
            (sample for sample in samples
             if len(sample.get("voltages_mv", ())) == 15),
            key=lambda sample: float(sample["timestamp"]),
        )
        period = _period(valid)
        ranking = self._ranking(valid, options, period, aggregate_records)
        resistance = self._resistance(valid, options, period)
        segments = self._segments(valid, options)
        capacity, curves = self._capacity_and_curves(segments, options, period)
        rest = self._rest(valid, options, period)
        balancing = self._balancing(valid, options, period)
        readiness = self._ica_readiness(segments, options, period)
        maintenance = self._maintenance(valid, maintenance_events, ranking, period, options)

        cells = []
        for index, base in enumerate(base_cells):
            evidences = {
                "ranking_drift": ranking["cells"][index],
                "dynamic_resistance": resistance["cells"][index],
                "capacity_consistency": capacity["cells"][index],
                "curve_analysis": curves["cells"][index],
                "rest_drift": rest["cells"][index],
                "balancing_context": balancing["cells"][index],
            }
            families = {
                "voltage_ranking": self._family("Spannungs-/Ranking-Konsistenz", {
                    "ranking_drift": evidences["ranking_drift"]}),
                "dynamic_resistance": self._family("Dynamische Widerstandsantwort", {
                    "dynamic_resistance": evidences["dynamic_resistance"]}),
                "capacity_curve": self._family("Kapazitäts-/Kurvenverhalten", {
                    "capacity_consistency": evidences["capacity_consistency"],
                    "curve_analysis": evidences["curve_analysis"]}),
                "rest_drift": self._family("Ruhe-/Driftverhalten", {
                    "rest_drift": evidences["rest_drift"]}),
                "balancing": self._family("Balancing-Kontext", {
                    "balancing_context": evidences["balancing_context"]}),
                "maintenance": self._family("Maintenance-/Servicekontext", {
                    "maintenance_context": maintenance[index]}),
            }
            worsening_families = [name for name, value in families.items()
                                  if value["direction"] == "verschlechternd"]
            improving_families = [name for name, value in families.items()
                                  if value["direction"] == "verbessernd"]
            assessable_families = [name for name, value in families.items()
                                   if value["status"] == "BEWERTBAR"]
            if len(worsening_families) >= 2:
                trend = "verschlechternd"
            elif improving_families and not worsening_families:
                trend = "verbessernd"
            elif assessable_families:
                trend = "stabil"
            else:
                trend = "unklar"

            condition = base["status"]
            trend_risk_confidence, confidence_basis = self._trend_risk_confidence(
                families, evidences["ranking_drift"], period, options
            )
            qualified_worsening = [name for name in worsening_families
                                   if families[name]["quality"] in {"MEDIUM", "HIGH"}]
            if condition == "KRITISCH":
                risk = "Wartung empfohlen"
                risk_reason = "Dokumentierte harte Current-Condition-Regel: Zellstatus KRITISCH."
            elif len(qualified_worsening) >= 2:
                risk = "Wartung empfohlen"
                risk_reason = "Mindestens zwei qualifizierte unabhängige Evidenzfamilien verschlechtern sich: " + ", ".join(qualified_worsening)
            elif condition == "AUFFÄLLIG" or qualified_worsening:
                risk = "beobachten"
                risk_reason = "Auffälliger aktueller Befund oder eine einzelne qualifizierte verschlechternde Evidenzfamilie."
            else:
                risk = "kein Hinweis"
                risk_reason = "Keine qualifizierte Konvergenz unabhängiger verschlechternder Evidenzfamilien."
            cells.append({
                "cell": index + 1,
                "current_condition": condition,
                "trend": trend,
                "maintenance_risk": risk,
                "maintenance_risk_reason": risk_reason,
                "confidence": base["confidence"],
                "trend_risk_confidence": trend_risk_confidence,
                "trend_risk_confidence_basis": confidence_basis,
                "method_quality": trend_risk_confidence,
                "evidence_families": families,
                "contributing_evidence": qualified_worsening,
                "missing_evidence": [name for name, value in evidences.items()
                                     if value.get("status") == "NICHT BEWERTBAR"],
                "methods": evidences,
                "maintenance_context": maintenance[index],
                "ica_dva_readiness": readiness,
            })

        worst = max(cells, key=lambda cell: (
            {"kein Hinweis": 0, "beobachten": 1, "Wartung empfohlen": 2,
             "Service dringend": 3}[cell["maintenance_risk"]],
            {"stabil": 0, "unklar": 1, "verbessernd": 1,
             "verschlechternd": 2}[cell["trend"]],
        )) if cells else None
        return {
            "schema_version": 1,
            "guardian_version": GUARDIAN_VERSION,
            "diagnostic_engine_version": DIAGNOSTIC_ENGINE_VERSION,
            "config_id": config_id(diagnostic_parameters(options)),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "observation_period": period,
            "single_pass_sample_count": len(valid),
            "cells": cells,
            "module": {
                "worst_cell": worst["cell"] if worst else None,
                "trend": worst["trend"] if worst else "unklar",
                "maintenance_risk": worst["maintenance_risk"] if worst else "kein Hinweis",
                "trend_risk_confidence": worst["trend_risk_confidence"] if worst else "LOW",
                "evidence_families": worst["evidence_families"] if worst else {},
                "contributing_evidence": worst["contributing_evidence"] if worst else [],
            },
        }

    def _ranking(self, samples, options, period, aggregate_records=()):
        daily = defaultdict(lambda: [{"rank": [], "dev": [], "low": 0,
                                      "high": 0, "n": 0} for _ in range(15)])
        phase_counts = defaultdict(int)
        current_config_id = config_id(diagnostic_parameters(options))
        persisted = [record for record in aggregate_records
                     if record.get("config_id") == current_config_id
                     and record.get("phase") in PHASES]
        source_samples = [] if persisted else samples
        for sample in source_samples:
            voltages = sample["voltages_mv"]
            median = statistics.median(voltages)
            ranks = _ranks(voltages)
            day = datetime.fromtimestamp(float(sample["timestamp"]), timezone.utc).date().isoformat()
            for phase in self.phase_classifier(sample, options):
                if phase not in PHASES:
                    continue
                phase_counts[phase] += 1
                low, high = min(voltages), max(voltages)
                for index, voltage in enumerate(voltages):
                    entry = daily[(day, phase)][index]
                    entry["rank"].append(ranks[index])
                    entry["dev"].append(voltage - median)
                    entry["low"] += voltage == low
                    entry["high"] += voltage == high
                    entry["n"] += 1
        ranking_period = period
        if persisted:
            starts = [float(record["first_timestamp"]) for record in persisted]
            ends = [float(record["last_timestamp"]) for record in persisted]
            ranking_period = {
                "from": datetime.fromtimestamp(min(starts), timezone.utc).isoformat(),
                "to": datetime.fromtimestamp(max(ends), timezone.utc).isoformat(),
                "seconds": round(max(ends) - min(starts), 1),
            }
            for record in persisted:
                phase_counts[record["phase"]] += int(record["sample_count"])
        min_days = int(options["cell_diag_trend_min_days"])
        rank_limit = float(options["cell_diag_trend_min_rank_change"])
        deviation_limit = float(options["cell_diag_trend_min_deviation_change_mv"])
        results = [{"status": "NICHT BEWERTBAR", "quality": "LOW", "phases": {}}
                   for _ in range(15)]
        for cell in range(15):
            for phase in PHASES:
                series = []
                if persisted:
                    series = [{"day": record["day"], "mean_rank": record["mean_rank"],
                               "median_deviation_mv": record["median_deviation_mv"],
                               "lowest_percent": record["lowest_percent"],
                               "highest_percent": record["highest_percent"],
                               "samples": record["sample_count"]}
                              for record in persisted
                              if int(record["cell"]) == cell + 1 and record["phase"] == phase]
                else:
                    for (day, item_phase), rows in sorted(daily.items()):
                        row = rows[cell]
                        if item_phase == phase and row["n"]:
                            series.append({"day": day,
                                           "mean_rank": statistics.fmean(row["rank"]),
                                           "median_deviation_mv": statistics.median(row["dev"]),
                                           "lowest_percent": 100 * row["low"] / row["n"],
                                           "highest_percent": 100 * row["high"] / row["n"],
                                           "samples": row["n"]})
                if len(series) < min_days:
                    value = _not_assessable(
                        f"{len(series)} Tagesaggregate; mindestens {min_days} erforderlich.",
                        sum(item["samples"] for item in series), ranking_period)
                    value["daily_aggregates"] = len(series)
                else:
                    rank_change = _slope([item["mean_rank"] for item in series])
                    deviation_change = _slope([abs(item["median_deviation_mv"]) for item in series])
                    rank_drift = abs(rank_change or 0) >= rank_limit
                    # Rank direction is phase-dependent and therefore never
                    # becomes a worsening classification on its own.
                    worsening = (deviation_change or 0) >= deviation_limit
                    improving = (deviation_change or 0) <= -deviation_limit
                    value = {
                        "status": "BEWERTBAR", "quality": "HIGH" if len(series) >= 2 * min_days else "MEDIUM",
                        "trend": "verschlechternd" if worsening else "verbessernd" if improving else "stabil",
                        "mean_rank": round(statistics.fmean(item["mean_rank"] for item in series), 2),
                        "rank_trend_per_day": round(rank_change, 3),
                        "rank_drift_detected": rank_drift,
                        "median_deviation_mv": round(statistics.median(item["median_deviation_mv"] for item in series), 2),
                        "median_deviation_trend_mv_per_day": round(deviation_change, 3),
                        "lowest_percent": round(statistics.fmean(item["lowest_percent"] for item in series), 1),
                        "highest_percent": round(statistics.fmean(item["highest_percent"] for item in series), 1),
                        "persistence_percent": round(100 * max(
                            sum(item["mean_rank"] >= 12 for item in series),
                            sum(item["mean_rank"] <= 4 for item in series)) / len(series), 1),
                        "valid_data": sum(item["samples"] for item in series),
                        "daily_aggregates": len(series), "observation_period": ranking_period,
                        "coverage_percent": round(100 * len(series) / max(
                            1, int(ranking_period["seconds"] // 86400) + 1), 1),
                        "explanation": "Robuste Tages-/Phasenaggregate; Rangdrift ist Zusatz-Evidenz und kein alleiniger Alarm.",
                    }
                results[cell]["phases"][phase] = value
            assessable = [value for value in results[cell]["phases"].values()
                          if value["status"] == "BEWERTBAR"]
            if assessable:
                results[cell].update(status="BEWERTBAR",
                                     quality="HIGH" if all(v["quality"] == "HIGH" for v in assessable) else "MEDIUM",
                                     trend="verschlechternd" if any(v.get("trend") == "verschlechternd" for v in assessable)
                                     else "verbessernd" if any(v.get("trend") == "verbessernd" for v in assessable)
                                     else "stabil", valid_data=sum(v["valid_data"] for v in assessable),
                                     observation_period=ranking_period)
            else:
                results[cell]["reason"] = "Kein Diagnosebereich erfüllt die Mindestzahl an Tagesaggregaten."
                results[cell]["observation_period"] = ranking_period
        return {"cells": results, "phase_sample_counts": dict(phase_counts)}

    def _resistance(self, samples, options, period):
        events = []
        minimum = float(options["cell_diag_resistance_min_delta_current_a"])
        max_seconds = float(options["cell_diag_resistance_max_step_seconds"])
        max_temp = float(options["cell_diag_quality_max_temperature_change_c"])
        window_size = int(options["cell_diag_resistance_window_samples"])
        max_span = float(options["cell_diag_resistance_max_current_span_a"])
        for index in range(window_size - 1, len(samples) - window_size):
            before_window = samples[index - window_size + 1:index + 1]
            after_window = samples[index + 1:index + 1 + window_size]
            before, after = before_window[-1], after_window[0]
            dt = float(after["timestamp"]) - float(before["timestamp"])
            before_current = [float(item["current_a"]) for item in before_window]
            after_current = [float(item["current_a"]) for item in after_window]
            delta_i = statistics.fmean(after_current) - statistics.fmean(before_current)
            temperatures = [value for item in before_window + after_window
                            for value in item.get("temperatures_c", ())]
            if not 0 < dt <= max_seconds or abs(delta_i) < minimum or len(temperatures) != 30 * window_size:
                continue
            if max(before_current) - min(before_current) > max_span or max(after_current) - min(after_current) > max_span:
                continue
            if max(temperatures) - min(temperatures) > max_temp:
                continue
            before_voltage = [statistics.median(item["voltages_mv"][cell] for item in before_window)
                              for cell in range(15)]
            after_voltage = [statistics.median(item["voltages_mv"][cell] for item in after_window)
                             for cell in range(15)]
            raw = [abs(a - b) / abs(delta_i) for a, b in zip(after_voltage, before_voltage)]
            median = statistics.median(raw)
            if median <= 0:
                continue
            events.append([value / median for value in raw])
        required = int(options["cell_diag_resistance_min_events"])
        cells = []
        for index in range(15):
            values = [event[index] for event in events]
            if len(values) < required:
                cells.append(_not_assessable(
                    f"{len(values)} geeignete natürliche Stromsprünge; mindestens {required} erforderlich.",
                    len(values), period))
                continue
            median = statistics.median(values)
            mad = statistics.median(abs(value - median) for value in values)
            reproducible = mad <= float(options["cell_diag_resistance_max_relative_mad"])
            trend = _relative_trend(values, float(options["cell_diag_relative_trend_change_percent"]))
            cells.append({
                "status": "BEWERTBAR" if reproducible else "NICHT BEWERTBAR",
                "quality": "MEDIUM" if reproducible else "LOW", "trend": trend if reproducible else "unklar",
                "relative_resistance_index": round(median, 3), "unit": "relativ zum Modulmedian",
                "events": len(values), "relative_mad": round(mad, 3), "valid_data": len(values),
                "observation_period": period,
                "reason": None if reproducible else "Relative Ereigniswerte sind nicht ausreichend reproduzierbar.",
                "explanation": "Nur relativer Index; Abtastrate und Synchronität belegen keine absolute mΩ-Angabe.",
            })
        return {"cells": cells, "accepted_events": len(events)}

    def _segments(self, samples, options):
        max_gap = float(options["cell_diag_sequence_max_gap_seconds"])
        result, current = [], []
        current_phase = None
        for sample in samples:
            axis = next((phase for phase in self.phase_classifier(sample, options)
                         if phase in {"charge", "discharge"}), None)
            gap = float(sample["timestamp"]) - float(current[-1]["timestamp"]) if current else 0
            if axis and axis == current_phase and gap <= max_gap:
                current.append(sample)
            else:
                if current:
                    result.append((current_phase, current))
                current, current_phase = ([sample], axis) if axis else ([], None)
        if current:
            result.append((current_phase, current))
        return result

    def _capacity_and_curves(self, segments, options, period):
        minimum_samples = int(options["cell_diag_sequence_min_samples"])
        minimum_seconds = float(options["cell_diag_sequence_min_duration_seconds"])
        minimum_ah = float(options["cell_diag_sequence_min_charge_ah"])
        accepted = []
        for phase, segment in segments:
            duration = float(segment[-1]["timestamp"]) - float(segment[0]["timestamp"])
            charge = sum((abs(float(a["current_a"])) + abs(float(b["current_a"]))) / 2
                         * (float(b["timestamp"]) - float(a["timestamp"])) / 3600
                         for a, b in zip(segment, segment[1:]))
            if len(segment) >= minimum_samples and duration >= minimum_seconds and charge >= minimum_ah:
                accepted.append((phase, segment, charge))
        minimum_segments = int(options["cell_diag_sequence_min_segments"])
        by_phase = {
            phase: [item for item in accepted if item[0] == phase]
            for phase in ("discharge", "charge")
        }
        qualified = {phase: items for phase, items in by_phase.items()
                     if len(items) >= minimum_segments}
        if not qualified:
            reason = ("Keine Phase besitzt genügend reproduzierbare Sequenzen: "
                      + ", ".join(f"{phase}={len(items)}" for phase, items in by_phase.items())
                      + f"; mindestens {minimum_segments} je Phase erforderlich.")
            empty = [_not_assessable(reason, 0, period) for _ in range(15)]
            return {"cells": empty, "segments": 0}, {"cells": [dict(item) for item in empty], "segments": 0}
        grid_count = int(options["cell_diag_curve_grid_points"])
        grid = [index / (grid_count - 1) for index in range(grid_count)]
        boundary = float(options["cell_diag_capacity_boundary_fraction"])
        sequence_results = {phase: [] for phase in qualified}
        for phase, phase_items in qualified.items():
            for _phase, segment, charge in phase_items:
                cumulative = [0.0]
                for before, after in zip(segment, segment[1:]):
                    dt = float(after["timestamp"]) - float(before["timestamp"])
                    cumulative.append(cumulative[-1] + (abs(float(before["current_a"]))
                        + abs(float(after["current_a"]))) / 2 * dt / 3600)
                if cumulative[-1] <= 0:
                    continue
                q_axis = [value / cumulative[-1] for value in cumulative]

                def interpolate(values, target):
                    if target < q_axis[0] or target > q_axis[-1]:
                        return None
                    for right in range(1, len(q_axis)):
                        if q_axis[right] >= target:
                            left = right - 1
                            span = q_axis[right] - q_axis[left]
                            ratio = 0 if span == 0 else (target - q_axis[left]) / span
                            return values[left] + ratio * (values[right] - values[left])
                    return values[-1]

                curves = [[interpolate([sample["voltages_mv"][cell] for sample in segment], point)
                           for point in grid] for cell in range(15)]
                if any(any(value is None for value in curve) for curve in curves):
                    continue
                median_curve = [statistics.median(curves[cell][point] for cell in range(15))
                                for point in range(grid_count)]
                rms = [math.sqrt(statistics.fmean(
                    (curves[cell][point] - median_curve[point]) ** 2
                    for point in range(grid_count))) for cell in range(15)]
                target_index = boundary * (grid_count - 1)
                left_index = int(math.floor(target_index))
                right_index = min(grid_count - 1, left_index + 1)
                target_ratio = target_index - left_index
                target_voltage = median_curve[left_index] + target_ratio * (
                    median_curve[right_index] - median_curve[left_index]
                )
                crossings = []
                for curve in curves:
                    crossing = None
                    for right in range(1, grid_count):
                        before, after = curve[right - 1], curve[right]
                        crossed = (before <= target_voltage <= after) if phase == "charge" else (before >= target_voltage >= after)
                        if crossed:
                            span = after - before
                            ratio = 0 if span == 0 else (target_voltage - before) / span
                            crossing = grid[right - 1] + ratio * (grid[right] - grid[right - 1])
                            break
                    crossings.append(crossing)
                capacity_eligible = not any(value is None for value in crossings)
                median_crossing = statistics.median(crossings) if capacity_eligible else None
                sequence_results[phase].append({
                    "charge_ah": charge, "q_axis": grid,
                    "median_curve_mv": [round(value, 3) for value in median_curve],
                    "crossing_q": crossings,
                    "crossing_delta": ([value - median_crossing for value in crossings]
                                       if capacity_eligible else [None] * 15),
                    "capacity_eligible": capacity_eligible,
                    "rms_mv": rms,
                })

        capacity_cells, curve_cells = [], []
        for cell in range(15):
            capacity_phases, curve_phases = {}, {}
            for phase, results in sequence_results.items():
                capacity_results = [result for result in results if result["capacity_eligible"]]
                if len(capacity_results) < minimum_segments:
                    reason = f"Nur {len(capacity_results)} Sequenzen erreichen den gemeinsamen Grenzbereich ohne Extrapolation; mindestens {minimum_segments} erforderlich."
                    capacity_phases[phase] = _not_assessable(reason, len(results), period)
                else:
                    crossing = [result["crossing_q"][cell] for result in capacity_results]
                    deltas = [result["crossing_delta"][cell] for result in capacity_results]
                    crossing_mad = statistics.median(abs(value - statistics.median(crossing)) for value in crossing)
                    capacity_reproducible = crossing_mad <= float(options["cell_diag_capacity_max_crossing_mad_fraction"])
                    common_capacity = {"segments": len(capacity_results), "valid_data": len(capacity_results),
                                       "transported_charge_ah": [round(result["charge_ah"], 4) for result in capacity_results],
                                       "q_common_from": 0.0, "q_common_to": 1.0,
                                       "q_grid_points": grid_count, "observation_period": period}
                    capacity_phases[phase] = {**common_capacity,
                        "status": "BEWERTBAR" if capacity_reproducible else "NICHT BEWERTBAR",
                        "quality": "HIGH" if capacity_reproducible and len(capacity_results) >= 2 * minimum_segments else "MEDIUM" if capacity_reproducible else "LOW",
                        "trend": _relative_trend([abs(value) for value in deltas], float(options["cell_diag_relative_trend_change_percent"])) if capacity_reproducible else "unklar",
                        "crossing_q_fraction": round(statistics.median(crossing), 4),
                        "delta_to_module_median_q_fraction": round(statistics.median(deltas), 4),
                        "crossing_mad_q_fraction": round(crossing_mad, 4),
                        "earlier_than_median_percent": round(100 * sum(value < 0 for value in deltas) / len(deltas), 1),
                        "unit": "normalisierter Q-Anteil",
                        "reason": None if capacity_reproducible else "Grenzbereichsreihenfolge ist über die Sequenzen nicht reproduzierbar."}
                if len(results) < minimum_segments:
                    reason = f"Nur {len(results)} vollständig gemeinsame Q-Sequenzen; mindestens {minimum_segments} erforderlich."
                    curve_phases[phase] = _not_assessable(reason, len(results), period)
                    continue
                rms_values = [result["rms_mv"][cell] for result in results]
                rms_mad = statistics.median(abs(value - statistics.median(rms_values)) for value in rms_values)
                curve_reproducible = rms_mad <= float(options["cell_diag_curve_max_rms_mad_mv"])
                common = {"segments": len(results), "valid_data": len(results),
                          "transported_charge_ah": [round(result["charge_ah"], 4) for result in results],
                          "q_common_from": 0.0, "q_common_to": 1.0,
                          "q_grid_points": grid_count, "observation_period": period}
                curve_phases[phase] = {**common,
                    "status": "BEWERTBAR" if curve_reproducible else "NICHT BEWERTBAR",
                    "quality": "HIGH" if curve_reproducible and len(results) >= 2 * minimum_segments else "MEDIUM" if curve_reproducible else "LOW",
                    "trend": _relative_trend(rms_values, float(options["cell_diag_relative_trend_change_percent"])) if curve_reproducible else "unklar",
                    "rms_deviation_mv": round(statistics.median(rms_values), 3),
                    "rms_mad_mv": round(rms_mad, 3), "unit": "mV",
                    "reason": None if curve_reproducible else "Kurven-RMS ist über die Sequenzen nicht reproduzierbar.",
                    "reference_curve": "punktweiser Median der 15 Zellkurven",
                    "interpolation": "linear auf gemeinsamer normalisierter Q-Achse; keine Extrapolation"}
            def combined(phases, explanation):
                assessable = [value for value in phases.values() if value["status"] == "BEWERTBAR"]
                if not assessable:
                    return {"status": "NICHT BEWERTBAR", "quality": "LOW", "trend": "unklar",
                            "valid_data": 0, "observation_period": period, "phases": phases,
                            "reason": "Keine Lade- oder Entladephase erfüllt Reproduzierbarkeit und Quality Gates.",
                            "explanation": explanation}
                return {"status": "BEWERTBAR",
                        "quality": min((value["quality"] for value in assessable),
                                       key=lambda value: {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[value]),
                        "trend": "verschlechternd" if any(value["trend"] == "verschlechternd" for value in assessable)
                        else "verbessernd" if any(value["trend"] == "verbessernd" for value in assessable) else "stabil",
                        "valid_data": sum(value["valid_data"] for value in assessable),
                        "observation_period": period, "phases": phases, "explanation": explanation}
            capacity_cells.append(combined(capacity_phases,
                "Reproduzierbare relative Grenzbereichsreihenfolge auf gemeinsamer Q-Achse; keine absolute Zellkapazität."))
            curve_cells.append(combined(curve_phases,
                "Phasengetrennte RMS-Abweichung von der Modulmedian-Kurve auf gemeinsamer normalisierter Q-Achse."))
        return {"cells": capacity_cells, "segments": len(accepted)}, {"cells": curve_cells, "segments": len(accepted)}

    def _rest(self, samples, options, period):
        threshold = float(options["cell_diag_rest_max_current_a"])
        minimum_seconds = float(options["cell_diag_rest_min_duration_seconds"])
        max_temp = float(options["cell_diag_quality_max_temperature_change_c"])
        segments, current = [], []
        previous_axis = None
        segment_predecessor = None
        for sample in samples:
            if abs(float(sample["current_a"])) <= threshold:
                if not current:
                    segment_predecessor = previous_axis
                current.append(sample)
            elif current:
                segments.append((segment_predecessor, current)); current = []
                previous_axis = "charge" if float(sample["current_a"]) > 0 else "discharge"
            else:
                previous_axis = "charge" if float(sample["current_a"]) > 0 else "discharge"
        if current:
            segments.append((segment_predecessor, current))
        accepted = [(predecessor, segment) for predecessor, segment in segments
                    if float(segment[-1]["timestamp"]) - float(segment[0]["timestamp"]) >= minimum_seconds
                    and max(value for sample in segment for value in sample["temperatures_c"])
                    - min(value for sample in segment for value in sample["temperatures_c"]) <= max_temp]
        if not accepted:
            reason = "Keine ausreichend lange, strom- und temperaturstabile Ruhephase."
            return {"cells": [_not_assessable(reason, 0, period) for _ in range(15)], "segments": 0}
        cells = []
        for index in range(15):
            slopes = []
            for _predecessor, segment in accepted:
                duration_hours = (float(segment[-1]["timestamp"]) - float(segment[0]["timestamp"])) / 3600
                start = segment[0]["voltages_mv"][index] - statistics.median(segment[0]["voltages_mv"])
                end = segment[-1]["voltages_mv"][index] - statistics.median(segment[-1]["voltages_mv"])
                slopes.append((end - start) / duration_hours)
            cells.append({"status": "BEWERTBAR", "quality": "MEDIUM",
                          "trend": _relative_trend([abs(value) for value in slopes], float(options["cell_diag_relative_trend_change_percent"])),
                          "relative_drift_mv_per_hour": round(statistics.median(slopes), 3), "unit": "mV/h",
                          "segments": len(accepted), "preceding_phases": sorted({item[0] or "unknown" for item in accepted}),
                          "valid_data": sum(len(item[1]) for item in accepted),
                          "observation_period": period,
                          "explanation": "Relative Relaxations-/Driftevidenz; keine automatische Aussage zur Selbstentladung."})
        return {"cells": cells, "segments": len(accepted)}

    def _balancing(self, samples, options, period):
        required = int(options["cell_diag_balancing_min_active_samples"])
        cells = []
        for index in range(15):
            known_samples = [sample for sample in samples if len(sample.get("balancing", ())) == 15]
            known = [sample["balancing"][index] for sample in known_samples]
            active_samples = [sample for sample in known_samples if sample["balancing"][index] is True]
            active = len(active_samples)
            if active < required:
                cells.append(_not_assessable(
                    f"Nur {active} dokumentierte aktive Balancing-Samples; Herstellerkriterien für weitere Gelegenheiten sind nicht belegt.",
                    len(known), period))
            else:
                relevant_deviations = 0
                deviations = []
                for sample in active_samples:
                    deviation = abs(sample["voltages_mv"][index] - statistics.median(sample["voltages_mv"]))
                    deviations.append(deviation)
                    phases = [phase for phase in self.phase_classifier(sample, options) if phase in PHASES]
                    limits = [float(options.get(f"cell_diag_{phase}_observe_deviation_mv",
                                                options["cell_diag_observe_deviation_mv"]))
                              for phase in phases]
                    relevant_deviations += bool(limits and deviation >= min(limits))
                context = ("WIEDERHOLTE STATUSWIRKSAME ABWEICHUNG BEI DOKUMENTIERTEM BALANCING"
                           if relevant_deviations >= required else
                           "BALANCING DOKUMENTIERT; KEINE WIEDERHOLTE STATUSWIRKSAME ABWEICHUNG IN DIESEN SAMPLES")
                cells.append({"status": "BEWERTBAR", "quality": "MEDIUM", "trend": "stabil",
                              "active_samples": active, "observed_samples": len(known),
                              "active_percent": round(100 * active / len(known), 1),
                              "median_deviation_during_balancing_mv": round(statistics.median(deviations), 2),
                              "status_effective_deviation_samples": relevant_deviations,
                              "context_classification": context,
                              "valid_data": len(known), "observation_period": period,
                              "explanation": "Beobachteter BMS-Balancing-Status; keine erfundene Spannungs-/Stromschwelle."})
        return {"cells": cells}

    def _ica_readiness(self, segments, options, period):
        required = int(options["cell_diag_ica_min_samples"])
        max_cv = float(options["cell_diag_ica_max_current_cv"])
        for phase, segment in segments:
            if phase != "charge" or len(segment) < required:
                continue
            currents = [abs(float(sample["current_a"])) for sample in segment]
            mean = statistics.fmean(currents)
            cv = statistics.pstdev(currents) / mean if mean else math.inf
            distinct = len({value for sample in segment for value in sample["voltages_mv"]})
            if cv <= max_cv and distinct >= int(options["cell_diag_ica_min_voltage_steps"]):
                return {"status": "DATEN GEEIGNET", "quality": "MEDIUM", "valid_data": len(segment),
                        "current_cv": round(cv, 3), "voltage_steps": distinct,
                        "observation_period": period,
                        "explanation": "Quality Gate bestanden; 0.7.0 aktiviert dennoch keine ICA/DVA-Berechnung."}
        return _not_assessable(
            "Kein ausreichend gleichmäßiger Ladeabschnitt mit genügend Messpunkten und Spannungsstufen.",
            sum(len(segment) for phase, segment in segments if phase == "charge"), period)

    def _maintenance(self, samples, events, ranking, period, options):
        start = float(samples[0]["timestamp"]) if samples else None
        end = float(samples[-1]["timestamp"]) if samples else None
        physical_serial = samples[-1].get("module_serial") if samples else None
        result = []
        for cell in range(15):
            if not physical_serial:
                result.append({"status": "NICHT BEWERTBAR", "quality": "LOW",
                               "trend": "unklar", "valid_data": 0,
                               "events": [], "event_count": 0,
                               "observation_period": period,
                               "reason": "Physische Modulidentität ist für den Diagnosezeitraum nicht sicher dokumentiert.",
                               "explanation": "Keine Korrelation anhand der aktuellen Modulposition."})
                continue
            matching = []
            ambiguous = 0
            for event in events:
                raw = event.to_dict() if hasattr(event, "to_dict") else dict(event)
                timestamp = datetime.fromisoformat(str(raw["occurred_at"]).replace("Z", "+00:00")).timestamp()
                if raw.get("archived_at") is not None or raw.get("cell_number") not in (None, cell + 1) \
                        or not (start is None or start <= timestamp <= end):
                    continue
                event_serial = raw.get("resolved_module_serial") or raw.get("module_serial")
                if not event_serial:
                    ambiguous += 1
                    continue
                if event_serial != physical_serial:
                    continue
                # A matching serial is authoritative even after a physical
                # move; module position is documentary, not identity.
                window = float(options["cell_diag_maintenance_context_window_hours"]) * 3600
                before = [sample for sample in samples if timestamp - window <= float(sample["timestamp"]) < timestamp]
                after = [sample for sample in samples if timestamp < float(sample["timestamp"]) <= timestamp + window]

                def state(values):
                    deviations = [sample["voltages_mv"][cell] - statistics.median(sample["voltages_mv"])
                                  for sample in values]
                    return {
                        "status": "BEWERTBAR" if deviations else "NICHT BEWERTBAR",
                        "samples": len(deviations),
                        "median_deviation_mv": round(statistics.median(deviations), 2) if deviations else None,
                    }

                matching.append({"maintenance_event_id": raw["maintenance_event_id"],
                                 "occurred_at": raw["occurred_at"], "category": raw["category"],
                                 "title": raw["title"], "association_only": True,
                                 "physical_module_serial": physical_serial,
                                 "identity_source": raw.get("identity_status", "explicit")})
                matching[-1]["before"] = state(before)
                matching[-1]["after"] = state(after)
                matching[-1]["context_window_hours"] = options["cell_diag_maintenance_context_window_hours"]
                matching[-1]["elapsed_to_first_after_seconds"] = (
                    round(float(after[0]["timestamp"]) - timestamp, 1) if after else None
                )
                before_state, after_state = matching[-1]["before"], matching[-1]["after"]
                if before_state["status"] == after_state["status"] == "BEWERTBAR":
                    before_abs = abs(before_state["median_deviation_mv"])
                    after_abs = abs(after_state["median_deviation_mv"])
                    matching[-1]["trend"] = _relative_trend(
                        [before_abs, after_abs],
                        float(options["cell_diag_relative_trend_change_percent"]),
                    )
                else:
                    matching[-1]["trend"] = "unklar"
            qualified = [item for item in matching if item["trend"] != "unklar"]
            if matching:
                status, quality = "BEWERTBAR", "MEDIUM" if qualified else "LOW"
                trend = matching[-1]["trend"]
                reason = None if qualified else "Vorher-/Nachher-Daten reichen für keine Trendrichtung."
            elif ambiguous:
                status, quality, trend = "NICHT BEWERTBAR", "LOW", "unklar"
                reason = f"{ambiguous} zeitlich passende Ereignisse ohne sicher auflösbare physische Identität."
            else:
                status, quality, trend = "KEIN EREIGNIS IM ZEITRAUM", "LOW", "unklar"
                reason = None
            result.append({"status": status, "quality": quality, "trend": trend,
                           "valid_data": sum(item["before"]["samples"] + item["after"]["samples"] for item in matching),
                           "events": matching, "event_count": len(matching),
                           "last_relevant_event": matching[-1] if matching else None,
                           "ambiguous_event_count": ambiguous, "physical_module_serial": physical_serial,
                           "observation_period": period, "reason": reason,
                           "explanation": "Zeitliche Zuordnung zur physischen Serienidentität ist eine Korrelation; keine Kausalitätsaussage."})
        return result
