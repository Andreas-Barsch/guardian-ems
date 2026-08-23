"""Stable, bounded MQTT projections for Guardian diagnostic results."""

from __future__ import annotations

from typing import Any, Mapping


MQTT_MAX_PAYLOAD_BYTES = 65_536
MQTT_MAX_ATTRIBUTE_BYTES = 16_384
MQTT_MAX_TEXT_CHARS = 384


def compact_text(value: Any, limit: int = MQTT_MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _method_value(value: Mapping[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    scalar_keys = (
        "status", "quality", "trend", "valid_data", "events", "segments",
        "mean_rank", "rank_trend_per_day", "rank_drift_detected",
        "median_deviation_mv", "median_deviation_trend_mv_per_day",
        "lowest_percent", "highest_percent", "persistence_percent",
        "daily_aggregates", "coverage_percent", "relative_resistance_index",
        "relative_mad", "relative_drift_mv_per_hour", "active_samples",
        "observed_samples", "median_deviation_during_balancing_mv",
        "current_cv", "voltage_steps", "event_count", "ambiguous_event_count",
        "physical_module_serial",
    )
    result = {
        key: value.get(key)
        for key in scalar_keys
        if value.get(key) is not None and not isinstance(value.get(key), (dict, list))
    }
    period = value.get("observation_period")
    if isinstance(period, Mapping):
        result["observation_period"] = {
            key: period.get(key) for key in ("from", "to", "seconds")
        }
    reason = value.get("reason") or value.get("explanation")
    if reason:
        result["reason"] = compact_text(reason)
    phases = value.get("phases")
    if isinstance(phases, Mapping):
        result["phases"] = {
            str(phase): {
                key: details.get(key)
                for key in (
                    "status", "quality", "trend", "valid_data", "segments",
                    "mean_rank", "rank_trend_per_day", "median_deviation_mv",
                    "median_deviation_trend_mv_per_day", "lowest_percent",
                    "highest_percent", "persistence_percent", "daily_aggregates",
                    "coverage_percent", "crossing_q_fraction",
                    "delta_to_module_median_q_fraction", "crossing_mad_q_fraction",
                    "rms_deviation_mv", "rms_mad_mv", "q_grid_points",
                )
                if isinstance(details, Mapping) and details.get(key) is not None
            }
            for phase, details in phases.items()
        }
    return result


def _family_summary(families: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        str(name): {
            key: value.get(key)
            for key in ("family", "status", "quality", "direction")
            if isinstance(value, Mapping) and value.get(key) is not None
        }
        for name, value in (families or {}).items()
        if isinstance(value, Mapping)
    }


def _maintenance_summary(value: Mapping[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    result = _method_value(value)
    event = value.get("last_relevant_event")
    if isinstance(event, Mapping):
        compact_event = {
            key: event.get(key)
            for key in (
                "maintenance_event_id", "occurred_at", "category", "title",
                "elapsed_to_first_after_seconds", "trend",
            )
            if event.get(key) is not None
        }
        compact_event["title"] = compact_text(compact_event.get("title"))
        for name in ("before", "after"):
            state = event.get(name)
            if isinstance(state, Mapping):
                compact_event[name] = {
                    key: state.get(key)
                    for key in ("status", "samples", "median_deviation_mv")
                    if state.get(key) is not None
                }
        result["last_relevant_event"] = compact_event
    return result


def compact_diagnostic_method_summary(diagnostics: Mapping[str, Any] | None) -> dict[str, Any]:
    diagnostics = diagnostics or {}
    source = diagnostics.get("methods", {})
    result = {
        str(name): _method_value(value)
        for name, value in source.items()
        if isinstance(value, Mapping)
    }
    readiness = diagnostics.get("ica_dva_readiness")
    if isinstance(readiness, Mapping):
        result["ica_dva"] = _method_value(readiness)
    return result


def compact_module_state(module: int, result: Mapping[str, Any] | None,
                         physical_serial: str | None = None) -> dict[str, Any]:
    result = result or {}
    advanced = result.get("advanced_diagnostics", {})
    provenance_id = advanced.get("config_id") if isinstance(advanced, Mapping) else None
    return {
        "module": int(module),
        "physical_module_serial": compact_text(
            physical_serial or result.get("physical_module_serial"), 200
        ),
        "status": result.get("status"),
        "current_condition": result.get("status"),
        "current_condition_confidence": result.get("confidence"),
        "worst_cell": result.get("evidence_worst_cell"),
        "evidence_phase": result.get("evidence_phase"),
        "evidence_deviation_mv": result.get("evidence_deviation_mv"),
        "sample_count": result.get("sample_count"),
        "trend": result.get("trend"),
        "maintenance_risk": result.get("maintenance_risk"),
        "trend_risk_confidence": result.get("trend_risk_confidence"),
        "provenance_id": provenance_id,
    }


def compact_battery_diagnostics(results: Mapping[int, Mapping[str, Any]],
                                module_infos: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        compact_module_state(
            module,
            result,
            (module_infos.get(module, {}) or {}).get("barcode"),
        )
        for module, result in sorted(results.items(), key=lambda item: int(item[0]))
    ]


def compact_cell_attributes(cell: Mapping[str, Any],
                            advanced: Mapping[str, Any] | None = None) -> dict[str, Any]:
    diagnostics = cell.get("diagnostics", {}) or {}
    advanced = advanced or {}
    phases = cell.get("phases", {}) or {}
    reason = diagnostics.get("maintenance_risk_reason")
    return {
        "cell": cell.get("cell"),
        "current_voltage_mv": cell.get("current_voltage_mv"),
        "current_deviation_mv": cell.get("current_deviation_mv"),
        "current_condition": diagnostics.get("current_condition", cell.get("status")),
        "confidence": cell.get("confidence"),
        "phase_summary": {
            phase: {
                "status": details.get("status", "LERNPHASE"),
                "samples": details.get("samples", 0),
            }
            for phase in ("discharge", "low", "charge", "high")
            for details in [phases.get(phase, {}) or {}]
        },
        "trend": diagnostics.get("trend"),
        "maintenance_risk": diagnostics.get("maintenance_risk"),
        "trend_risk_confidence": diagnostics.get("trend_risk_confidence"),
        "method_quality": diagnostics.get("method_quality"),
        "reason": compact_text(reason),
        "maintenance_risk_reason": compact_text(reason),
        "trend_risk_confidence_basis": {
            key: diagnostics.get("trend_risk_confidence_basis", {}).get(key)
            for key in (
                "independent_families", "high_quality_families",
                "observation_seconds", "data_coverage_percent",
                "qualified_data_families",
            )
        },
        "evidence_families": _family_summary(diagnostics.get("evidence_families")),
        "diagnostic_methods": compact_diagnostic_method_summary(diagnostics),
        "balancing_context": _method_value(
            (diagnostics.get("methods", {}) or {}).get("balancing_context")
        ),
        "maintenance_context": _maintenance_summary(diagnostics.get("maintenance_context")),
        "ica_dva_readiness": _method_value(diagnostics.get("ica_dva_readiness")),
        "diagnostic_provenance": {
            "schema_version": advanced.get("schema_version"),
            "guardian_version": advanced.get("guardian_version"),
            "diagnostic_engine_version": advanced.get("diagnostic_engine_version"),
            "config_id": advanced.get("config_id"),
            "evaluated_at": advanced.get("evaluated_at"),
            "observation_period": advanced.get("observation_period"),
            "single_pass_sample_count": advanced.get("single_pass_sample_count"),
        },
        "provenance_id": advanced.get("config_id"),
    }
