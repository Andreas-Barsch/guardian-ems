"""Compact Home Assistant MQTT projection for passive RS485 observations."""
from __future__ import annotations

import json
import time


class Rs485MqttProjection:
    def __init__(self, mqtt_publisher, *, stale_seconds=120, wall_clock=time.time):
        self.mqtt = mqtt_publisher
        self.stale_seconds = int(stale_seconds)
        self.wall_clock = wall_clock
        self._discovered = set()

    def _discovery(self, key, name, unit=None, device_class=None, icon=None):
        if not self.mqtt.discovery_enabled or key in self._discovered:
            return
        payload = {
            "name": name, "unique_id": f"guardian_battery_{key}",
            "state_topic": f"{self.mqtt.prefix}/battery/sensor/{key}/state",
            "json_attributes_topic": f"{self.mqtt.prefix}/battery/sensor/{key}/attributes",
            "availability_topic": f"{self.mqtt.prefix}/battery/{key}/availability",
            "device": {"identifiers": ["guardian_battery"], "name": "Guardian Battery",
                       "manufacturer": "Guardian EMS"},
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if icon:
            payload["icon"] = icon
        self.mqtt._publish(f"homeassistant/sensor/guardian_battery/{key}/config",
                           json.dumps(payload, separators=(",", ":")), retain=True)
        self._discovered.add(key)

    def publish(self, status: dict, latest: dict[int, dict], writer_status=None):
        self._discovery("rs485_status", "Guardian Battery RS485 Status", icon="mdi:serial-port")
        self.mqtt._publish(f"{self.mqtt.prefix}/battery/rs485_status/availability",
                           "online", retain=True)
        self.mqtt.state("rs485_status", status.get("state", "unavailable"))
        compact = {key: status.get(key) for key in (
            "resolved_port", "baudrate", "last_valid_frame_at", "last_0x92_at",
            "frames_total", "valid_frames", "checksum_errors", "frame_errors", "last_error")}
        if writer_status:
            compact.update({"history_queue_depth": writer_status.get("queue_depth"),
                            "history_dropped_records": writer_status.get("dropped_records")})
        self.mqtt.attributes("rs485_status", compact)
        now = self.wall_clock()
        definitions = (
            ("ccl", "RS485 Charge Current Limit", "charge_current_limit_a", "A", "current"),
            ("dcl", "RS485 Discharge Current Limit", "discharge_current_limit_a", "A", "current"),
            ("cvl", "RS485 Charge Voltage Limit", "charge_voltage_limit_v", "V", "voltage"),
            ("dvl", "RS485 Discharge Voltage Limit", "discharge_voltage_limit_v", "V", "voltage"),
            ("charge_enable", "RS485 Charge Enable", "charge_enable", None, None),
            ("discharge_enable", "RS485 Discharge Enable", "discharge_enable", None, None),
            ("charge_immediately_1", "RS485 Charge Immediately 1", "charge_immediately_1", None, None),
            ("charge_immediately_2", "RS485 Charge Immediately 2", "charge_immediately_2", None, None),
            ("full_charge_request", "RS485 Full Charge Request", "full_charge_request", None, None),
            ("last_update", "RS485 Last Update", "timestamp", None, "timestamp"),
        )
        for adr, values in sorted(latest.items()):
            stale = now - float(values.get("timestamp", 0)) > self.stale_seconds
            for suffix, label, field, unit, device_class in definitions:
                key = f"rs485_adr_{adr:02x}_{suffix}"
                self._discovery(key, f"Guardian Battery ADR {adr:02X} {label}", unit, device_class)
                self.mqtt._publish(f"{self.mqtt.prefix}/battery/{key}/availability",
                                   "offline" if stale else "online", retain=True)
                if not stale and field in values:
                    value = values[field]
                    if isinstance(value, bool):
                        value = "ENABLED" if value else "STOP REQUEST"
                    elif field == "timestamp":
                        value = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))
                    self.mqtt.state(key, value)
                    self.mqtt.attributes(key, {
                        "adr": f"{adr:02X}", "identity_resolved": False,
                        "sample_age_seconds": round(max(0, now - values["timestamp"]), 1),
                        "evidence_level": "observation", "causality": "not_determined",
                    })
