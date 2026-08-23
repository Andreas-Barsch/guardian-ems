import json
from unittest.mock import patch

import pytest

from maintenance_mqtt import MaintenanceMqttPublisher
from config_ui import DEFAULTS
from mqtt_projection import (MQTT_MAX_ATTRIBUTE_BYTES, MQTT_MAX_PAYLOAD_BYTES,
                             compact_battery_diagnostics, compact_cell_attributes,
                             compact_diagnostic_method_summary)


with patch("pathlib.Path.mkdir", return_value=None):
    from main import Mqtt, Module


FORBIDDEN = {
    "advanced_diagnostics", "methods",
    "transported_charge_ah", "q_axis", "median_curve_mv", "raw_samples",
    "daily_aggregates_records",
}


class FakeResult:
    rc = 0


class FakeClient:
    def __init__(self):
        self.calls = []

    def publish(self, topic, payload, retain=False):
        encoded = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.calls.append({"topic": topic, "payload": payload,
                           "size": len(encoded), "retain": retain})
        return FakeResult()


def method(status="BEWERTBAR"):
    return {
        "status": status, "quality": "HIGH", "trend": "stabil", "valid_data": 8640,
        "reason": "R" * 5000,
        "phases": {
            phase: {
                "status": status, "quality": "HIGH", "trend": "stabil",
                "valid_data": 2160, "segments": 120,
                "transported_charge_ah": [1.2345] * 1000,
                "q_axis": [index / 100 for index in range(101)],
            }
            for phase in ("discharge", "low", "charge", "high")
        },
    }


def diagnostic_result(module):
    methods = {
        name: method()
        for name in (
            "ranking_drift", "dynamic_resistance", "capacity_consistency",
            "curve_analysis", "rest_drift", "balancing_context",
        )
    }
    cells = []
    advanced_cells = []
    for cell in range(1, 16):
        diagnostics = {
            "current_condition": "NORMAL", "trend": "stabil",
            "maintenance_risk": "kein Hinweis",
            "maintenance_risk_reason": "M" * 5000,
            "trend_risk_confidence": "HIGH", "method_quality": "HIGH",
            "methods": methods,
            "evidence_families": {"capacity_curve": method()},
            "maintenance_context": {"events": [{"title": "E" * 5000}] * 100},
            "ica_dva_readiness": method(),
        }
        advanced_cells.append(diagnostics)
        cells.append({
            "cell": cell, "status": "NORMAL", "confidence": "HIGH",
            "current_voltage_mv": 3300 + cell, "current_deviation_mv": cell - 8,
            "evidence_deviation_mv": abs(cell - 8), "evidence_phase": "charge",
            "phases": {
                phase: {"status": "NORMAL", "samples": 2160,
                        "median_deviation_mv": cell - 8, "mean_rank": cell}
                for phase in ("discharge", "low", "charge", "high")
            },
            "diagnostics": diagnostics,
        })
    return {
        "module": module, "status": "NORMAL", "confidence": "HIGH",
        "sample_count": 8640, "current_median_mv": 3308,
        "evidence_worst_cell": 15, "evidence_deviation_mv": 7,
        "evidence_phase": "charge", "trend": "stabil",
        "maintenance_risk": "kein Hinweis", "trend_risk_confidence": "HIGH",
        "cells": cells,
        "advanced_diagnostics": {
            "schema_version": 1, "guardian_version": "0.7.4",
            "diagnostic_engine_version": "0.4.12", "config_id": f"cfg-{module}",
            "cells": advanced_cells, "raw_samples": [{"voltage": 3300}] * 8640,
        },
    }


def walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_keys(child)


def modules():
    return [
        Module(index, 49.5, -2.0, 25, 24, 26, 3.28, 3.32,
               "normal", "normal", "normal", "normal", 55)
        for index in range(1, 7)
    ]


def test_projection_is_compact_and_excludes_internal_structures():
    result = diagnostic_result(1)
    module_projection = compact_battery_diagnostics(
        {1: result}, {1: {"barcode": "SN-1"}}
    )
    cell_projection = compact_cell_attributes(
        result["cells"][0], result["advanced_diagnostics"]
    )
    summary = compact_diagnostic_method_summary(result["cells"][0]["diagnostics"])
    assert module_projection[0]["physical_module_serial"] == "SN-1"
    assert cell_projection["provenance_id"] == "cfg-1"
    assert summary["capacity_consistency"]["status"] == "BEWERTBAR"
    assert cell_projection["diagnostic_methods"]["curve_analysis"]["status"] == "BEWERTBAR"
    assert {
        "trend_risk_confidence_basis", "evidence_families", "diagnostic_methods",
        "balancing_context", "maintenance_context", "ica_dva_readiness",
        "diagnostic_provenance", "maintenance_risk_reason",
    } <= set(cell_projection)
    assert not (set(walk_keys(module_projection)) | set(walk_keys(cell_projection))) & FORBIDDEN
    assert len(json.dumps(cell_projection, ensure_ascii=False).encode()) <= MQTT_MAX_ATTRIBUTE_BYTES
    assert "advanced_diagnostics" in result
    assert "transported_charge_ah" in set(walk_keys(result))


def test_six_module_ninety_cell_worst_case_all_mqtt_packets_are_bounded_and_retained():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = client
    publisher.discovery_enabled = True
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    publisher.discovery(6)

    results = {module: diagnostic_result(module) for module in range(1, 7)}
    infos = {module: {"device_name": "US2000C", "barcode": f"SN-{module}"}
             for module in range(1, 7)}
    options = {**DEFAULTS,
        "cell_diagnostics_enabled": True, "cell_diagnostics_interval_seconds": 60,
        "cell_diag_min_phase_samples": 30, "cell_diag_confidence_medium_samples": 120,
        "cell_diag_confidence_high_samples": 600, "cell_diag_low_soc_percent": 30,
        "cell_diag_high_soc_percent": 80, "cell_diag_charge_current_a": .8,
        "cell_diag_discharge_current_a": .8, "cell_diag_observe_deviation_mv": 10,
        "cell_diag_warning_deviation_mv": 20, "cell_diag_critical_deviation_mv": 40,
    }
    for cycle in range(2):
        publisher.publish(
            modules(), "ok", [], options, {"alarm_counts": {}}, {},
            {"active": False, "last_summary": "kein Incident"}, results,
            {"soh_percent": 95, "cycles": 123}, infos,
        )

    assert max(call["size"] for call in client.calls) <= MQTT_MAX_PAYLOAD_BYTES
    attributes = [call for call in client.calls if call["topic"].endswith("/attributes")]
    assert max(call["size"] for call in attributes) <= MQTT_MAX_ATTRIBUTE_BYTES
    battery_states = [call for call in client.calls if call["topic"] == "guardian/battery/state"]
    assert len(battery_states) == 2
    assert all(call["retain"] for call in battery_states)
    assert all(call["size"] <= MQTT_MAX_PAYLOAD_BYTES for call in battery_states)
    state = json.loads(battery_states[-1]["payload"])
    assert len(state["cell_diagnostics"]) == 6
    assert not set(walk_keys(state)) & FORBIDDEN
    discovery = [call for call in client.calls if call["topic"].startswith("homeassistant/")]
    assert discovery and all(call["retain"] for call in discovery)


def test_hard_payload_guards_fail_before_client_publish():
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = FakeClient()
    with pytest.raises(ValueError, match="exceeds"):
        publisher._publish("guardian/too-large", "x" * (MQTT_MAX_PAYLOAD_BYTES + 1), retain=True)
    with pytest.raises(ValueError, match="exceeds"):
        publisher.attributes("too_large", {"value": "x" * MQTT_MAX_ATTRIBUTE_BYTES})
    assert publisher.client.calls == []
