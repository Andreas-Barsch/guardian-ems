import time
from unittest.mock import patch

import pytest

from collector_timing import CollectorTiming
from maintenance_mqtt import MaintenanceMqttPublisher
from mqtt_observability import MqttCycleProfiler, mqtt_group
from rs485_mqtt import Rs485MqttProjection

with patch("pathlib.Path.mkdir", return_value=None):
    from main import Mqtt


class Result:
    def __init__(self, rc=0):
        self.rc = rc


class Client:
    def __init__(self, results=None, delay=0, cpu_work=0):
        self.results = iter(results or [])
        self.delay = delay
        self.cpu_work = cpu_work
        self.calls = []

    def is_connected(self):
        return True

    def publish(self, topic, payload, retain=False):
        if self.delay:
            time.sleep(self.delay)
        value = 0
        for number in range(self.cpu_work):
            value += number
        self.calls.append((topic, payload, retain))
        return Result(next(self.results, 0))


def publisher(client):
    value = Mqtt.__new__(Mqtt)
    value.prefix = "guardian"
    value.client = client
    value.discovery_enabled = False
    value.maintenance_events = MaintenanceMqttPublisher(client, "guardian")
    value._cycle_profiler = None
    return value


def test_fixed_groups_and_return_codes_are_bounded():
    assert mqtt_group("guardian/battery/state") == "stack_battery"
    assert mqtt_group("guardian/battery/sensor/module_1_soc/state") == "modules"
    assert mqtt_group("guardian/battery/sensor/module_1_cell_1_status/state") == \
        "cell_diagnostics"
    assert mqtt_group("guardian/battery/sensor/rs485_status/state") == "rs485"
    assert mqtt_group("homeassistant/sensor/x/config") == "discovery"
    client = Client([0, 4, 15, 99])
    pub = publisher(client)
    pub.begin_cycle_profile(17)
    for suffix in ("state", "health", "soc", "current"):
        pub._publish(f"guardian/battery/sensor/{suffix}/state", "x", retain=True)
    profile = pub.finish_cycle_profile(.1, .01)
    assert profile["cycle_id"] == 17
    assert profile["return_codes"] == {
        "success_count": 1, "no_conn_count": 1,
        "queue_full_count": 1, "other_error_count": 1}
    assert profile["mqtt_publish_count"] == 4
    assert sum(value["publish_count"] for value in profile["groups"].values()) == 4
    assert sum(value["payload_bytes"] for value in profile["groups"].values()) == 4


def test_json_publish_and_accounting_preserve_payload_semantics():
    client = Client()
    pub = publisher(client)
    pub.begin_cycle_profile(3)
    wall = time.monotonic(); cpu = time.thread_time()
    pub.attributes("module_1_cell_1_status", {"value": "ä"})
    total_wall = time.monotonic() - wall
    total_cpu = time.thread_time() - cpu
    pub._cycle_profiler.guardian_finished(total_wall, total_cpu)
    profile = pub.finish_cycle_profile(total_wall, total_cpu)
    assert client.calls == [("guardian/battery/sensor/module_1_cell_1_status/attributes",
                             '{"value": "ä"}', True)]
    assert profile["mqtt_json_count"] == 1
    assert profile["mqtt_json_bytes"] == len('{"value": "ä"}'.encode())
    assert profile["mqtt_payload_bytes"] == profile["mqtt_json_bytes"]
    assert profile["groups"]["cell_diagnostics"]["publish_count"] == 1
    assert profile["mqtt_build_wall_seconds"] + profile["mqtt_json_wall_seconds"] + \
        profile["mqtt_publish_wall_seconds"] + profile["mqtt_other_wall_seconds"] == \
        pytest.approx(total_wall, abs=.002)


def test_discovery_is_not_counted_in_normal_cycle():
    client = Client()
    pub = publisher(client)
    pub.begin_cycle_profile(1)
    pub._publish("homeassistant/sensor/guardian_battery/test/config", "{}", retain=True)
    pub._cycle_profiler.guardian_finished(.001, .001)
    profile = pub.finish_cycle_profile(.001, .001)
    assert len(client.calls) == 1
    assert profile["mqtt_publish_count"] == 0
    assert sum(value["publish_count"] for value in profile["groups"].values()) == 0


def test_sleep_and_cpu_publish_have_distinct_wall_cpu_evidence():
    sleepy = publisher(Client(delay=.01))
    sleepy.begin_cycle_profile(1)
    sleepy._publish("guardian/battery/state", "x", retain=True)
    sleep_profile = sleepy.finish_cycle_profile(.02, .001)
    assert sleep_profile["mqtt_publish_wall_seconds"] >= .009
    assert sleep_profile["mqtt_publish_thread_cpu_seconds"] < .005

    busy = publisher(Client(cpu_work=100_000))
    busy.begin_cycle_profile(2)
    busy._publish("guardian/battery/state", "x", retain=True)
    busy_profile = busy.finish_cycle_profile(.02, .02)
    assert busy_profile["mqtt_publish_wall_seconds"] > 0
    assert busy_profile["mqtt_publish_thread_cpu_seconds"] > 0
    assert busy_profile["mqtt_publish_max_group"] == "stack_battery"


def test_exception_is_not_swallowed_or_retried():
    class Broken(Client):
        def publish(self, topic, payload, retain=False):
            raise OSError("broken")

    pub = publisher(Broken())
    pub.begin_cycle_profile(1)
    with pytest.raises(OSError, match="broken"):
        pub._publish("guardian/battery/state", "x", retain=True)
    assert pub._cycle_profiler.publish_count == 0


def test_completed_cycle_keeps_mqtt_profile_immutable():
    timing = CollectorTiming(10, 60)
    timing.cycle_started(100, 1)
    timing.mqtt_details({"cycle_id": 1, "mqtt_publish_count": 12})
    timing.cycle_finished(2)
    timing.cycle_started(110, 11)
    timing.mqtt_details({"cycle_id": 2, "mqtt_publish_count": 99})
    state = timing.snapshot()
    assert state["last_completed_cycle"]["mqtt_subtiming"] == {
        "cycle_id": 1, "mqtt_publish_count": 12}
    assert state["current_cycle"]["mqtt_subtiming"]["cycle_id"] == 2


def test_rs485_projection_uses_own_group_without_payload_change():
    client = Client()
    pub = publisher(client)
    projection = Rs485MqttProjection(pub, wall_clock=lambda: 110)
    latest = {2: {
        "timestamp": 100, "position": 2, "serial_string": "SERIAL-2",
        "physical_serial": "SERIAL-2", "identity_resolved": True,
        "identity_known": True, "identity_currently_confirmed": True,
        "charge_current_limit_a": 25.0, "discharge_current_limit_a": -25.0,
    }}
    pub.begin_cycle_profile(5)
    wall = time.monotonic(); cpu = time.thread_time()
    projection.publish({"state": "listening"}, latest)
    elapsed = time.monotonic() - wall; cpu_elapsed = time.thread_time() - cpu
    pub._cycle_profiler.guardian_finished(0, 0)
    pub._cycle_profiler.rs485_finished(elapsed, cpu_elapsed)
    profile = pub.finish_cycle_profile(elapsed, cpu_elapsed)
    rs485 = profile["groups"]["rs485"]
    assert rs485["publish_count"] > 0
    assert rs485["payload_bytes"] == sum(
        len(str(payload).encode()) for topic, payload, _retain in client.calls
        if not topic.startswith("homeassistant/"))
    assert profile["mqtt_publish_count"] == rs485["publish_count"]
    assert any(topic.endswith("rs485_adr_02_ccl/state")
               for topic, _payload, _retain in client.calls)
