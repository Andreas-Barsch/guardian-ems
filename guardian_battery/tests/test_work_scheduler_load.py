"""Synthetic local load evidence; interval is a stress input, not an HA budget."""
import json
import resource
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone

import pytest
from work_scheduler import WorkScheduler, checkpoint
from local_work_composition import LocalWorkComposition


@pytest.mark.parametrize('mode',['isolated','combined'])
def test_existing_workers_local_load(tmp_path,mode):
    from test_diagnostic_repository import setup,raw,run
    from test_canonical_repository import append_cells,sample
    from test_history_repository_preparation import prep
    from canonical_day_repository import bounds
    from diagnostic_startup_io import StartupBudget
    from test_daily_diagnostics import source_set,management,cell,berlin_timestamp
    from daily_diagnostics import run_daily_diagnostic
    from test_display_history_projection import builder,cell_record,NOW
    from test_history_api import api_env,write_sample
    from test_mqtt_observability import publisher,Client
    from test_derived_mqtt_worker import job
    def wait_for(predicate):
        deadline=time.monotonic()+30
        while not predicate():
            assert time.monotonic()<deadline, 'local load synchronization deadline'
            time.sleep(.002)
    from cell_history import CellHistoryWriter
    from collector_timing import CollectorTiming,PeriodicDeadline
    for name in ('diag','daily','display','manual'): (tmp_path/name).mkdir()
    env=setup(tmp_path/'diag',maximum=100)
    start,_=bounds(env['origin'])
    records=[dict(raw(start+i,serial='SERIAL-A'),module=1) for i in range(1,9)]
    for offset in range(0,8,30):append_cells(env,records[offset:offset+30])
    run(env);analysis_job=env['d'].begin_analysis('test',[1])
    append_cells(env,[dict(raw(start+9,serial='SERIAL-A'),module=1)])
    preparation=prep(env);preparation.enroll('serial-a');preparation.confirm_frontier(env['origin'],proof={'synthetic':True})
    stamp=berlin_timestamp();sources=source_set(tmp_path/'daily',[management(stamp,-25)], [cell(stamp+i,c8=3200) for i in range(80)])
    display,_=builder(tmp_path/'display',[cell_record(NOW-60+i) for i in range(40)])
    _,directory,api=api_env(tmp_path/'manual');write_sample(directory,datetime(2026,8,12,10,42,tzinfo=timezone.utc).timestamp())
    scheduler=WorkScheduler(quantum_checks=8);composition=LocalWorkComposition(scheduler)
    persist,analysis,writer=composition.diagnostic_workers(env['d'])
    client=Client();mqtt=composition.mqtt_worker(publisher(client).derived_sink().publish_derived_job)
    display_worker=composition.display_worker(display,interval_seconds=.01)
    raw_writer=CellHistoryWriter(tmp_path/'raw-output')
    timing=CollectorTiming(.01,.01);stop=threading.Event();jitter=[];missed=[];intervals=[]
    def acquisition():
        deadline=PeriodicDeadline(.01);previous=None
        with scheduler.select():
            for index in range(40):
                stop.wait(deadline.delay())
                now=time.monotonic();planned=deadline.next_deadline
                jitter.append(max(0,now-planned));missed.append(deadline.consume(now))
                if previous is not None:intervals.append(now-previous)
                previous=now;timing.cycle_started(time.time(),now);timing.cell_started(time.time(),now,planned)
                raw_writer.append(dict(raw(start+index,serial='SYNTHETIC-LIVE'),module=1))
                timing.cell_finished(time.monotonic()-now);timing.cycle_finished(time.monotonic()-now)
    def startup_work():
        budget=StartupBudget(stop)
        for _ in range(1000):
            composition.preparation_step(preparation,budget)
            if preparation.status()['state']=='caught_up':return
        raise AssertionError('preparation failed to progress: '+repr(preparation.status()))
    def startup():
        return scheduler.execute('startup_preparation',startup_work)
    def daily():
        return scheduler.execute('daily',lambda:run_daily_diagnostic('2026-08-31',sources,tmp_path/'daily-out'))
    def manual():
        response=composition.request(api,'GET','/api/history/series?metric=soc&from=2026-08-12T10:00:00Z&to=2026-08-12T11:00:00Z&module_number=3',deadline=time.monotonic()+20)
        assert response.status==200,response.body
    def workers():
        persist.submit(dict(name='test',source='raw',revision='r1',target_offset=env['catalog'].progress('raw','r1')['cursor']),None)
        persist.start();analysis.start();analysis.submit(1,start+8,env['d'].analysis_reference(analysis_job))
        mqtt.start();request=job(1);request['results']={1:{'status':'unknown','cells':[]}};assert mqtt.submit(request)
        display_worker.request_historical_rebuild();display_worker.start()
        try:
            wait_for(lambda:analysis.status()['last_success'] is not None)
            wait_for(lambda:mqtt.status()['generation_last_completed']==1)
            wait_for(lambda:any(j['kind']=='display' for j in scheduler.status()['recent']))
            wait_for(lambda:persist.status()['last_completed'] is not None)
            assert persist.stop(10)
        finally:
            assert analysis.stop(10);assert mqtt.stop(10);assert display_worker.stop(10)
        assert persist.status()['last_completed']['success']
        assert analysis.status()['last_success']
        assert mqtt.status()['last_completed']['success']
    before={str(p):p.stat().st_size for p in tmp_path.rglob('*') if p.is_file()}
    tracemalloc.start();cpu=time.process_time();started=time.monotonic()
    if mode=='combined':
        with ThreadPoolExecutor(5) as pool:
            futures=[pool.submit(fn) for fn in (acquisition,startup,daily,manual,workers)]
            for future in futures:future.result(30)
    else:
        for fn in (acquisition,startup,daily,manual,workers):fn()
    wall=time.monotonic()-started;cpu=time.process_time()-cpu
    _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    rawbytes=sum(p.stat().st_size for p in (tmp_path/'raw-output').glob('*.jsonl'))
    growth=sum(max(0,p.stat().st_size-before.get(str(p),0)) for p in tmp_path.rglob('*') if p.is_file())
    report={'scope':'synthetic local Mac; no hardware/HA acceptance','mode':mode,
            'wall_seconds':wall,'cpu_seconds':cpu,'peak_python_bytes':peak,
            'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'synthetic_interval_seconds':.01,'intervals_seconds':intervals,
            'jitter_seconds':jitter,'missed_slots':sum(missed),
            'raw_written_bytes':rawbytes,'all_logical_file_growth_bytes':growth,
            'physical_io_bytes':None,'raw_hardware_queue':None,'raw_hardware_drops':None,
            'diagnostic_last_transaction':env['d'].last_transaction,
            'scheduler':scheduler.status()}
    print('SCHEDULER_LOAD '+json.dumps(report,sort_keys=True))
    assert len(jitter)==40 and rawbytes>0
    assert not scheduler.status()['active']
    assert all(j['state']=='complete' or (j['kind']=='display' and j['state']=='cancelled') for j in scheduler.status()['recent'])
    assert {'manual_read','daily','analysis','persistence','mqtt','display','startup_preparation'} <= {j['kind'] for j in scheduler.status()['recent']}
