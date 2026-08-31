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


def test_stale_management_entities_are_unavailable_and_not_republished_as_current():
    mqtt = FakeMqtt()
    Rs485MqttProjection(mqtt, wall_clock=lambda: 200, stale_seconds=20).publish(
        {"state": "listening"}, {2: {"timestamp": 100, "charge_enable": True}})
    availability = [(topic, payload) for topic, payload, _ in mqtt.messages
                    if "adr_02" in topic and "availability" in topic]
    assert availability and all(payload == "offline" for _, payload in availability)
