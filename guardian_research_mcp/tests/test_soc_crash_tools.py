"""Exercise the exact registered handlers without a server or follow-up calls."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from errors import GatewayError
from gateway import QueryGate, ServiceState
from tools import register_tools


class Registry:
    def __init__(self):
        self.tools = {}

    def tool(self, *, annotations):
        def register(function):
            self.tools[function.__name__] = (function, annotations)
            return function
        return register


class Client:
    settings = SimpleNamespace(guardian_timeout_seconds=10)

    def __init__(self, payload, failure=None):
        self.payload, self.failure, self.calls = payload, failure, []

    async def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if self.failure:
            raise self.failure
        return self.payload, 100


def registered(client):
    registry = Registry()
    register_tools(registry, client, QueryGate(), ServiceState())
    return registry.tools


def test_event_tools_forward_one_page_without_follow_up_or_interpretation():
    payload = {'data': {'events': [{'causality': 'not_determined'}]},
               'truncated': True, 'next_cursor': 'not-followed'}
    client = Client(payload); tools = registered(client)
    assert 'find_soc_crashes' not in tools and 'build_evidence_package' not in tools
    assert len(tools) == 15
    function, annotation = tools['list_soc_crash_events']
    assert annotation.read_only_hint and not annotation.destructive_hint
    result = asyncio.run(function('2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z', 7))
    assert result is payload
    assert client.calls == [('soc-crash-events', {
        'from': '2026-09-01T00:00:00Z', 'to': '2026-09-02T00:00:00Z', 'max_records': 7})]
    client.calls.clear()
    function, annotation = tools['get_soc_crash_event']
    assert annotation.read_only_hint and not annotation.destructive_hint
    assert asyncio.run(function('SCS-' + 'a' * 64)) is payload
    assert client.calls == [('soc-crash-events/SCS-' + 'a' * 64, {})]


@pytest.mark.parametrize('event_id', ['..', '../status', 'id?x=y', 'unknown', 'SCS-' + 'G' * 64])
def test_invalid_event_id_never_changes_endpoint(event_id):
    client = Client({}); function, _ = registered(client)['get_soc_crash_event']
    with pytest.raises(Exception):
        asyncio.run(function(event_id))
    assert client.calls == []


def test_event_error_has_no_package_or_history_fallback():
    client = Client({}, GatewayError('not_found', ''))
    function, _ = registered(client)['get_soc_crash_event']
    with pytest.raises(Exception):
        asyncio.run(function('SCS-' + 'a' * 64))
    assert len(client.calls) == 1


@pytest.mark.parametrize('event', [
    {'event_id': 'SCS-' + 'a' * 64, 'observed_drop_pp': 5, 'x_pp': 5,
     'reference_module': 1, 'detector_version': 'guardian_soc_crash_simple_v1'},
    {'event_id': 'SCS-' + 'b' * 64, 'observed_drop_pp': 5.1, 'x_pp': 5,
     'reference_module': None, 'reference_physical_serial': None},
])
def test_historical_and_null_reference_payloads_are_not_reinterpreted(event):
    for tool, payload, args in [
        ('list_soc_crash_events', {'data': {'events': [event]}},
         ('1970-01-01T00:00:00Z', '1970-01-02T00:00:00Z')),
        ('get_soc_crash_event', {'data': {'event': event}}, (event['event_id'],)),
    ]:
        client = Client(payload)
        function, _ = registered(client)[tool]
        assert asyncio.run(function(*args)) is payload
        assert len(client.calls) == 1
