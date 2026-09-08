"""Bounded, cycle-local MQTT projection timing without payload retention."""
from __future__ import annotations

import time

from paho.mqtt.client import (MQTT_ERR_NO_CONN, MQTT_ERR_QUEUE_SIZE,
                              MQTT_ERR_SUCCESS)


GROUPS = ("stack_battery", "modules", "cell_diagnostics", "rs485", "other")
RC_SUCCESS = int(MQTT_ERR_SUCCESS)
RC_NO_CONN = int(MQTT_ERR_NO_CONN)
RC_QUEUE_FULL = int(MQTT_ERR_QUEUE_SIZE)


def mqtt_group(topic: str) -> str:
    """Map a topic to one fixed observability group."""
    if topic.startswith("homeassistant/"):
        return "discovery"
    if "rs485" in topic:
        return "rs485"
    if "_cell_" in topic or "cell_diag" in topic:
        return "cell_diagnostics"
    if "/module_" in topic:
        return "modules"
    if "/battery/state" in topic or "/battery/alarms" in topic:
        return "stack_battery"
    if "/battery/sensor/" in topic:
        return "stack_battery"
    return "other"


class MqttCycleProfiler:
    """Small main-thread DTO accumulator for exactly one collector cycle."""

    def __init__(self, cycle_id: int, client, *, wall_clock=time.monotonic,
                 cpu_clock=time.thread_time):
        self.cycle_id = int(cycle_id)
        self.client = client
        self.wall_clock = wall_clock
        self.cpu_clock = cpu_clock
        self.started_wall = wall_clock()
        self.started_cpu = cpu_clock()
        self.json_wall = 0.0
        self.json_cpu = 0.0
        self.json_count = 0
        self.json_bytes = 0
        self.publish_wall = 0.0
        self.publish_cpu = 0.0
        self.publish_count = 0
        self.payload_bytes = 0
        self.return_codes = {
            "success_count": 0, "no_conn_count": 0,
            "queue_full_count": 0, "other_error_count": 0,
        }
        # count, bytes, wall, cpu, max-wall, max-cpu,
        # json-wall, json-cpu, json-count, json-bytes
        self.groups = {name: [0, 0, 0.0, 0.0, 0.0, 0.0,
                              0.0, 0.0, 0, 0] for name in GROUPS}
        self.guardian_wall = None
        self.guardian_cpu = None
        self.guardian_json_wall = 0.0
        self.guardian_json_cpu = 0.0
        self.guardian_publish_wall = 0.0
        self.guardian_publish_cpu = 0.0
        self.rs485_wall = 0.0
        self.rs485_cpu = 0.0
        self.max_publish_group = None
        self.max_publish_wall = 0.0
        self.max_publish_cpu = 0.0
        self.connected_before = self._connected()
        self.derived_publish = None

    def derived_publish_record(self, **values):
        self.derived_publish = dict(values)

    def _connected(self):
        try:
            return bool(self.client.is_connected())
        except Exception:
            return None

    def json_record(self, group, wall, cpu, byte_count):
        if group not in self.groups:
            return
        self.json_wall += wall
        self.json_cpu += cpu
        self.json_count += 1
        self.json_bytes += byte_count
        values = self.groups[group]
        values[6] += wall
        values[7] += cpu
        values[8] += 1
        values[9] += byte_count

    def publish_record(self, group, wall, cpu, byte_count, result):
        if group not in self.groups:
            return
        self.publish_wall += wall
        self.publish_cpu += cpu
        self.publish_count += 1
        self.payload_bytes += byte_count
        values = self.groups[group]
        values[0] += 1
        values[1] += byte_count
        values[2] += wall
        values[3] += cpu
        if wall > values[4]:
            values[4] = wall
        if cpu > values[5]:
            values[5] = cpu
        if wall >= self.max_publish_wall:
            self.max_publish_wall = wall
            self.max_publish_group = group
        self.max_publish_cpu = max(self.max_publish_cpu, cpu)
        rc = getattr(result, "rc", MQTT_ERR_SUCCESS)
        rc = None if rc is None else int(rc)
        if rc in (None, RC_SUCCESS):
            key = "success_count"
        elif rc == RC_NO_CONN:
            key = "no_conn_count"
        elif rc == RC_QUEUE_FULL:
            key = "queue_full_count"
        else:
            key = "other_error_count"
        self.return_codes[key] += 1

    def guardian_finished(self, wall, cpu):
        self.guardian_wall = float(wall)
        self.guardian_cpu = float(cpu)
        self.guardian_json_wall = self.json_wall
        self.guardian_json_cpu = self.json_cpu
        self.guardian_publish_wall = self.publish_wall
        self.guardian_publish_cpu = self.publish_cpu

    def rs485_finished(self, wall, cpu):
        self.rs485_wall = float(wall)
        self.rs485_cpu = float(cpu)

    def finish(self, total_wall, total_cpu):
        guardian_wall = float(self.guardian_wall or 0.0)
        guardian_cpu = float(self.guardian_cpu or 0.0)
        build_wall = (guardian_wall - self.guardian_json_wall
                      - self.guardian_publish_wall)
        build_cpu = (guardian_cpu - self.guardian_json_cpu
                     - self.guardian_publish_cpu)
        # RS485 total is an overlapping diagnostic boundary. Its JSON and
        # publish portions already belong to the global JSON/publish totals;
        # its remaining projection work therefore stays in MQTT Other.
        accounted_wall = build_wall + self.json_wall + self.publish_wall
        accounted_cpu = build_cpu + self.json_cpu + self.publish_cpu
        result = {
            "cycle_id": self.cycle_id,
            "mqtt_projection_wall_seconds": float(total_wall),
            "mqtt_projection_thread_cpu_seconds": float(total_cpu),
            "mqtt_build_wall_seconds": build_wall,
            "mqtt_build_thread_cpu_seconds": build_cpu,
            "mqtt_json_wall_seconds": self.json_wall,
            "mqtt_json_thread_cpu_seconds": self.json_cpu,
            "mqtt_json_count": self.json_count,
            "mqtt_json_bytes": self.json_bytes,
            "mqtt_publish_wall_seconds": self.publish_wall,
            "mqtt_publish_thread_cpu_seconds": self.publish_cpu,
            "mqtt_publish_count": self.publish_count,
            "mqtt_payload_bytes": self.payload_bytes,
            "mqtt_rs485_wall_seconds": self.rs485_wall,
            "mqtt_rs485_thread_cpu_seconds": self.rs485_cpu,
            "mqtt_other_wall_seconds": float(total_wall) - accounted_wall,
            "mqtt_other_thread_cpu_seconds": float(total_cpu) - accounted_cpu,
            "mqtt_publish_max_wall_seconds": self.max_publish_wall,
            "mqtt_publish_max_thread_cpu_seconds": self.max_publish_cpu,
            "mqtt_publish_max_group": self.max_publish_group,
            "return_codes": dict(self.return_codes),
            "groups": {name: {
                "publish_count": values[0], "payload_bytes": values[1],
                "publish_wall_seconds": values[2],
                "publish_thread_cpu_seconds": values[3],
                "max_publish_wall_seconds": values[4],
                "max_publish_thread_cpu_seconds": values[5],
                "json_wall_seconds": values[6],
                "json_thread_cpu_seconds": values[7],
                "json_count": values[8], "json_bytes": values[9],
            } for name, values in self.groups.items()},
            "connected_before": self.connected_before,
            "connected_after": self._connected(),
            "queue_evidence": "unavailable_private_paho_state",
        }
        if self.derived_publish is not None:
            result.update({
                "derived_publish_performed": self.derived_publish["performed"],
                "derived_publish_skipped": self.derived_publish["skipped"],
                "derived_publish_generation": self.derived_publish["generation"],
                "last_successfully_published_generation": self.derived_publish["last_successful"],
                "derived_publish_performed_count": self.derived_publish["performed_count"],
                "derived_publish_skipped_count": self.derived_publish["skipped_count"],
                "derived_publish_failure_count": self.derived_publish["failure_count"],
                "derived_publish_invalidated_by_reconnect": self.derived_publish[
                    "invalidated_by_reconnect"],
            })
        return result
