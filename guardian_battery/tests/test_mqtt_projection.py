import json
from unittest.mock import patch

import pytest

from maintenance_mqtt import MaintenanceMqttPublisher
from config_ui import DEFAULTS
from mqtt_projection import (MQTT_MAX_ATTRIBUTE_BYTES, MQTT_MAX_PAYLOAD_BYTES,
                             compact_battery_diagnostics, compact_cell_attributes,
                             compact_diagnostic_method_summary)
from derived_mqtt_projection import publish_derived_results


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


def managed_result(module=1, generation=7, serial="SN-1", config="cfg-1"):
    result = diagnostic_result(module)
    result["physical_module_serial"] = serial
    result["advanced_diagnostics"]["config_id"] = config
    result.update({
        "analysis_generation": generation,
        "analysis_source_sample_at": 100.0,
        "analysis_analyzed_at": 105.0,
        "analysis_age_seconds": 5.0,
    })
    context = {
        "generation": generation, "config_id": config,
        "module_identities": {module: serial},
    }
    return result, context


def publish_managed(publisher, module, result, context, *, status="ok"):
    publisher.publish(
        [module], status, [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"},
        {module.module: result}, {}, {module.module: {"barcode": "SN-1"}},
        analysis_context=context)


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


def test_module_projection_exposes_derived_analysis_freshness():
    result = diagnostic_result(1) | {
        "analysis_generation": 12,
        "analysis_source_sample_at": 100.0,
        "analysis_analyzed_at": 105.0,
        "analysis_age_seconds": 7.5,
    }
    projected = compact_battery_diagnostics({1: result}, {})[0]
    assert projected["analysis_generation"] == 12
    assert projected["analysis_source_sample_at"] == 100.0
    assert projected["analysis_analyzed_at"] == 105.0
    assert projected["analysis_age_seconds"] == 7.5
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


def test_same_analysis_generation_skips_only_derived_and_keeps_live_projection():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    result, context = managed_result()
    first_module = modules()[0]
    publish_managed(publisher, first_module, result, context)
    first_count = len(client.calls)
    client.calls.clear()
    changed = Module(**(first_module.__dict__ | {"soc_percent": 54}))
    publish_managed(publisher, changed, result, context)
    topics = [call["topic"] for call in client.calls]
    assert len(client.calls) < first_count
    assert "guardian/battery/sensor/module_1_soc/state" in topics
    assert "guardian/battery/sensor/module_1_cell_delta/state" in topics
    assert "guardian/battery/sensor/module_1_cell_diag_live/state" in topics
    assert "guardian/battery/sensor/module_1_cell_1_status/state" not in topics
    assert "guardian/battery/sensor/module_1_cell_diag_status/state" not in topics
    assert client.calls[topics.index("guardian/battery/sensor/module_1_soc/state")]["payload"] == "54"


def test_same_generation_keeps_new_live_alarm_and_mixed_battery_state_current():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    result, context = managed_result()
    module = modules()[0]
    publish_managed(publisher, module, result, context)
    client.calls.clear()
    alarm = {"level": "critical", "code": "cell_delta_critical",
             "module": 1, "message": "critical live delta"}
    publisher.publish(
        [module], "critical", [alarm], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {1: result}, {},
        {1: {"barcode": "SN-1"}}, analysis_context=context)
    calls = {item["topic"]: item["payload"] for item in client.calls}
    assert json.loads(calls["guardian/battery/alarms"]) == [alarm]
    assert json.loads(calls["guardian/battery/state"])["status"] == "critical"
    assert calls["guardian/battery/sensor/stack_status/state"] == "critical"
    assert "guardian/battery/sensor/module_1_cell_1_status/state" not in calls


def test_new_generation_config_identity_and_reconnect_each_republish_derived():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    module = modules()[0]
    result, context = managed_result()
    publish_managed(publisher, module, result, context)
    derived_topic = "guardian/battery/sensor/module_1_cell_1_status/state"
    client.calls.clear(); publish_managed(publisher, module, result, context)
    assert derived_topic not in [call["topic"] for call in client.calls]
    for changed_result, changed_context in (
        managed_result(generation=8),
        managed_result(generation=8, config="cfg-2"),
        managed_result(generation=8, serial="SN-2", config="cfg-2"),
    ):
        client.calls.clear()
        publish_managed(publisher, module, changed_result, changed_context)
        assert derived_topic in [call["topic"] for call in client.calls]
    publisher._on_mqtt_connect(None, None, None, 0)
    client.calls.clear()
    publisher.begin_cycle_profile(9)
    publish_managed(publisher, module, changed_result, changed_context)
    profile = publisher.finish_cycle_profile(1, .1)
    assert derived_topic in [call["topic"] for call in client.calls]
    assert profile["derived_publish_invalidated_by_reconnect"] is True


def test_initial_connect_and_reconnect_restore_retained_global_availability():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher._ensure_derived_publish_state()

    publisher._on_mqtt_connect(client, None, None, 0)
    client.publish("guardian/battery/availability", "offline", retain=True)
    publisher._last_successfully_published_derived_key = (7, "cfg", (), 1)
    publisher._on_mqtt_connect(client, None, None, 0)

    availability = [call for call in client.calls
                    if call["topic"] == "guardian/battery/availability"]
    assert [(call["payload"], call["retain"]) for call in availability] == [
        ("online", True), ("offline", True), ("online", True)]
    assert publisher._derived_republish_required_by_reconnect is True


def test_connect_availability_publish_failure_is_callback_isolated():
    class Failing(FakeClient):
        def publish(self, topic, payload, retain=False):
            raise RuntimeError("broker unavailable")

    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = Failing()
    publisher._ensure_derived_publish_state()
    publisher._on_mqtt_connect(publisher.client, None, None, 0)
    assert publisher._derived_reconnect_epoch == 1


def test_connection_loss_during_derived_publish_does_not_mark_success():
    class Disconnecting(FakeClient):
        def __init__(self):
            super().__init__(); self.connected = True

        def is_connected(self):
            return self.connected

        def publish(self, topic, payload, retain=False):
            result = super().publish(topic, payload, retain)
            if topic.endswith("cell_1_status/state"):
                self.connected = False
            return result

    client = Disconnecting()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    result, context = managed_result()
    publish_managed(publisher, modules()[0], result, context)
    assert publisher._last_successfully_published_derived_key is None
    assert publisher._derived_publish_failure_count == 1


def test_failed_derived_return_code_does_not_mark_generation_complete():
    class RcClient(FakeClient):
        def publish(self, topic, payload, retain=False):
            super().publish(topic, payload, retain)
            result = FakeResult()
            result.rc = 4 if topic.endswith("cell_1_status/state") else 0
            return result

    client = RcClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    result, context = managed_result()
    publish_managed(publisher, modules()[0], result, context)
    assert publisher._last_successfully_published_derived_key is None
    first = len(client.calls); client.calls.clear()
    publish_managed(publisher, modules()[0], result, context)
    assert len(client.calls) == first
    assert publisher._derived_publish_failure_count == 2


def test_managed_first_publish_preserves_legacy_topics_payload_order_and_retain():
    result, context = managed_result()
    module = modules()[0]
    clients = [FakeClient(), FakeClient()]
    publishers = []
    for client in clients:
        value = Mqtt.__new__(Mqtt)
        value.prefix = "guardian"; value.client = client
        value.discovery_enabled = False
        value.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
        publishers.append(value)
    publishers[0].publish(
        [module], "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {1: result}, {},
        {1: {"barcode": "SN-1"}})
    publish_managed(publishers[1], module, result, context)
    assert [(item["topic"], item["retain"]) for item in clients[1].calls] == \
        [(item["topic"], item["retain"]) for item in clients[0].calls]
    for managed, legacy in zip(clients[1].calls, clients[0].calls):
        if managed["topic"] == "guardian/battery/state":
            managed_payload = json.loads(managed["payload"])
            legacy_payload = json.loads(legacy["payload"])
            managed_payload.pop("timestamp"); legacy_payload.pop("timestamp")
            assert managed_payload == legacy_payload
        elif managed["topic"].endswith("/last_update/state"):
            assert managed["payload"] and legacy["payload"]
        else:
            assert managed["payload"] == legacy["payload"]


def test_derived_exception_retries_and_cycle_observability_is_bounded():
    class BrokenOnce(FakeClient):
        def __init__(self):
            super().__init__(); self.broken = True

        def publish(self, topic, payload, retain=False):
            if self.broken and topic.endswith("cell_1_status/state"):
                self.broken = False
                raise OSError("derived broken")
            return super().publish(topic, payload, retain)

    client = BrokenOnce()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    result, context = managed_result()
    publisher.begin_cycle_profile(1)
    with pytest.raises(OSError, match="derived broken"):
        publish_managed(publisher, modules()[0], result, context)
    assert publisher._last_successfully_published_derived_key is None
    publisher.finish_cycle_profile(1, .1)
    publisher.begin_cycle_profile(2)
    publish_managed(publisher, modules()[0], result, context)
    profile = publisher.finish_cycle_profile(1, .1)
    assert profile["derived_publish_performed"] is True
    assert profile["derived_publish_skipped"] is False
    assert profile["derived_publish_generation"] == 7
    assert profile["last_successfully_published_generation"] == 7
    assert profile["derived_publish_performed_count"] == 1
    assert profile["derived_publish_failure_count"] == 1
    publisher.begin_cycle_profile(3)
    publish_managed(publisher, modules()[0], result, context)
    skipped = publisher.finish_cycle_profile(1, .1)
    assert skipped["derived_publish_skipped"] is True
    assert skipped["groups"]["cell_diagnostics"]["publish_count"] < \
        profile["groups"]["cell_diagnostics"]["publish_count"]


def test_extracted_worker_projection_matches_legacy_derived_burst_exactly():
    result, _context = managed_result()
    clients = [FakeClient(), FakeClient()]
    publishers = []
    for client in clients:
        value = Mqtt.__new__(Mqtt); value.prefix = "guardian"; value.client = client
        value.discovery_enabled = False; value._cycle_profiler = None
        value.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
        publishers.append(value)
    publishers[0]._publish_cycle(
        [modules()[0]], "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {1: result}, {},
        {1: {"barcode": "SN-1"}}, publish_derived=True)
    publish_derived_results(publishers[1], (1,), {1: result})
    worker_calls = clients[1].calls
    legacy = {item["topic"]: item for item in clients[0].calls}
    assert worker_calls
    assert [legacy[item["topic"]] for item in worker_calls] == worker_calls
    assert len({item["topic"] for item in worker_calls}) == len(worker_calls)


def test_runtime_live_projection_excludes_all_worker_owned_topics():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt); publisher.prefix = "guardian"
    publisher.client = client; publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    result, context = managed_result()
    publisher.publish(
        [modules()[0]], "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "none"}, {1: result}, {},
        {1: {"barcode": "SN-1"}}, analysis_context=context,
        include_derived=False)
    topics = {item["topic"] for item in client.calls}
    assert "guardian/battery/sensor/module_1_soc/state" in topics
    assert "guardian/battery/sensor/module_1_cell_diag_live/state" in topics
    assert "guardian/battery/sensor/module_1_cell_1_status/state" not in topics
    assert "guardian/battery/sensor/module_1_cell_diag_status/state" not in topics


def test_live_topology_projects_four_of_five_and_invalidates_retained_live_values():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = client
    publisher.discovery_enabled = True
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    publisher.discovery(5)
    topology = {
        position: {"position": position, "expected": position <= 5,
                   "status": "present" if position <= 4 else
                             "absent" if position == 5 else "not_expected"}
        for position in range(1, 7)
    }
    publisher.publish(
        modules()[:4], "critical", [{"message": "Modul 5 liefert keine Daten"}],
        DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {}, {}, {}, topology)

    states = {call["topic"]: call["payload"] for call in client.calls}
    assert states["guardian/battery/sensor/modules_present/state"] == "4 / 5"
    assert states["guardian/battery/sensor/stack_status_reason/state"] == \
        "Modul 5 liefert keine Daten"
    assert states["guardian/battery/module_5/availability"] == "offline"
    assert states["guardian/battery/module_6/availability"] == "offline"
    assert states["guardian/battery/sensor/module_5_presence/state"] == "absent"
    assert states["guardian/battery/sensor/module_6_presence/state"] == "not_expected"
    configs = [json.loads(call["payload"]) for call in client.calls
               if call["topic"].endswith("/config")]
    module_five_soc = next(item for item in configs
                           if item["unique_id"] == "guardian_battery_module_5_soc")
    assert module_five_soc["availability_topic"] == "guardian/battery/module_5/availability"
    module_five_info = next(item for item in configs
                            if item["unique_id"] == "guardian_battery_module_5_info")
    assert module_five_info["availability_topic"] == "guardian/battery/availability"
    assert len({item["unique_id"] for item in configs}) == len(configs)
    assert not any(call["payload"] is None for call in client.calls)


def test_unexpected_present_module_is_online_but_not_counted_as_expected():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    topology = {
        position: {"position": position, "expected": position <= 5,
                   "status": "present"}
        for position in range(1, 7)
    }
    publisher.publish(
        modules(), "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {}, {}, {}, topology)
    states = {call["topic"]: call["payload"] for call in client.calls}
    assert states["guardian/battery/sensor/modules_present/state"] == \
        "5 / 5 (+1 nicht erwartet)"
    assert states["guardian/battery/sensor/unexpected_modules_present/state"] == "1"
    assert states["guardian/battery/module_6/availability"] == "online"
    attrs = json.loads(states["guardian/battery/sensor/modules_present/attributes"])
    assert attrs == {"present_expected": 5, "expected_module_count": 5,
                     "unexpected_present": 1}


@pytest.mark.parametrize("expected,diagnostic_status", [
    (True, "NORMAL"),
    (True, "KRITISCH"),
    (False, "NORMAL"),
    (False, "KRITISCH"),
])
def test_present_module_keeps_diagnosis_separate_from_topology(
        expected, diagnostic_status):
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = client
    publisher.discovery_enabled = True
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    publisher.discovery(5)
    topology = {6: {"position": 6, "expected": expected,
                    "status": "present", "observed_serial": "SERIAL-M6"}}
    publisher.publish(
        modules(), "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"},
        {6: {"status": diagnostic_status, "cells": []}}, {}, {}, topology)
    states = {call["topic"]: call["payload"] for call in client.calls}
    assert states["guardian/battery/sensor/module_6_cell_diag_live/state"] == \
        diagnostic_status
    attrs = json.loads(
        states["guardian/battery/sensor/module_6_cell_diag_live/attributes"])
    assert attrs["diagnostic_status"] == diagnostic_status
    assert attrs["presence_status"] == "present"
    assert attrs["expected"] is expected
    assert attrs["physical_serial"] == "SERIAL-M6"
    assert attrs["topology_label"] == ("" if expected else "NICHT ERWARTET")
    config = next(json.loads(call["payload"]) for call in client.calls
                  if call["topic"].endswith("module_6_cell_diag_live/config"))
    assert config["availability_topic"] == "guardian/battery/availability"


def test_live_diagnostic_projection_marks_stale_absent_and_unknown_without_nulls():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    base = {position: {"position": position, "expected": True,
                       "status": "present"}
            for position in range(1, 7)}
    publisher.publish(
        modules(), "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"},
        {1: {"status": "NORMAL", "cells": []}}, {}, {}, base)
    first_count = len(client.calls)
    changed = {**base, 1: {"position": 1, "expected": True, "status": "stale"},
               2: {"position": 2, "expected": True, "status": "absent"},
               3: {"position": 3, "expected": True, "status": "unknown"},
               6: {"position": 6, "expected": False, "status": "not_expected"}}
    publisher.publish(
        [], "critical", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {}, {}, {}, changed)
    recent = client.calls[first_count:]
    states = {call["topic"]: call["payload"] for call in recent}
    assert "guardian/battery/sensor/module_1_cell_diag_live/state" not in states
    stale_attrs = json.loads(
        states["guardian/battery/sensor/module_1_cell_diag_live/attributes"])
    assert stale_attrs["topology_label"] == "VERALTET"
    assert states["guardian/battery/sensor/module_2_cell_diag_live/state"] == \
        "ENTFERNT / NICHT VERFÜGBAR"
    assert states["guardian/battery/sensor/module_3_cell_diag_live/state"] == \
        "STATUS UNBEKANNT"
    assert states["guardian/battery/sensor/module_6_cell_diag_live/state"] == \
        "NICHT ERWARTET · NICHT VORHANDEN"
    assert not any(call["payload"] is None for call in recent)


def test_hard_payload_guards_fail_before_client_publish():
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"
    publisher.client = FakeClient()
    with pytest.raises(ValueError, match="exceeds"):
        publisher._publish("guardian/too-large", "x" * (MQTT_MAX_PAYLOAD_BYTES + 1), retain=True)
    with pytest.raises(ValueError, match="exceeds"):
        publisher.attributes("too_large", {"value": "x" * MQTT_MAX_ATTRIBUTE_BYTES})
    assert publisher.client.calls == []


def test_full_diagnostics_mqtt_profile_counts_bytes_and_preserves_output():
    results = {module: diagnostic_result(module) for module in range(1, 7)}
    infos = {module: {"device_name": "US2000C", "barcode": f"SN-{module}"}
             for module in range(1, 7)}
    args = (modules(), "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
            {"active": False, "last_summary": "kein Incident"}, results,
            {"soh_percent": 95, "cycles": 123}, infos)
    plain_client = FakeClient()
    plain = Mqtt.__new__(Mqtt)
    plain.prefix = "guardian"; plain.client = plain_client
    plain.discovery_enabled = False
    plain.maintenance_events = MaintenanceMqttPublisher(plain_client, "guardian")
    plain._cycle_profiler = None
    plain.publish(*args)

    profiled_client = FakeClient()
    profiled = Mqtt.__new__(Mqtt)
    profiled.prefix = "guardian"; profiled.client = profiled_client
    profiled.discovery_enabled = False
    profiled.maintenance_events = MaintenanceMqttPublisher(
        profiled_client, "guardian")
    profiled._cycle_profiler = None
    profiled.begin_cycle_profile(42)
    started = __import__("time").monotonic()
    cpu_started = __import__("time").thread_time()
    profiled.publish(*args)
    profile = profiled.finish_cycle_profile(
        __import__("time").monotonic() - started,
        __import__("time").thread_time() - cpu_started)

    assert [(call["topic"], call["retain"]) for call in profiled_client.calls] == \
        [(call["topic"], call["retain"]) for call in plain_client.calls]
    for profiled_call, plain_call in zip(profiled_client.calls, plain_client.calls):
        if profiled_call["topic"] == "guardian/battery/state":
            profiled_payload = json.loads(profiled_call["payload"])
            plain_payload = json.loads(plain_call["payload"])
            profiled_payload.pop("timestamp")
            plain_payload.pop("timestamp")
            assert profiled_payload == plain_payload
        else:
            assert profiled_call["payload"] == plain_call["payload"]
    assert profile["cycle_id"] == 42
    assert profile["mqtt_publish_count"] == len(profiled_client.calls)
    assert profile["mqtt_payload_bytes"] == sum(call["size"] for call in profiled_client.calls)
    assert profile["groups"]["cell_diagnostics"]["publish_count"] > \
        profile["groups"]["modules"]["publish_count"]
    assert sum(item["publish_count"] for item in profile["groups"].values()) == \
        profile["mqtt_publish_count"]
    assert sum(item["payload_bytes"] for item in profile["groups"].values()) == \
        profile["mqtt_payload_bytes"]


def test_before_analysis_has_lower_profiled_publish_count():
    client = FakeClient()
    publisher = Mqtt.__new__(Mqtt)
    publisher.prefix = "guardian"; publisher.client = client
    publisher.discovery_enabled = False
    publisher.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    publisher._cycle_profiler = None
    publisher.begin_cycle_profile(1)
    publisher.publish(
        modules(), "ok", [], DEFAULTS, {"alarm_counts": {}}, {},
        {"active": False, "last_summary": "kein Incident"}, {}, {}, {})
    profile = publisher.finish_cycle_profile(.1, .1)
    assert 0 < profile["mqtt_publish_count"] < 1000
