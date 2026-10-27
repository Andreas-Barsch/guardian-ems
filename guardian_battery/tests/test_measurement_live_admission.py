"""Synthetic regressions for the installed default path, without a scheduler."""
import json
import queue
import threading
import time
import weakref
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import history_live_priority as priority
from research_budget import cancellation_scope, query_budget, ResearchQueryError
from test_research_api import environment


@pytest.fixture(autouse=True)
def clean_priority(monkeypatch):
    monkeypatch.setattr(priority, "_raw_writers", weakref.WeakSet())
    priority.collector_stopped()
    yield
    priority.collector_stopped()
    assert priority._read_waiters == 0
    assert priority._jobs.acquire(False)
    priority._jobs.release()


@pytest.mark.parametrize('endpoint,extra', [
    ('module-history', {'metric': 'soc', 'paged': 'true'}),
    ('cell-history', {'metric': 'cell_voltage', 'cell_numbers': '1', 'paged': 'true'}),
    ('coverage', {'datasets': 'soc,cell_voltage'}),
])
def test_real_measurements_wait_for_live_cycle_without_retry(tmp_path, monkeypatch, endpoint, extra):
    api, _, _ = environment(tmp_path)
    assert api.scheduler is None and api.window_backend is None
    target = '/api/research/' + endpoint + '?' + urlencode({
        'physical_serial': 'SERIAL-M4', 'from': '2026-09-11T10:00:00Z',
        'to': '2026-09-11T10:10:00Z', **extra})
    expected = api.handle('GET', target)
    assert expected.status == 200
    waiting = threading.Event()
    original_sleep = time.sleep
    def sleep(delay):
        waiting.set()
        original_sleep(delay)
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    ticks = []
    @priority.raw_priority
    def persist():
        ticks.append('raw persisted')
    def collect():
        assert waiting.wait(2)
        persist()
        priority.idle_until(time.monotonic() + 5)
    priority.cycle_started()
    thread = threading.Thread(target=collect)
    thread.start()
    try:
        actual = api.handle('GET', target)
    finally:
        thread.join(2)
    assert not thread.is_alive() and ticks == ['raw persisted']
    assert actual.status == 200, actual.body
    assert actual.body['data'] == expected.body['data']
    assert len(api.gate.audit) == 2  # baseline plus exactly one manual request


def test_mid_read_pause_resumes_without_decoding_again(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    original = json.loads
    decoded = []
    waits = []
    def loads(value, *args, **kwargs):
        result = original(value, *args, **kwargs)
        if isinstance(result, dict) and 'soc_percent' in result:
            decoded.append(result['timestamp'])
            if len(decoded) == 1:
                priority.cycle_started()
        return result
    def sleep(_):
        waits.append(True)
        priority.idle_until(time.monotonic() + 5)
    monkeypatch.setattr(json, 'loads', loads)
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    priority.cycle_started()
    priority.idle_until(time.monotonic() + 5)
    result = api.handle('GET', '/api/research/module-history?physical_serial=SERIAL-M4&metric=soc'
                        '&from=2026-09-11T10:00:00Z&to=2026-09-11T10:10:00Z')
    assert result.status == 200, result.body
    assert waits == [True] and len(decoded) == 7
    assert len(result.body['data']['points']) == 5


@pytest.mark.parametrize('occupied', [False, True])
def test_wait_cancellation_returns_capacity(monkeypatch, occupied):
    cancelled = []
    def sleep(_): cancelled.append(True)
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    priority.cycle_started()
    if occupied: assert priority._jobs.acquire(False)
    try:
        with pytest.raises(ResearchQueryError) as error:
            with cancellation_scope(lambda: bool(cancelled)), query_budget(time.monotonic() + 5):
                with priority.measurement_slot(): pytest.fail('must not read')
        assert error.value.code == 'cancelled'
    finally:
        if occupied: priority._jobs.release()


def test_wait_timeout_and_backlog_are_bounded(monkeypatch):
    now = [0.0]
    import research_budget
    clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda _: now.__setitem__(0, now[0] + 1))
    monkeypatch.setattr(priority, 'time', clock)
    monkeypatch.setattr(research_budget, 'time', clock)
    writer = type('Writer', (), {})()
    writer._queue = queue.Queue(); writer._queue.put('pending')
    priority.register_raw_writer(writer)
    priority.cycle_started(); priority.idle_until(100)
    with pytest.raises(ResearchQueryError) as error:
        with query_budget(2), priority.measurement_slot(): pytest.fail('must not read backlog')
    assert error.value.code == 'timeout' and now == [2]


def test_missing_index_still_fails_closed(tmp_path):
    api, _, _ = environment(tmp_path)
    path = tmp_path / 'cells' / '2026-09-11.jsonl'
    path.write_bytes(path.read_bytes() * 100)
    priority.cycle_started(); priority.idle_until(time.monotonic() + 5)
    result = api.handle('GET', '/api/research/module-history?physical_serial=SERIAL-M4&metric=soc'
                        '&from=2026-09-11T10:00:00Z&to=2026-09-11T10:10:00Z')
    assert result.status == 503 and result.body['error']['code'] == 'history_index_required'


def test_background_startup_yields_to_waiting_manual_read():
    from diagnostic_startup_io import StartupBudget
    stop = threading.Event()
    budget = StartupBudget(stop)
    priority.cycle_started(); priority.idle_until(time.monotonic() + 5)
    budget.checkpoint()
    entered = threading.Event()
    def read():
        with query_budget(time.monotonic() + 2), priority.measurement_slot():
            entered.set()
            stop.set()
    thread = threading.Thread(target=read); thread.start()
    deadline = time.monotonic() + 2
    while priority._read_waiters == 0 and time.monotonic() < deadline: time.sleep(.001)
    from diagnostic_startup_io import StartupIncomplete
    try:
        with pytest.raises(StartupIncomplete): budget.checkpoint()
    finally:
        stop.set(); budget.close(); thread.join(2)
    assert entered.is_set() and not thread.is_alive()
