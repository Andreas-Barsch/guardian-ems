"""Actual API routes, synthetic immutable inputs; never HA/runtime data."""
from datetime import datetime,timezone
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from history_api import HistoryApi
from history_series import CellHistorySeries
from hycube_evidence import HycubeBatteryCapacitySeries
from indexed_history_window import IndexedHistoryWindow
from source_catalog import SourceCatalog
from test_source_catalog import registration,index_all,write
from test_indexed_history_readers import hycube
from test_research_api import snapshot
from research_identity import ResearchIdentityResolver
from read_metadata_index import PreparedMetadata
from projection_repository import ProjectionRepository
from event_overlay import EventOverlayAdapter
from work_scheduler import WorkScheduler
from history_timing import HistoryRequestTimingState


def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()


def fixture(tmp_path,older_days):
    t=1800000000;catalog=SourceCatalog(tmp_path/'cells.sqlite')
    for day in range(older_days+1):
        rows=[{'schema_version':1,'timestamp':t-day*86400+offset,'module':module,
            'module_serial':f'S{module}','soc_percent':50+offset/1000,'current_a':-1,
            'voltages_mv':[3300,3301],'temperatures_c':[20,21]}
            for offset in (0,300,86399) for module in range(1,7)]
        path=write(tmp_path/f'cells-{day}.jsonl',rows)
        catalog.register(**registration(path,source=str(day)));index_all(catalog,str(day))
    hc=SourceCatalog(tmp_path/'hycube.sqlite');path=write(tmp_path/'hycube.jsonl',[hycube(t),hycube(t+86399)])
    hc.register(**registration(path,context={'record_format':'hycube_system_observation'}));index_all(hc)
    metadata=PreparedMetadata(ProjectionRepository(tmp_path/'meta.sqlite'),'synthetic');metadata.initialize()
    metadata.prepare_identity(ResearchIdentityResolver([snapshot(iso(t-100),{i:f'S{i}' for i in range(1,7)})]));metadata.prepare_timeline([])
    scheduler=WorkScheduler(quantum_checks=8,capacity=3)
    window=IndexedHistoryWindow(catalog)
    api=HistoryApi(CellHistorySeries(tmp_path/'absent',window_backend=window),EventOverlayAdapter(metadata),
        hycube_series=HycubeBatteryCapacitySeries(tmp_path/'absent',window_backend=IndexedHistoryWindow(hc)),
        scheduler=scheduler,timing_state=HistoryRequestTimingState())
    target='/api/history/series?metrics=soc,current,cell_voltage&selected_modules=1,2,3,4,5,6&voltage_cell_numbers=1,2&from='+iso(t).replace('+00:00','Z')+'&to='+iso(t+86399).replace('+00:00','Z')
    return api,window,target,scheduler


@pytest.mark.parametrize('older_days',[0,29,364,729])
def test_actual_24h_api_all_modules_shared_channels_scale(tmp_path,older_days):
    api,window,target,_=fixture(tmp_path,older_days)
    for cache in ('cold','warm'):
        import tracemalloc
        tracemalloc.start();started=time.perf_counter();cpu=time.process_time()
        response=api.handle('GET',target)
        measured={'wall_seconds':time.perf_counter()-started,'cpu_seconds':time.process_time()-cpu,'python_peak_bytes':tracemalloc.get_traced_memory()[1]}
        tracemalloc.stop()
        assert response.status==200,response.body
        series=response.body['series'];m=window.metrics
        assert len(series)==3 and [len(s['points']) for s in series]==[18,18,36]
        assert len(response.body['soc_timeline']['hycube_series']['points'])==2
        assert m['decodes']==18 and m['files_opened']==1 and m['pages']==1
        assert m['sql_vm_steps']<3000
        assert all([p['timestamp'] for p in s['points']]==sorted(p['timestamp'] for p in s['points']) for s in series)
        print('ACTUAL_HISTORY_24H '+json.dumps({'days':older_days+1,'cache':cache,**measured,**m},sort_keys=True))


def test_actual_api_fair_admission_cancel_timeout_during_live_raw(tmp_path):
    api,window,target,scheduler=fixture(tmp_path,0)
    from history_live_priority import raw_priority
    from work_scheduler import checkpoint
    started=threading.Event();stop=threading.Event();raw_count=[]
    def raw():
        with scheduler.select():
            while not stop.is_set():
                with scheduler.raw_section():
                    raw_count.append(time.monotonic());started.set()
                stop.wait(.002)
    thread=threading.Thread(target=raw);thread.start();assert started.wait(2)
    try:
        with ThreadPoolExecutor(2) as pool:
            results=[pool.submit(api.handle,'GET',target) for _ in range(2)]
            assert all(f.result(timeout=10).status==200 for f in results)
        assert len(raw_count)>1
        assert api.handle('GET',target,cancelled=lambda:True).body['error']['code']=='cancelled'
        assert api.handle('GET',target,deadline=time.monotonic()-1).status==503
        assert not scheduler.status()['active']
        assert len([j for j in scheduler.status()['recent'] if j['state']=='complete'])==2
    finally:
        stop.set();thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize('start,end,hours',[
    ('2026-03-29T00:00:00+01:00','2026-03-30T00:00:00+02:00',23),
    ('2026-10-25T00:00:00+02:00','2026-10-26T00:00:00+01:00',25)])
def test_actual_api_dst_window_and_inclusive_last_measurement(tmp_path,start,end,hours):
    from urllib.parse import urlencode
    first,last=[datetime.fromisoformat(x).timestamp() for x in (start,end)]
    assert last-first==hours*3600
    catalog=SourceCatalog(tmp_path/'catalog.sqlite')
    records=[{'schema_version':1,'timestamp':t,'module':1,'module_serial':'S1',
        'soc_percent':value,'current_a':0,'voltages_mv':[3300],'temperatures_c':[20]}
        for t,value in [(first-1,10),(first,20),(last,30),(last+1,40)]]
    path=write(tmp_path/'legacy-local-name.jsonl',records);catalog.register(**registration(path));index_all(catalog)
    metadata=PreparedMetadata(ProjectionRepository(tmp_path/'meta.sqlite'),'test');metadata.initialize();metadata.prepare_timeline([])
    api=HistoryApi(CellHistorySeries(tmp_path/'unused',window_backend=IndexedHistoryWindow(catalog)),EventOverlayAdapter(metadata),scheduler=WorkScheduler())
    response=api.handle('GET','/api/history/series?'+urlencode({'metric':'soc','module_number':1,'from':start,'to':end}))
    assert response.status==200,response.body
    assert [p['value'] for p in response.body['series']['points']]==[20,30]
    assert response.body['window']['inclusive'] is True
