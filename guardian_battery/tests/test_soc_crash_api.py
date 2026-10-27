from datetime import datetime, timezone
from pathlib import Path

import pytest

from research_api import GuardianResearchApi, ResearchPaths
from soc_crash_events import SocCrashEventStore
from test_soc_crash_events import event_at, historical_equal_values, initial_tie_event


def api_at(tmp_path):
    paths = ResearchPaths(tmp_path / 'cells', tmp_path / 'positions',
        tmp_path / 'maintenance', tmp_path / 'phase', tmp_path / 'daily',
        soc_crash_events=tmp_path / 'soc-events.jsonl')
    return GuardianResearchApi(paths)


def get(api, path):
    return api.handle('GET', '/api/research/' + path)


@pytest.mark.parametrize('values', [historical_equal_values(), initial_tie_event().to_dict()])
def test_historical_and_null_reference_events_forwarded_unchanged(tmp_path, values):
    import json
    api = api_at(tmp_path)
    raw = (json.dumps(values) + '\n').encode()
    api.paths.soc_crash_events.write_bytes(raw)
    listed = get(api, 'soc-crash-events?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z')
    assert listed.status == 200
    assert listed.body['data']['events'] == [values]
    single = get(api, 'soc-crash-events/' + values['event_id'])
    assert single.status == 200 and single.body['data']['event'] == values
    assert api.paths.soc_crash_events.read_bytes() == raw


def test_persisted_events_only_sorted_and_single_lookup_without_history(tmp_path, monkeypatch):
    api = api_at(tmp_path)
    store = SocCrashEventStore(api.paths.soc_crash_events)
    first, second = event_at(), event_at(100)
    store.append(second); store.append(first)
    real_open = Path.open
    opened = []
    def guarded_open(path, *args, **kwargs):
        opened.append(path)
        assert path == api.paths.soc_crash_events, 'event request touched another source'
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded_open)
    response = get(api, 'soc-crash-events?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z&max_records=1')
    assert response.status == 200 and response.body['truncated']
    assert response.body['data']['events'] == [first.to_dict()]
    single = get(api, 'soc-crash-events/' + second.event_id)
    assert single.status == 200 and single.body['data']['event'] == second.to_dict()
    assert get(api, 'soc-crash-events/SCS-' + '0' * 64).status == 404
    assert opened and not api.paths.cell_history.exists()
    assert get(GuardianResearchApi(api.paths), 'soc-crash-events/' + first.event_id).status == 200


@pytest.mark.parametrize('query,status', [
    ('', 400), ('?from=1970-01-01T00:00:00Z', 400),
    ('?from=bad&to=bad', 400),
    ('?from=1970-01-02T00:00:00Z&to=1970-01-01T00:00:00Z', 400),
    ('?from=1970-01-01T00:00:00Z&to=1970-03-01T00:00:00Z', 413),
    ('?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z&max_records=0', 400),
    ('?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z&max_records=501', 400),
    ('?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z&max_records=bad', 400),
    ('?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z&unexpected=1', 400),
])
def test_invalid_limits_rejected_before_store_read(tmp_path, monkeypatch, query, status):
    api = api_at(tmp_path)
    monkeypatch.setattr(api.event_store, '_refresh', lambda: pytest.fail('invalid query touched store'))
    assert get(api, 'soc-crash-events' + query).status == status


def test_missing_event_file_is_empty_and_not_created(tmp_path):
    api = api_at(tmp_path)
    result = get(api, 'soc-crash-events?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z')
    assert result.status == 200 and result.body['data']['events'] == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('endpoint', ['soc-crash-events', 'soc-crash-events/SCS-' + 'a' * 64])
def test_event_api_rejects_writes(tmp_path, method, endpoint):
    response = api_at(tmp_path).handle(method, '/api/research/' + endpoint)
    assert response.status == 405 and response.headers['Allow'] == 'GET'
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('endpoint', ['events/soc-crashes', 'evidence-package', 'evidence-core'])
def test_old_soc_endpoints_are_unreachable_without_scanning(tmp_path, monkeypatch, endpoint):
    api = api_at(tmp_path)
    monkeypatch.setattr(api, '_refresh_identity', lambda: pytest.fail('legacy request read history'))
    monkeypatch.setattr(api, '_soc_crashes', lambda *_: pytest.fail('legacy detector invoked'))
    monkeypatch.setattr(api, '_package', lambda *_: pytest.fail('package invoked'))
    assert get(api, endpoint).status == 404


@pytest.mark.parametrize('method', ['PATCH', 'PUT', 'DELETE'])
def test_http_handler_rejects_authorized_research_writes(method):
    from config_ui import Handler
    handler = object.__new__(Handler)
    handler.path = '/api/research/soc-crash-events'
    handler._research_machine_allowed = lambda: True
    handler._ingress_allowed = lambda: False
    sent = []
    handler._send = lambda *args, **kwargs: sent.append((args, kwargs))
    getattr(handler, 'do_' + method)()
    assert sent[0][0][0] == 405
    assert sent[0][1]['headers']['Allow'] == 'GET'
