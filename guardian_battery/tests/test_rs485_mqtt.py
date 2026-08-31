import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from rs485_mqtt import Rs485MqttProjection


class FakeMqtt:
    prefix = "guardian"
    discovery_enabled = True
    def __init__(self): self.messages = []
    def _publish(self, topic, payload, retain): self.messages.append((topic, payload, retain))
    def state(self, name, value): self.messages.append((name + "/state", value, True))
    def attributes(self, name, value): self.messages.append((name + "/attributes", value, True))


def test_compact_projection_has_no_raw_and_preserves_dcl_sign_and_bool_semantics():
    mqtt = FakeMqtt()
    projection = Rs485MqttProjection(mqtt, wall_clock=lambda: 110, stale_seconds=20)
    projection.publish({"state": "listening", "frames_total": 4}, {2: {
        "timestamp": 100, "charge_current_limit_a": 0.0,
        "discharge_current_limit_a": -25.0, "charge_enable": False,
        "discharge_enable": True, "charge_voltage_limit_v": 53.0,
        "discharge_voltage_limit_v": 46.0, "raw_frame": b"x" * 10000,
    }})
    serialised = repr(mqtt.messages)
    assert "raw_frame" not in serialised
    assert "-25.0" in serialised
    assert "STOP REQUEST" in serialised and "ENABLED" in serialised
    assert max(len(str(payload).encode()) for _, payload, _ in mqtt.messages) < 4096


def test_discovery_topics_match_guardian_state_and_attribute_publishers():
    mqtt = FakeMqtt()
    Rs485MqttProjection(mqtt, wall_clock=lambda: 110).publish(
        {"state": "listening"}, {2: {"timestamp": 100, "charge_enable": True}})
    discovery = next(json.loads(payload) for topic, payload, _ in mqtt.messages
                     if topic.endswith("rs485_adr_02_charge_enable/config"))
    assert discovery["state_topic"] == "guardian/battery/sensor/rs485_adr_02_charge_enable/state"
    assert discovery["json_attributes_topic"] == \
        "guardian/battery/sensor/rs485_adr_02_charge_enable/attributes"
    assert discovery["availability_topic"] == \
        "guardian/battery/rs485_adr_02_charge_enable/availability"
    assert ("rs485_adr_02_charge_enable/state", "ENABLED", True) in mqtt.messages


def management(timestamp=100):
    return {2: {
        "timestamp": timestamp, "charge_current_limit_a": -25.0,
        "discharge_current_limit_a": -30.0, "charge_enable": True,
        "discharge_enable": False,
    }}


def attributes(mqtt, suffix="ccl"):
    return next(payload for topic, payload, _ in mqtt.messages
                if topic == f"rs485_adr_02_{suffix}/attributes")


def test_realistic_management_poll_gaps_remain_current_with_default_threshold():
    for age in (30, 119, 286, 333):
        mqtt = FakeMqtt()
        Rs485MqttProjection(mqtt, wall_clock=lambda age=age: 100 + age).publish(
            {"state": "listening"}, management())
        assert attributes(mqtt)["management_freshness"] == "current"
        assert attributes(mqtt)["sample_age_seconds"] == age


def test_stale_management_keeps_values_enable_semantics_and_availability():
    mqtt = FakeMqtt()
    Rs485MqttProjection(mqtt, wall_clock=lambda: 701).publish(
        {"state": "listening"}, management())
    availability = [(topic, payload) for topic, payload, _ in mqtt.messages
                    if "adr_02" in topic and "availability" in topic]
    assert availability and all(payload == "online" for _, payload in availability)
    assert ("rs485_adr_02_ccl/state", -25.0, True) in mqtt.messages
    assert ("rs485_adr_02_dcl/state", -30.0, True) in mqtt.messages
    assert ("rs485_adr_02_charge_enable/state", "ENABLED", True) in mqtt.messages
    assert ("rs485_adr_02_discharge_enable/state", "STOP REQUEST", True) in mqtt.messages
    assert attributes(mqtt)["management_freshness"] == "stale"


def test_bus_unavailable_makes_management_unavailable_without_inventing_values():
    mqtt = FakeMqtt()
    Rs485MqttProjection(mqtt, wall_clock=lambda: 110).publish(
        {"state": "reconnecting"}, management())
    availability = [(topic, payload) for topic, payload, _ in mqtt.messages
                    if "adr_02" in topic and "availability" in topic]
    assert availability and all(payload == "offline" for _, payload in availability)
    assert ("rs485_adr_02_ccl/state", -25.0, True) in mqtt.messages

    never_seen = FakeMqtt()
    Rs485MqttProjection(never_seen, wall_clock=lambda: 110).publish(
        {"state": "listening"}, {})
    assert not any("rs485_adr_" in topic for topic, _, _ in never_seen.messages)


def test_new_management_after_stale_returns_to_current():
    mqtt = FakeMqtt()
    projection = Rs485MqttProjection(mqtt, wall_clock=lambda: 701)
    projection.publish({"state": "listening"}, management())
    assert attributes(mqtt)["management_freshness"] == "stale"
    mqtt.messages.clear()
    projection.wall_clock = lambda: 705
    projection.publish({"state": "listening"}, management(timestamp=700))
    assert attributes(mqtt)["management_freshness"] == "current"
