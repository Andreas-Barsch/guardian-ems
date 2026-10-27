"""Loopback only: real Handler, real API and synthetic prepared inputs."""
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
import config_ui
from test_local_history_window_matrix import fixture
from history_live_priority import cycle_started,collector_stopped
from research_budget import check_budget


@contextmanager
def server(api,monkeypatch):
    monkeypatch.setattr(config_ui,'_get_history_api',lambda:api)
    monkeypatch.setattr(config_ui,'_HISTORY_TIMING',api.timing_state)
    class Handler(config_ui.Handler):
        def _ingress_allowed(self):return True
        def log_message(self,*args):pass
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    try:yield http.server_port
    finally:http.shutdown();http.server_close();thread.join(2)


def test_actual_http_serialization_completes_inside_one_scheduled_job(tmp_path,monkeypatch):
    api,window,target,scheduler=fixture(tmp_path,0)
    cycle_started()  # Legacy idle gating would reject encoding after query.
    try:
        with server(api,monkeypatch) as port:
            with urlopen(f'http://127.0.0.1:{port}'+target,timeout=10) as response:
                assert response.status==200
                body=json.load(response)
            assert body['data_state']['request']['kind']=='manual_http_history'
        jobs=scheduler.status()['recent']
        assert len(jobs)==1 and jobs[0]['state']=='complete'
    finally:collector_stopped()


def test_socket_disconnect_cancels_actual_api_and_releases_window(tmp_path,monkeypatch):
    api,window,target,scheduler=fixture(tmp_path,0)
    entered=threading.Event();leave=threading.Event();original=window.read
    @contextmanager
    def paused(*args,**kwargs):
        with original(*args,**kwargs) as rows:
            def stream():
                for row in rows:
                    entered.set()
                    while not leave.wait(.002):check_budget()
                    yield row
            yield stream()
    monkeypatch.setattr(window,'read',paused)
    with server(api,monkeypatch) as port:
        client=socket.create_connection(('127.0.0.1',port),timeout=5)
        client.sendall(('GET '+target+' HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n').encode())
        assert entered.wait(5)
        client.shutdown(socket.SHUT_RDWR);client.close()
        deadline=time.monotonic()+5
        while scheduler.status()['active'] and time.monotonic()<deadline:time.sleep(.005)
        leave.set()
        assert not scheduler.status()['active']
        assert scheduler.status()['recent'][-1]['state']=='cancelled'
    with window.resolver.catalog.connection() as db:
        assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0


# Deterministic transport boundaries; no network or productive inputs.
from types import SimpleNamespace
import pytest
from maintenance_api import ApiResponse
from work_scheduler import WorkScheduler, WorkCancelled


def transport(scheduler=None, callback=None, send_error=None):
    state = SimpleNamespace(disconnected=False, calls=0, sent=[], timeouts=[])
    def handle(*args, **kwargs):
        state.calls += 1
        if callback:
            callback(state)
        return ApiResponse(200, {'ok': True})
    def send(response):
        state.sent.append(response)
        if send_error:
            raise send_error()
    handler = SimpleNamespace(path='/api/history', close_connection=False,
        connection=SimpleNamespace(settimeout=state.timeouts.append),
        _history_disconnected=lambda: state.disconnected, _send_history=send)
    api = SimpleNamespace(scheduler=scheduler or WorkScheduler(), handle=handle)
    return state, handler, api


def invoke(handler, api):
    config_ui.Handler._scheduled_history_request(handler, api)
    assert not api.scheduler.status()['active']


def test_disconnect_before_admission_never_calls_api_or_sends():
    state, handler, api = transport()
    state.disconnected = True
    invoke(handler, api)
    assert state.calls == 0 and state.sent == []
    assert handler.close_connection
    assert api.scheduler.status()['recent'][-1]['state'] == 'cancelled'


@contextmanager
def occupied_read_lane(scheduler):
    entered=threading.Event();release=threading.Event()
    thread=threading.Thread(target=lambda:scheduler.execute('existing-read',
        lambda:(entered.set(),release.wait(5)),lane='read'))
    thread.start()
    try:
        assert entered.wait(2)
        yield
    finally:
        release.set();thread.join(2)
        assert not thread.is_alive()


def test_disconnect_while_queued_never_calls_api_or_sends():
    state, handler, api = transport()
    waiting = threading.Event()
    def disconnected():
        waiting.set()
        return state.disconnected
    handler._history_disconnected = disconnected
    errors = []
    def request():
        try:
            config_ui.Handler._scheduled_history_request(handler, api)
        except BaseException as exc:
            errors.append(exc)
    with occupied_read_lane(api.scheduler):
        thread = threading.Thread(target=request)
        thread.start()
        assert waiting.wait(2)
        state.disconnected = True
        thread.join(2)
        assert not thread.is_alive()
    assert errors == [] and state.calls == 0 and state.sent == []
    assert handler.close_connection


def test_disconnect_after_api_before_headers_never_sends():
    state, handler, api = transport(callback=lambda state: setattr(state, 'disconnected', True))
    invoke(handler, api)
    assert state.calls == 1 and state.sent == [] and handler.close_connection


@pytest.mark.parametrize('error', [BrokenPipeError, ConnectionResetError, TimeoutError])
def test_transfer_failure_closes_without_second_response(error):
    state, handler, api = transport(send_error=error)
    invoke(handler, api)
    assert state.calls == 1 and len(state.sent) == 1
    assert state.sent[0].status == 200 and handler.close_connection
    assert api.scheduler.status()['recent'][-1]['state'] == 'cancelled'


def test_reachable_deadline_before_admission_sends_timeout_once(monkeypatch):
    monkeypatch.setattr('research_api.QUERY_TIMEOUT_SECONDS', 0)
    state, handler, api = transport()
    invoke(handler, api)
    assert state.calls == 0 and len(state.sent) == 1
    assert state.sent[0].status == 503
    assert state.sent[0].body['error']['code'] == 'timeout'
    assert not handler.close_connection


@pytest.mark.parametrize('disconnected', [False, True])
@pytest.mark.parametrize('error', [None, BrokenPipeError, ConnectionResetError, TimeoutError])
def test_full_queue_only_replies_to_reachable_peer(disconnected, error):
    scheduler = WorkScheduler(capacity=1)
    state, handler, api = transport(scheduler=scheduler, send_error=error)
    state.disconnected = disconnected
    entered = threading.Event(); release = threading.Event()
    thread = threading.Thread(target=lambda: scheduler.execute('occupied',
        lambda: (entered.set(), release.wait(5)), lane='read'))
    thread.start()
    try:
        assert entered.wait(2)
        config_ui.Handler._scheduled_history_request(handler, api)
        assert state.calls == 0
        assert len(state.sent) == (0 if disconnected else 1)
        if state.sent:
            assert state.sent[0].status == 503
            assert state.sent[0].body['error']['code'] == 'scheduler_full'
        assert handler.close_connection == (disconnected or error is not None)
    finally:
        release.set(); thread.join(2)
    assert not scheduler.status()['active']


def test_reachable_deadline_during_queue_wait_sends_once(monkeypatch):
    monkeypatch.setattr('research_api.QUERY_TIMEOUT_SECONDS', .02)
    state, handler, api = transport()
    with occupied_read_lane(api.scheduler):
        config_ui.Handler._scheduled_history_request(handler, api)
    assert not api.scheduler.status()["active"]
    assert state.calls == 0 and len(state.sent) == 1
    assert state.sent[0].body['error']['code'] == 'timeout'
    assert not handler.close_connection


def test_deadline_after_api_before_headers_is_timeout(monkeypatch):
    # Controlled clock, without a race or a real wait.
    clock = [100.]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr('research_api.QUERY_TIMEOUT_SECONDS', 1)
    state, handler, api = transport(callback=lambda state: clock.__setitem__(0, 102.))
    invoke(handler, api)
    assert state.calls == 1 and len(state.sent) == 1
    assert state.sent[0].body['error']['code'] == 'timeout'


def test_serialization_deadline_is_not_reported_as_disconnect(monkeypatch):
    import io
    from history_timing import HistoryRequestTimingState
    def timeout(*args, **kwargs):
        raise WorkCancelled('deadline exceeded')
    monkeypatch.setattr('research_budget.check_budget', timeout)
    monkeypatch.setattr(config_ui, '_HISTORY_TIMING', HistoryRequestTimingState())
    statuses = []
    handler = SimpleNamespace(_history_disconnected=lambda: False,
        send_response=statuses.append, send_header=lambda *args: None,
        end_headers=lambda: None, wfile=io.BytesIO())
    config_ui.Handler._send_history(handler, ApiResponse(200, {'ok': True}))
    assert statuses == [503]
    assert json.loads(handler.wfile.getvalue())['error']['code'] == 'timeout'


def test_abort_unwinds_publication_pins_before_transport_returns(tmp_path):
    from test_publication_history_readers import setup
    env, preparation, index, readers = setup(tmp_path)
    def callback(state):
        with readers._published('display', [env['origin']]):
            with env['repo']._connection() as db:
                assert db.execute("SELECT count(*) FROM pin WHERE owner='local-history-request'").fetchone()[0] == 1
            state.disconnected = True
            check_budget()
    state, handler, api = transport(callback=callback)
    invoke(handler, api)
    assert state.calls == 1 and state.sent == [] and handler.close_connection
    with env['repo']._connection() as db:
        assert db.execute("SELECT count(*) FROM pin WHERE owner='local-history-request'").fetchone()[0] == 0
