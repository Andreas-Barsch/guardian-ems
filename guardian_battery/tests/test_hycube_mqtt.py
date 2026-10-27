import json
from types import SimpleNamespace

import pytest

from hycube_mqtt import HycubeMqttPublisher, SENSORS
from hycube_evidence import (HycubeCollector, HycubeEvidenceWriter, HycubePolicyHistory,
                             observation, policy_observation)
from test_hycube_evidence import Opener, Response, PAYLOAD

NOW = 1792958400


class Client:
    def __init__(self):
        self.messages = []
        self.rc = 0
    def publish(self, topic, payload, retain=False):
        self.messages.append((topic, payload, retain))
        return SimpleNamespace(rc=self.rc)


def publisher(client=None, enabled=True):
    return HycubeMqttPublisher(client or Client(), "guardian/", discovery_enabled=enabled,
        interval_seconds=20, policy_interval_seconds=300, timeout_seconds=.8, clock=lambda: NOW)


def system(value=42, timestamp=NOW):
    return observation(json.dumps({**PAYLOAD, "BatteryCapacity": value}).encode(), timestamp)


def policy():
    return policy_observation(json.dumps({"normalMode": 20, "bufferMode": 20,
        "emergency": 55, "batProtection": 5}).encode(), NOW)


def test_discovery_names_ids_units_and_freshness():
    p = publisher(); p.discovery()
    configs = [json.loads(body) for topic, body, retained in p.client.messages if topic.endswith('/config')]
    assert len(configs) == 4
    assert {x['unique_id'] for x in configs} == {f'guardian_battery_{s[0]}' for s in SENSORS}
    assert all(x['name'].startswith('Hycube ') and x['unit_of_measurement'] == '%' for x in configs)
    assert all(x['availability_mode'] == 'all' and len(x['availability']) == 2 for x in configs)
    assert configs[0]['device_class'] == 'battery'
    assert [x['expire_after'] for x in configs] == [61, 901, 901, 901]
    assert all(x['device_class'] == 'battery' and x['state_class'] == 'measurement' for x in configs[1:])
    assert [x['name'] for x in configs[1:]] == [
        'Hycube SOC-Grenze Normalbetrieb / Passiv',
        'Hycube SOC-Grenze Passiv / Notstrom',
        'Hycube SOC-Grenze Notstrom / Batterieschutz',
    ]
    assert [x['unique_id'] for x in configs[1:]] == [
        'guardian_battery_hycube_boundary_normal_passive',
        'guardian_battery_hycube_boundary_passive_emergency',
        'guardian_battery_hycube_boundary_emergency_protection',
    ]
    assert [x['state_topic'] for x in configs[1:]] == [
        'guardian/battery/sensor/hycube_boundary_normal_passive/state',
        'guardian/battery/sensor/hycube_boundary_passive_emergency/state',
        'guardian/battery/sensor/hycube_boundary_emergency_protection/state',
    ]
    assert all(retained for _, _, retained in p.client.messages)
    assert p.client.messages[0][1] == p.client.messages[1][1] == 'offline'


def test_only_validated_existing_values_published_without_retained_states():
    p = publisher(); p.observe('system', system()); p.observe('policy', policy())
    states = [(topic, body, retained) for topic, body, retained in p.client.messages if topic.endswith('/state')]
    assert [float(body) for _, body, _ in states] == [42, 80, 60, 5]
    assert all(not retained for _, _, retained in states)
    assert p.client.messages[-1][1] == 'online'


@pytest.mark.parametrize('value', [None, True, '42', float('nan'), float('inf'), -1, 101])
def test_invalid_capacity_is_unavailable_not_a_fabricated_zero(value):
    p = publisher(); p.observe('system', system(value))
    assert p.client.messages == [('guardian/battery/hycube/system/availability', 'offline', True)]


@pytest.mark.parametrize('kind', ['system', 'policy'])
def test_absent_stale_or_future_observations_never_publish_state(kind):
    p = publisher(); record = system() if kind == 'system' else policy()
    key = 'received_at' if kind == 'system' else 'observed_at'
    for stamp in ['2020-01-01T00:00:00+00:00', '2100-01-01T00:00:00+00:00', '2026-10-25T00:00:00']:
        p.observe(kind, {**record, key: stamp})
    p.observe(kind, None)
    assert len(p.client.messages) == 4
    assert all(body == 'offline' and topic.endswith('/availability') for topic, body, _ in p.client.messages)


def test_invalid_policy_never_partially_publishes_boundaries():
    p = publisher(); p.observe('policy', {**policy(), 'boundary_passive_emergency': None})
    assert len(p.client.messages) == 1 and p.client.messages[0][1] == 'offline'


def test_reconnect_discovery_marks_sources_offline_until_fresh_observation():
    p = publisher(); p.observe('system', system()); p.client.messages.clear(); p.discovery()
    assert p.client.messages[0][1] == p.client.messages[1][1] == 'offline'
    assert not any(t.endswith('/state') for t, _, _ in p.client.messages)
    p.observe('system', system())
    assert p.client.messages[-1][1] == 'online'


def test_discovery_opt_out_keeps_manual_mqtt_publication_possible():
    p = publisher(enabled=False); p.discovery(); p.observe('system', system())
    assert not any(t.endswith('/config') for t, _, _ in p.client.messages)
    assert any(t.endswith('/state') for t, _, _ in p.client.messages)


def test_collector_notifies_after_persistence_without_an_extra_request(tmp_path):
    p = publisher(); opener = Opener(); writer = HycubeEvidenceWriter(tmp_path/'raw')
    collector = HycubeCollector('http://192.168.1.22', writer, opener=opener,
        policy_writer=HycubePolicyHistory(tmp_path/'policy'), clock=lambda: NOW, observer=p.observe)
    collector.collect_once()
    opener.result = Response({'normalMode':20, 'bufferMode':20, 'emergency':55, 'batProtection':5})
    collector.collect_policy_once()
    assert [request.full_url for request, _ in opener.calls] == [
        'http://192.168.1.22/data_row/', 'http://192.168.1.22/Bat/getCustomBat/']
    assert len([t for t, _, _ in p.client.messages if t.endswith('/state')]) == 4
    assert collector.status()['observations'] == collector.status()['policy_observations'] == 1


def test_mqtt_failure_does_not_fail_or_repeat_acquisition(tmp_path):
    p = publisher(); p.client.rc = 4; opener = Opener()
    collector = HycubeCollector('http://192.168.1.22', HycubeEvidenceWriter(tmp_path/'raw'),
        opener=opener, clock=lambda: NOW, observer=p.observe)
    assert collector.collect_once()['BatteryCapacity'] == 42
    assert len(opener.calls) == 1 and collector.status()['observations'] == 1


def test_loop_failure_and_shutdown_mark_both_sources_unavailable(tmp_path):
    events = []
    collector = HycubeCollector('http://192.168.1.22', HycubeEvidenceWriter(tmp_path/'raw'),
        policy_writer=HycubePolicyHistory(tmp_path/'policy'), observer=lambda *args: events.append(args))
    def fail():
        collector._stop.set()
        raise RuntimeError('synthetic failure')
    collector.collect_once = collector.collect_policy_once = fail
    collector._run()
    assert events == [('system', None), ('policy', None), ('system', None), ('policy', None)]


def test_main_reconnect_refreshes_hycube_discovery_without_source_reads():
    from test_mqtt_projection import Mqtt
    p = publisher()
    mqtt = Mqtt.__new__(Mqtt)
    mqtt.prefix = "guardian"; mqtt.client = p.client
    mqtt._ensure_derived_publish_state()
    mqtt.hycube_mqtt = p
    mqtt._on_mqtt_connect(p.client, None, None, 0)
    assert len([t for t, _, _ in p.client.messages if t.endswith('/config')]) == 4
    assert not any(t.endswith('/state') for t, _, _ in p.client.messages)
    p.client.rc = 4
    mqtt._on_mqtt_connect(p.client, None, None, 0)
    assert mqtt._derived_reconnect_epoch == 2
