"""Synthetic local validation; no installation, hardware or productive data."""
import json
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
import pytest
from manual_history_page import ManualHistoryPages
from research_budget import ResearchQueryError


def test_boundaries_empty_pages_and_inner_cursor():
    pager = ManualHistoryPages(b"test")
    query = {"from":"2026-01-01T00:00:00Z", "to":"2026-01-01T02:00:00Z"}
    calls = []
    def read(page):
        calls.append(page)
        return {"next_cursor":"points" if len(calls)==1 else None}
    a=pager.run("module-history",query,read)
    b=pager.run("module-history",{**query,"cursor":a["next_cursor"]},read)
    c=pager.run("module-history",{**query,"cursor":b["next_cursor"]},read)
    assert calls[0]["from"] == calls[1]["from"]
    assert calls[1]["cursor"] == "points"
    assert datetime.fromisoformat(calls[2]["from"]) > datetime.fromisoformat(calls[1]["to"])
    assert c["next_cursor"] is None
    assert c["pagination"]["coverage_scope"] == "this_page_only"
    with pytest.raises(ResearchQueryError,match="cursor"):
        pager.run("module-history",{**query,"metric":"changed","cursor":a["next_cursor"]},read)
    with pytest.raises(ResearchQueryError):
        pager.run("module-history",{**query,"cursor":"hp1.bad"},read)


def test_long_range_preserves_auto_and_bounds_empty_read():
    query={"from":"2026-01-01T00:00:00Z","to":"2026-04-01T00:00:00Z"}
    calls=[]
    result=ManualHistoryPages().run("module-history",query,lambda p:calls.append(p) or {})
    assert len(calls)==1 and calls[0]["resolution"]=="display"
    assert (datetime.fromisoformat(calls[0]["to"])-datetime.fromisoformat(calls[0]["from"])).total_seconds()==3600
    assert result["next_cursor"] and result["truncated"]


def test_research_real_page_abort_retry_metadata(tmp_path):
    from test_research_api import environment
    api,_,_=environment(tmp_path)
    query={"from":"2026-09-11T10:00:00Z","to":"2026-09-12T10:00:00Z",
           "physical_serial":"SERIAL-M4","metric":"soc","paged":"true"}
    target="/api/research/module-history?"+urlencode(query)
    assert api.handle("GET",target,cancelled=lambda:True).body["error"]["code"]=="cancelled"
    a=api.handle("GET",target);b=api.handle("GET",target)
    assert a.status==b.status==200,(a.body,b.body)
    assert a.body["data"]["points"]==b.body["data"]["points"]
    point=a.body["data"]["points"][0]
    assert point["received_at"] is None and point["bms_protection"] is None
    assert a.body["data"]["coverage"]["missing_intervals"]
    query["cursor"]=a.body["next_cursor"]
    c=api.handle("GET","/api/research/module-history?"+urlencode(query))
    assert c.status==200 and not c.body["data"]["points"] and c.body["next_cursor"]


def test_six_module_repeated_manual_reads_during_simulated_collection(tmp_path):
    from test_local_history_window_matrix import fixture
    from research_api import GuardianResearchApi,ResearchPaths
    from research_identity import ResearchIdentityResolver
    from test_research_api import snapshot
    from test_local_history_window_matrix import iso
    api,window,target,scheduler=fixture(tmp_path,29)
    paths=ResearchPaths(tmp_path/'unused',tmp_path/'positions',tmp_path/'maintenance',tmp_path/'canonical',tmp_path/'daily')
    research=GuardianResearchApi(paths,window_backend=window,scheduler=scheduler)
    research.identity_backend=ResearchIdentityResolver([snapshot(iso(1799999900),{i:f'S{i}' for i in range(1,7)})])
    stop=threading.Event();ready=threading.Event();ticks=[];samples=[]
    def collect():
        with scheduler.select():
            while not stop.is_set():
                with scheduler.raw_section():
                    ticks.append(time.monotonic())
                    samples.extend((len(ticks),m,50.0) for m in range(1,7))
                    ready.set()
                stop.wait(.002)
    thread=threading.Thread(target=collect);thread.start();assert ready.wait(2)
    measured=[]
    try:
        for repeat in range(3):
            start=time.perf_counter();response=api.handle('GET',target+'&paged=true')
            assert response.status==200,response.body
            assert response.body['pagination']['next_cursor']
            measured.append({'route':'UI','seconds':time.perf_counter()-start,**window.metrics})
            for module in range(1,7):
                q={'from':iso(1800000000),'to':iso(1800000000+29*86400),'physical_serial':f'S{module}','metric':'soc','paged':'true','resolution':'full'}
                rt='/api/research/module-history?'+urlencode(q)
                start=time.perf_counter();res=research.handle('GET',rt)
                assert res.status==200,res.body
                assert res.body['next_cursor']
                measured.append({'route':'Research','seconds':time.perf_counter()-start,**window.metrics})
        assert api.handle('GET',target+'&paged=true',cancelled=lambda:True).body['error']['code']=='cancelled'
        assert api.handle('GET',target+'&paged=true').status==200
    finally:
        stop.set();thread.join(2)
    assert len(ticks)>1 and len(samples)==6*len(ticks)
    print('MANUAL_HISTORY_MEASUREMENTS '+json.dumps({'requests':measured,'simulated_tick_seconds':.002,'ticks':len(ticks),'six_module_samples':len(samples),'max_tick_deviation_seconds':max(b-a-.002 for a,b in zip(ticks,ticks[1:]))}))


def test_poll_defaults_and_bounded_ui_continuation():
    from config_ui import DEFAULTS
    from pathlib import Path
    assert DEFAULTS['poll_interval_seconds']==DEFAULTS['hycube_interval_seconds']==20
    assert DEFAULTS['cell_diagnostics_interval_seconds']==60
    source=(Path(__file__).parents[1]/'app'/'history_ui.py').read_text()
    assert "historyNext.onclick=load" in source and "pageParams.set('paged','true')" in source
    assert "await historyPause(controller.signal)" in source
    assert "Laden abbrechen" in source and "Laden fortsetzen" in source
