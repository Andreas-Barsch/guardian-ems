import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from work_scheduler import WorkScheduler, WorkCancelled, checkpoint
from local_work_composition import LocalWorkComposition
from test_work_scheduler import wait_for


def test_real_manual_request_waits_once_for_raw_and_completes(tmp_path):
    from test_history_api import api_env, write_sample
    import history_live_priority as live
    _, directory, api = api_env(tmp_path)
    stamp=datetime(2026,8,12,10,42,tzinfo=timezone.utc).timestamp()
    write_sample(directory,stamp)
    s=WorkScheduler(quantum_checks=4);c=LocalWorkComposition(s)
    live.cycle_started()
    try:
        with ThreadPoolExecutor(1) as pool:
            with s.raw_section():
                f=pool.submit(c.request,api,'GET','/api/history/series?metric=soc&from=2026-08-12T10:00:00Z&to=2026-08-12T11:00:00Z&module_number=3')
                wait_for(lambda:len(s.status()['active'])==1)
                assert not f.done()
            response=f.result(5)
        assert response.status==200,response.body
        assert response.body['series']['points'][0]['value']==64
        assert len(s.status()['recent'])==1
    finally:live.collector_stopped()


def test_real_daily_same_results_and_budget_cancellation(tmp_path,monkeypatch):
    import daily_diagnostics as daily
    from test_daily_diagnostics import source_set,management,cell,berlin_timestamp
    stamp=berlin_timestamp();sources=source_set(tmp_path,[management(stamp,-25)],
        [cell(stamp+n,c8=3200) for n in range(60)])
    reference=daily.run_daily_diagnostic('2026-08-31',sources,tmp_path/'reference')
    s=WorkScheduler(quantum_checks=4)
    actual=s.execute('daily',lambda:daily.run_daily_diagnostic('2026-08-31',sources,tmp_path/'actual'))
    assert actual['components']==reference['components']
    for path in (tmp_path/'reference/aggregates').rglob('*.json'):
        left=json.loads(path.read_text());right=json.loads((tmp_path/'actual/aggregates'/path.relative_to(tmp_path/'reference/aggregates')).read_text())
        # Existing daily generated timestamps are provenance, not scientific values.
        def stable(value):
            if isinstance(value,dict):return {k:stable(v) for k,v in value.items() if k not in ('created_at','generated_at','evaluated_at')}
            if isinstance(value,list):return [stable(v) for v in value]
            return value
        assert stable(left)==stable(right)
    assert s.status()['recent'][-1]['checkpoints']>60
    stop=threading.Event();original=daily.BmsManagementEvidenceAnalyzer.analyze
    def cancel(*a,**kw):
        stop.set();checkpoint();return original(*a,**kw)
    monkeypatch.setattr(daily.BmsManagementEvidenceAnalyzer,'analyze',cancel)
    with pytest.raises(WorkCancelled):
        s.execute('daily',lambda:daily.run_daily_diagnostic('2026-08-31',sources,tmp_path/'cancel'),cancelled=stop.is_set)
    assert not list((tmp_path/'cancel').rglob('*.json'))
    assert not list((tmp_path/'cancel').rglob('*.tmp'))


def test_actual_daily_worker_and_display_worker_share_admission(tmp_path):
    from test_daily_diagnostics import source_set,management,berlin_timestamp
    from test_display_history_projection import builder,cell_record,NOW
    from datetime import timedelta
    (tmp_path/'daily').mkdir();(tmp_path/'display').mkdir()
    sources=source_set(tmp_path/'daily',[management(berlin_timestamp(),-25)])
    s=WorkScheduler(quantum_checks=2);c=LocalWorkComposition(s)
    now=datetime(2026,9,1,12,tzinfo=timezone.utc)
    daily=c.daily_worker(sources,tmp_path/'daily-out',clock=lambda:now)
    daily.check_once();daily.check_once()
    assert any(j['kind']=='daily' and j['checkpoints']>2 for j in s.status()['recent'])
    core,_=builder(tmp_path/'display',[cell_record(NOW-60)])
    display=c.display_worker(core,interval_seconds=.01)
    display.start()
    try:wait_for(lambda:any(j['kind']=='display' for j in s.status()['recent']))
    finally:assert display.stop(5)
    assert not s.status()['active']


def test_real_reference_workers_exact_reduction_does_not_restart_on_yield(tmp_path,monkeypatch):
    from test_diagnostic_repository import setup,raw,run,reference,freeze_evaluation_clock
    from test_canonical_repository import append_cells
    from cell_diagnostics import CellDiagnosticStore
    # Freeze only the existing report timestamp for exact differential comparison.
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None):return cls(2026,1,1,tzinfo=timezone.utc)
    monkeypatch.setattr('evidence_diagnostics.datetime',Frozen)
    env=setup(tmp_path,maximum=160);records=[raw(i,delta=i%9) for i in range(1,145)]
    for offset in range(0,len(records),40):append_cells(env,records[offset:offset+40])
    s=WorkScheduler(quantum_checks=2);c=LocalWorkComposition(s)
    persistence,analysis,writer=c.diagnostic_workers(env['d'],max_persistence_steps=256)
    persistence.submit(dict(name='test',source='raw',revision='r1',target_offset=env['catalog'].progress('raw','r1')['cursor']),None)
    persistence.start();wait_for(lambda:persistence.status()['last_completed'] is not None);assert persistence.stop(15)
    assert persistence.status()['last_completed']['success']
    job=env['d'].begin_analysis('test',[1]);calls=[];original=CellDiagnosticStore.analyse
    def counted(self,*a,**kw):calls.append(1);return original(self,*a,**kw)
    monkeypatch.setattr(CellDiagnosticStore,'analyse',counted)
    stop=threading.Event();entered=threading.Event()
    def rival():
        entered.set()
        while not stop.is_set():checkpoint()
    with ThreadPoolExecutor(1) as pool:
        f=pool.submit(s.execute,'daily_competitor',rival);assert entered.wait(5)
        analysis.start();analysis.submit(1,144,env['d'].analysis_reference(job))
        try:wait_for(lambda:analysis.status()['last_success'] is not None)
        finally:stop.set();f.result(5);assert analysis.stop(5)
    assert analysis.status()['last_success']
    assert len(calls)==1, 'quantum must not restart exact module reduction'
    monkeypatch.setattr(CellDiagnosticStore,'analyse',original)
    cells,agg=reference(records,env['options'])
    assert analysis.latest()['results'][1]==dict(cells.analyse(1,env['options'],(),agg.for_identity(1,'A')),physical_module_serial='A')
    assert next(j for j in s.status()['recent'] if j['kind']=='analysis')['yields']>0


def test_actual_preparation_recovers_published_cursor(tmp_path):
    from test_canonical_repository import composed,append_cells,sample
    from test_history_repository_preparation import prep
    from canonical_day_repository import bounds
    from diagnostic_startup_io import StartupBudget
    env=composed(tmp_path);start,_=bounds(env['origin']);append_cells(env,[sample(start+5)])
    p=prep(env);p.enroll('serial-a');p.confirm_frontier(env['origin'],proof={'test':True})
    s=WorkScheduler(quantum_checks=2);c=LocalWorkComposition(s);stop=threading.Event()
    budget=StartupBudget(stop,gate=True)
    c.preparation_step(p,budget)
    p=prep(env)
    for _ in range(100):
        result=c.preparation_step(p,budget)
        if p.status()['state']=='caught_up':break
    assert p.status()['state']=='caught_up'
    assert c.preparation_step(p,budget)['raw_bytes']==0


def test_mqtt_real_projection_without_network(tmp_path):
    from test_mqtt_observability import publisher,Client
    from test_derived_mqtt_worker import job
    s=WorkScheduler(quantum_checks=2);c=LocalWorkComposition(s);client=Client()
    sink=publisher(client).derived_sink();worker=c.mqtt_worker(sink.publish_derived_job)
    request=job(1);request['results']={1:{'status':'unknown','cells':[]}}
    worker.start();assert worker.submit(request)
    try:wait_for(lambda:worker.status()['generation_last_completed']==1)
    finally:assert worker.stop(5)
    assert worker.status()['last_completed']['success']
    assert client.calls
    assert next(j for j in s.status()['recent'] if j['kind']=='mqtt')['checkpoints']>2


def test_cancelled_analysis_reopens_confirmed_progress_without_raw_replay(tmp_path,monkeypatch):
    from test_diagnostic_repository import setup,raw,run,drain_analysis
    from test_canonical_repository import append_cells
    from diagnostic_repository import DiagnosticRepository
    from projection_repository import ProjectionRepository
    env=setup(tmp_path,maximum=80)
    append_cells(env,[raw(i) for i in range(1,70)]);run(env)
    d=env['d'];job=d.begin_analysis('test',[1]);stop=threading.Event()
    scheduler=WorkScheduler(quantum_checks=1)
    def part():
        assert d.analysis_step(job)['samples_processed']==32
        stop.set();checkpoint()
    with pytest.raises(WorkCancelled):scheduler.execute('analysis',part,cancelled=stop.is_set)
    confirmed=d.analysis_state(job)['after_sample']
    reopened=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    assert reopened.analysis_state(job)['after_sample']==confirmed
    monkeypatch.setattr(reopened.reader,'read_refs',lambda *a,**k:pytest.fail('unexpected raw replay'))
    new_scheduler=WorkScheduler(quantum_checks=1)
    steps=new_scheduler.execute('analysis',lambda:drain_analysis(reopened,job))
    assert sum(x['samples_processed'] for x in steps)==37
    assert reopened.analysis_state(job)['status']=='complete'


@pytest.mark.parametrize('scheduled',[False,True])
def test_display_failure_does_not_repeat_completed_startup_reset(tmp_path,monkeypatch,scheduled):
    from test_display_history_projection import builder
    from display_history_projection import DisplayHistoryProjectionWorker
    core,_=builder(tmp_path);seen=[]
    monkeypatch.setattr(core,'reset_open_day',lambda day:seen.append(day))
    monkeypatch.setattr(core,'_sources',lambda day:True)
    def failure(day):raise OSError('synthetic chunk failure')
    monkeypatch.setattr(core,'process_chunk',failure)
    worker=DisplayHistoryProjectionWorker(core);recovery=[True]
    scheduler=WorkScheduler()
    for _ in range(2):
        with pytest.raises(OSError,match='synthetic chunk failure'):
            if scheduled:scheduler.execute('display',lambda:worker._cycle(recovery))
            else:worker._cycle(recovery)
    assert len(seen)==1 and recovery==[False]
