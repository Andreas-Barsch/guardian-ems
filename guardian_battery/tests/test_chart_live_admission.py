"""Synthetic checks for the installed UI path; no hardware or productive data."""
import io
import json
import threading
import time
from types import SimpleNamespace
import pytest
import history_live_priority as priority
from history_api import HistoryApi
from maintenance_api import ApiResponse
from test_measurement_live_admission import clean_priority


def api_for(callback):
    api=HistoryApi.__new__(HistoryApi)
    api.scheduler=None;api.timing_state=None;api._job_gate=threading.BoundedSemaphore(1)
    api._handle=callback
    return api


def test_chart_waits_for_real_startup_lease_and_live_cycle_without_retry():
    from diagnostic_startup_io import StartupBudget, StartupIncomplete
    stop=threading.Event();budget=StartupBudget(stop)
    priority.cycle_started();priority.idle_until(time.monotonic()+5)
    budget.checkpoint();calls=[];results=[]
    def read(*_):
        assert not budget.lease
        calls.append('read');stop.set()
        return ApiResponse(200,{'points':[27,5,29]})
    api=api_for(read)
    worker=threading.Thread(target=lambda:results.append(api.handle('GET','/test',deadline=time.monotonic()+2)))
    worker.start()
    until=time.monotonic()+2
    while not priority._read_waiters and time.monotonic()<until:time.sleep(.001)
    assert priority._read_waiters==1 and calls==[]
    # Existing startup checkpoint voluntarily yields its lease. No cancellation or reset.
    try:
        with pytest.raises(StartupIncomplete):budget.checkpoint()
    finally:
        stop.set();budget.close();worker.join(2)
    assert not worker.is_alive() and calls==['read'] and results[0].status==200


@pytest.mark.parametrize('cancel', [True,False])
def test_chart_wait_aborts_or_times_out_and_releases_its_capacity(monkeypatch,cancel):
    now=[0.];stop=[]
    def sleep(_):now[0]+=1;stop.append(True)
    clock=SimpleNamespace(monotonic=lambda:now[0],sleep=sleep)
    monkeypatch.setattr(priority,'time',clock)
    import research_budget
    monkeypatch.setattr(research_budget,'time',clock)
    assert priority._jobs.acquire(False)
    api=api_for(lambda *_:pytest.fail('read must not start'))
    try:
        response=api.handle('GET','/test',cancelled=lambda:cancel and bool(stop),deadline=2)
        assert response.status==503
        assert response.body['error']['code']==('cancelled' if cancel else 'timeout')
        assert api._job_gate.acquire(False);api._job_gate.release()
    finally:priority._jobs.release()


def test_serialization_waits_without_restarting_query_and_uses_same_deadline(monkeypatch):
    import config_ui
    from history_timing import HistoryRequestTimingState
    state=HistoryRequestTimingState();state.begin({'view':'single'})
    monkeypatch.setattr(config_ui,'_HISTORY_TIMING',state)
    statuses=[];handler=SimpleNamespace(_history_disconnected=lambda:False,
        send_response=statuses.append,send_header=lambda *_:None,end_headers=lambda:None,wfile=io.BytesIO())
    assert priority._jobs.acquire(False)
    original_sleep=time.sleep;released=[]
    def release(delay):
        if not released:priority._jobs.release();released.append(True)
        original_sleep(delay)
    monkeypatch.setattr(priority,'time',SimpleNamespace(monotonic=time.monotonic,sleep=release))
    config_ui.Handler._send_history(handler,ApiResponse(200,{'points':[27,5,29]}),deadline=time.monotonic()+2)
    assert released==[True] and statuses==[200]
    assert json.loads(handler.wfile.getvalue())=={'points':[27,5,29]}
    statuses.clear();handler.wfile=io.BytesIO()
    config_ui.Handler._send_history(handler,ApiResponse(200,{'points':[1]}),deadline=time.monotonic()-1)
    assert statuses==[503] and json.loads(handler.wfile.getvalue())['error']['code']=='timeout'
