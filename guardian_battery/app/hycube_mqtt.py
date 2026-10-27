"""Publish existing live Hycube observations; never fetch or replay history."""
import json
import math
import time
from datetime import datetime

from version import GUARDIAN_VERSION

SENSORS = (
    ("hycube_battery_capacity", "Hycube Battery Capacity SOC", "system", "BatteryCapacity"),
    ("hycube_boundary_normal_passive", "Hycube SOC-Grenze Normalbetrieb / Passiv", "policy", "boundary_normal_passive"),
    ("hycube_boundary_passive_emergency", "Hycube SOC-Grenze Passiv / Notstrom", "policy", "boundary_passive_emergency"),
    ("hycube_boundary_emergency_protection", "Hycube SOC-Grenze Notstrom / Batterieschutz", "policy", "boundary_emergency_protection"),
)


class HycubeMqttPublisher:
    def __init__(self, client, prefix, *, discovery_enabled, interval_seconds,
                 policy_interval_seconds, timeout_seconds, clock=time.time):
        self.client, self.prefix = client, prefix.rstrip("/")
        self.discovery_enabled, self.clock = discovery_enabled, clock
        # Freshness only: three configured acquisition periods plus HTTP budget.
        self.expiry = {kind: max(1, math.ceil(3 * interval + timeout_seconds))
                       for kind, interval in (("system", interval_seconds),
                                              ("policy", policy_interval_seconds))}

    def _publish(self, topic, payload, *, retain=False):
        result = self.client.publish(topic, payload, retain=retain)
        if getattr(result, "rc", 0) != 0:
            raise RuntimeError("Hycube MQTT publish was not queued")

    def availability(self, kind, available):
        self._publish(f"{self.prefix}/battery/hycube/{kind}/availability",
                      "online" if available else "offline", retain=True)

    def discovery(self):
        # Restart/reconnect cannot make a retained old observation current.
        for kind in self.expiry:
            self.availability(kind, False)
        if not self.discovery_enabled:
            return
        for key, name, kind, field in SENSORS:
            payload = {"name": name, "unique_id": f"guardian_battery_{key}",
                "state_topic": f"{self.prefix}/battery/sensor/{key}/state",
                "unit_of_measurement": "%", "state_class": "measurement",
                "icon": "mdi:battery" if kind == "system" else "mdi:chart-line",
                "expire_after": self.expiry[kind], "availability_mode": "all",
                "availability": [
                    {"topic": f"{self.prefix}/battery/availability"},
                    {"topic": f"{self.prefix}/battery/hycube/{kind}/availability"}],
                "device": {"identifiers": ["guardian_battery"],
                           "name": "Guardian Battery", "sw_version": GUARDIAN_VERSION}}
            # Explicitly approved: SOC thresholds share the HA battery grouping.
            payload["device_class"] = "battery"
            self._publish(f"homeassistant/sensor/guardian_battery/{key}/config",
                          json.dumps(payload), retain=True)

    def observe(self, kind, record):
        sensors = [s for s in SENSORS if s[2] == kind]
        if kind not in self.expiry:
            raise ValueError("unknown Hycube source")
        try:
            if record is None or record.get("parse_quality") == "invalid":
                raise ValueError("missing or invalid observation")
            if kind == "policy" and record.get("parse_quality") != "complete":
                raise ValueError("incomplete policy")
            stamp = datetime.fromisoformat(record["received_at" if kind == "system" else "observed_at"])
            if stamp.tzinfo is None or not 0 <= self.clock() - stamp.timestamp() <= self.expiry[kind]:
                raise ValueError("stale observation")
            values = [record[field] for _, _, _, field in sensors]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                   not math.isfinite(v) or not 0 <= v <= 100 for v in values):
                raise ValueError("invalid percentage")
        except (ValueError, TypeError, KeyError):
            self.availability(kind, False)
            return
        for (key, _, _, _), value in zip(sensors, values):
            # No retained state: broker/HA restart must wait for fresh acquisition.
            self._publish(f"{self.prefix}/battery/sensor/{key}/state", str(value))
        self.availability(kind, True)
