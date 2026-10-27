import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from cell_diagnostics import CellDiagnosticStore, CellSample
from diagnostic_aggregates import DiagnosticAggregateStore
from diagnostic_repository import DiagnosticRepository
from config_ui import DEFAULTS
from projection_repository import ProjectionRepository
from test_canonical_repository import composed, append_cells


@pytest.fixture(autouse=True)
def freeze_evaluation_clock(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr('evidence_diagnostics.datetime', Frozen)


def raw(stamp, serial='A', module=1, delta=0, current=-2, soc=50):
    return dict(schema_version=1, **asdict(CellSample(stamp,module,[3300+delta]+[3300]*14,
                current,soc,[25]*15,[False]*15,serial,'position')))


def setup(tmp_path, maximum=8, mode='live'):
    env=composed(tmp_path)
    d=DiagnosticRepository(env['repo'],env['catalog']);d.initialize()
    options={**DEFAULTS,'cell_diag_history_max_samples':maximum,'cell_diag_min_phase_samples':1,
             'cell_diag_aggregate_retention_days':30}
    d.create('test',options,ingestion=mode)
    env.update(d=d,options=options)
    return env


def run(env):
    steps=[]
    for _ in range(2000):
        step=env['d'].consume('test','raw','r1');steps.append(step)
        if step['status']=='caught_up':return steps
    raise AssertionError('not caught up')


def reference(records,options):
    cells=CellDiagnosticStore.in_memory(options['cell_diag_history_max_samples'])
    agg=DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases,30)
    for r in records:
        sample=CellSample(**{k:r[k] for k in CellSample.__dataclass_fields__})
        cells.add(sample);agg.add(sample,options)
    return cells,agg


def assert_equal(env,records):
    cells,agg=reference(records,env['options'])
    with env['d'].view('test') as (view,meta):
        assert list(view.cells.iter_all_samples())==list(cells.iter_all_samples())
        assert dict(view.aggregates.records)==agg.records
        for module in (1,2):
            assert view.cells.current_serial(module)==cells.current_serial(module)
            projected=agg.for_identity(module,cells.current_serial(module))
            assert view.cells.analyse(module,env['options'],(),projected)==cells.analyse(module,env['options'],(),projected)


def test_real_stores_delta_and_exact_analysis(tmp_path):
    env=setup(tmp_path)
    records=[raw(i,delta=-i) for i in range(1,14)]
    append_cells(env,records);steps=run(env)
    assert sum(s['decodes'] for s in steps)==len(records)
    assert max(s.get('changed_rows',0) for s in steps)<=128
    assert_equal(env,records)


def test_warm_restart_no_raw_read_no_ring_rebuild(tmp_path,monkeypatch):
    env=setup(tmp_path);records=[raw(i) for i in range(1,12)]
    append_cells(env,records);run(env)
    d=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    def fail(*a,**k):raise AssertionError('unexpected raw/rebuild')
    monkeypatch.setattr(d.reader,'read_refs',fail)
    monkeypatch.setattr(CellDiagnosticStore,'replace_samples',fail)
    with d.view('test') as (v,meta):
        assert len(v.cells.identity_samples['A'])==8
        assert v.cells.values_for_module(1)[-1]['timestamp']==11
    assert d.consume('test','raw','r1')['status']=='caught_up'


def test_new_append_only_new_raw_and_old_view_immutable(tmp_path):
    env=setup(tmp_path);records=[raw(i) for i in range(1,12)]
    append_cells(env,records);run(env);old=env['d'].state('test')['bundle']
    append_cells(env,[raw(12,delta=10)]);steps=run(env)
    assert sum(s['decodes'] for s in steps)==1
    with env['d'].view('test',bundle=old) as (v,_):assert v.cells.values_for_module(1)[-1]['timestamp']==11
    assert_equal(env,records+[raw(12,delta=10)])


@pytest.mark.parametrize('point',['after_private_delta','after_delta_commit','after_publications','after_bundle','after_head','after_pair_cursor','after_pair_commit'])
def test_failure_resume_pair_and_cursor(tmp_path,point):
    env=setup(tmp_path);records=[raw(1),raw(2)]
    append_cells(env,records[:1]);run(env);old=env['d'].state('test')['bundle']
    append_cells(env,records[1:]);called=[]
    def fault(stage):
        if stage==point:called.append(stage);raise RuntimeError('synthetic crash')
    with pytest.raises(RuntimeError,match='synthetic'):
        for _ in range(10):env['d'].consume('test','raw','r1',fault=fault)
    assert called
    with env['d'].view('test') as (v,_):
        stamps=[x['timestamp'] for x in v.cells.values_for_module(1)]
        assert stamps==([1,2] if point=='after_pair_commit' else [1])
        assert {r['sample_count'] for r in v.aggregates.records.values()}=={len(stamps)}
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    run(env);assert_equal(env,records)


def test_retention_pages_restart_and_late_old_day_semantics(tmp_path):
    env=setup(tmp_path)
    records=[raw(i*86400+1,serial=f'S-{i}',module=1) for i in range(1,15)]
    append_cells(env,records);run(env)
    future=raw(70*86400,serial='future');append_cells(env,[future])
    assert env['d'].consume('test','raw','r1')['status']=='private_delta'
    first=env['d'].consume('test','raw','r1');assert first['status']=='pruning' and first['changed_rows']==32
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    steps=run(env);assert max(x.get('changed_rows',0) for x in steps)<=32
    late=raw(2,serial='late');append_cells(env,[late]);run(env)
    assert_equal(env,records+[future,late])


def test_unknown_and_identity_swap_live_preserve_reference(tmp_path):
    env=setup(tmp_path,maximum=3)
    records=[raw(1),raw(2,serial=None),raw(3,serial='B'),raw(4,serial='A',module=2),raw(5,serial=None)]
    append_cells(env,records);run(env);assert_equal(env,records)


def test_invalid_private_pair_not_adopted(tmp_path):
    env=setup(tmp_path);append_cells(env,[raw(1)])
    env['d'].consume('test','raw','r1')
    with env['d'].view('test') as (v,_):assert not list(v.cells.iter_all_samples())
    assert env['repo'].cursor('diagnostic:test','raw','r1') is None
    run(env);assert_equal(env,[raw(1)])


def drain_analysis(d,job):
    steps=[]
    for _ in range(100):
        step=d.analysis_step(job);steps.append(step)
        if step['status']=='complete':return steps
    raise AssertionError('analysis not complete')


def test_analysis_pages_resume_and_current_options_reference(tmp_path,monkeypatch):
    env=setup(tmp_path,maximum=80)
    records=[raw(i,delta=-(i%10),current=(-2 if i%2 else 2),soc=(10 if i%3 else 90)) for i in range(1,76)]
    append_cells(env,records);run(env)
    changed={**env['options'],'cell_diag_discharge_current_a':3,'cell_diag_low_soc_percent':5,
             'cell_diag_observe_deviation_mv':3}
    old_bundle=env['d'].state('test')['bundle']
    env['d'].configure('test',changed)
    job=env['d'].begin_analysis('test',[1])
    assert env['d'].analysis_step(job)['samples_processed']==32
    before=env['d'].analysis_state(job)['after_sample']
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    assert env['d'].analysis_state(job)['after_sample']==before
    def fail(*a,**k):raise AssertionError('old Raw/rebuild must not run')
    monkeypatch.setattr(env['d'].reader,'read_refs',fail)
    monkeypatch.setattr(CellDiagnosticStore,'replace_samples',fail)
    steps=drain_analysis(env['d'],job)
    assert sum(x['samples_processed'] for x in steps)==43
    result=env['d'].analysis_result(job)[1]
    cells,agg=reference(records,env['options'])
    expected=cells.analyse(1,changed,(),agg.for_identity(1,'A'))
    assert result==dict(expected,physical_module_serial='A')
    assert env['d'].analysis_step(job)['samples_processed']==0
    with env['d'].view('test',bundle=old_bundle) as (_,meta):assert meta['options']==env['options']
    with env['d'].view('test') as (view,_):assert dict(view.aggregates.records)==agg.records


@pytest.mark.parametrize('point',['after_statistics_page','after_analysis_module'])
def test_analysis_failure_resume_without_duplicate_statistics(tmp_path,point):
    env=setup(tmp_path,maximum=40);records=[raw(i,delta=i%5) for i in range(1,38)]
    append_cells(env,records);run(env);job=env['d'].begin_analysis('test',[1])
    def fail(stage):
        if stage==point:raise RuntimeError('injected analysis crash')
    with pytest.raises(RuntimeError):
        for _ in range(6):env['d'].analysis_step(job,fault=fail)
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    drain_analysis(env['d'],job)
    cells,agg=reference(records,env['options'])
    assert env['d'].analysis_result(job)[1]==dict(cells.analyse(1,env['options'],(),agg.for_identity(1,'A')),physical_module_serial='A')


def test_actual_workers_use_checkpoint_refs_not_cache_snapshots(tmp_path,monkeypatch):
    import threading
    from diagnostic_repository import local_diagnostic_workers
    env=setup(tmp_path);records=[raw(1),raw(2)]
    append_cells(env,records)
    def fail(*a,**k):raise AssertionError('full cache snapshot/rebuild forbidden')
    monkeypatch.setattr(CellDiagnosticStore,'persistence_payload',fail)
    monkeypatch.setattr(DiagnosticAggregateStore,'persistence_payload',fail)
    monkeypatch.setattr(CellDiagnosticStore,'replace_samples',fail)
    persistence,analysis,writer=local_diagnostic_workers(env['d'])
    target=env['catalog'].progress('raw','r1')['cursor']
    persistence.submit(dict(name='test',source='raw',revision='r1',target_offset=target),None)
    persistence.start();assert persistence.stop(5)
    assert persistence.status()['last_completed']['success']
    assert_equal(env,records)
    job=env['d'].begin_analysis('test',[1])
    completed=threading.Event();actual=analysis.analyze
    def finish(payload):
        result=actual(payload);completed.set();return result
    analysis.analyze=finish;analysis.start();analysis.submit(1,2,env['d'].analysis_reference(job))
    assert completed.wait(5);assert analysis.stop(5)
    assert analysis.latest()['results']==env['d'].analysis_result(job)


def test_writer_budget_resumes_and_newer_prefix_survives_old_completion(tmp_path):
    from diagnostic_repository import RepositoryPairWriter
    env=setup(tmp_path);append_cells(env,[raw(1)])
    first=env['catalog'].progress('raw','r1')['cursor']
    writer=RepositoryPairWriter(env['d'],max_steps=1)
    with pytest.raises(Exception,match='step budget'):
        writer.persist_payload(dict(name='test',source='raw',revision='r1',target_offset=first))
    append_cells(env,[raw(2)])
    second=env['catalog'].progress('raw','r1')['cursor']
    writer.max_steps=10
    writer.persist_payload(dict(name='test',source='raw',revision='r1',target_offset=first))
    assert env['repo'].cursor('diagnostic:test','raw','r1').offset==first
    writer.persist_payload(dict(name='test',source='raw',revision='r1',target_offset=second))
    assert env['repo'].cursor('diagnostic:test','raw','r1').offset==second
    assert_equal(env,[raw(1),raw(2)])


def test_history_slot_replacement_and_sorted_retention(tmp_path):
    from current_condition_backfill import CurrentConditionBackfill
    env=setup(tmp_path,maximum=2,mode='history')
    records=[raw(10),raw(20),raw(5),raw(20,serial='B',delta=15),raw(30,serial='A',module=2)]
    append_cells(env,records);run(env)
    slots={};agg=DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases,30)
    for record in records:
        sample=CurrentConditionBackfill._sample(record,([],[]))
        slots[(sample.module,sample.timestamp)]=asdict(sample);agg.add(sample,env['options'])
    cells=CellDiagnosticStore.in_memory(2);cells.replace_samples(slots.values())
    with env['d'].view('test') as (view,_):
        for module in (1,2):
            assert view.cells.values_for_module(module)==cells.values_for_module(module)
            assert view.cells.current_serial(module)==cells.current_serial(module)
        assert dict(view.aggregates.records)==agg.records


@pytest.mark.parametrize('point',['after_delta_commit','after_pair_cursor','after_pair_commit'])
def test_actual_process_exit_recovers_confirmed_pair(tmp_path,point):
    import os,subprocess,sys
    env=setup(tmp_path);append_cells(env,[raw(1)]);run(env)
    append_cells(env,[raw(2)])
    script='''
import os,sys
from projection_repository import ProjectionRepository
from source_catalog import SourceCatalog
from diagnostic_repository import DiagnosticRepository
p=DiagnosticRepository(ProjectionRepository(sys.argv[1]),SourceCatalog(sys.argv[2]))
def crash(stage):
    if stage==sys.argv[3]:os._exit(83)
for _ in range(10):p.consume('test','raw','r1',fault=crash)
'''
    result=subprocess.run([sys.executable,'-c',script,str(env['repo'].path),str(env['catalog'].path),point],env=os.environ.copy())
    assert result.returncode==83
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    with env['d'].view('test') as (view,_):
        assert len(view.cells.values_for_module(1))==(2 if point=='after_pair_commit' else 1)
    run(env);assert_equal(env,[raw(1),raw(2)])


def test_current_identity_handover_is_persisted_and_old_pair_unchanged(tmp_path):
    env=setup(tmp_path);records=[raw(1),raw(2,serial='B',module=2),raw(3,serial=None)]
    append_cells(env,records);run(env);old=env['d'].state('test')['bundle']
    env['d'].set_current_identities('test',{1:'B',2:None})
    cells,agg=reference(records,env['options']);cells.set_current_identities({1:'B',2:None})
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    with env['d'].view('test') as (view,_):
        for module in (1,2):
            assert view.cells.current_serial(module)==cells.current_serial(module)
            assert view.cells.values_for_module(module)==cells.values_for_module(module)
    with env['d'].view('test',bundle=old) as (view,_):assert view.cells.current_serial(1) is None


def test_statistics_pages_do_not_materialize_whole_ring(tmp_path,monkeypatch):
    env=setup(tmp_path,maximum=70);append_cells(env,[raw(i) for i in range(1,67)]);run(env)
    job=env['d'].begin_analysis('test',[1])
    def forbidden(*a,**k):raise AssertionError('whole ring materialization during page')
    monkeypatch.setattr(CellDiagnosticStore,'values_for_module',forbidden)
    assert env['d'].analysis_step(job)['samples_processed']==32
    assert env['d'].analysis_step(job)['samples_processed']==32
    assert env['d'].analysis_step(job)['samples_processed']==2
    assert env['d'].analysis_state(job)['status']=='reduce'


def test_independent_raw_append_while_derived_transaction_is_held(tmp_path):
    from raw_registry import encoded,hashed
    env=setup(tmp_path);append_cells(env,[raw(1)])
    appended=[]
    def fault(stage):
        if stage=='after_private_delta':
            receipt=env['rawreceipt']
            env['rawreceipt']=env['rawreg'].append('raw',encoded(raw(2))+b'\n',expected_receipt=hashed(encoded(receipt)),operation='independent-raw')
            appended.append(True)
    step=env['d'].consume('test','raw','r1',fault=fault)
    assert appended==[True]
    assert step['transaction']['transaction_seconds']>=0
    assert step['transaction']['sql_vm_steps_approx']<200000
    assert step['changed_rows']<128


def test_corrupt_checkpoint_row_fails_without_fallback(tmp_path,monkeypatch):
    env=setup(tmp_path);append_cells(env,[raw(1)]);run(env)
    with env['repo']._write() as db:
        db.execute("UPDATE diagnostic_row SET payload='{}' WHERE kind='sample' AND retired IS NULL")
    def forbidden(*a,**k):raise AssertionError('Raw fallback forbidden')
    monkeypatch.setattr(env['d'].reader,'read_refs',forbidden)
    with pytest.raises(Exception,match='checksum'):
        with env['d'].view('test') as (view,_):view.cells.values_for_module(1)


def test_partial_analysis_keeps_old_options_and_pair_after_append(tmp_path):
    env=setup(tmp_path,maximum=40);records=[raw(i) for i in range(1,36)]
    append_cells(env,records);run(env);job=env['d'].begin_analysis('test',[1])
    env['d'].analysis_step(job)
    changed={**env['options'],'cell_diag_low_soc_percent':80}
    env['d'].configure('test',changed);append_cells(env,[raw(36)]);run(env)
    drain_analysis(env['d'],job)
    cells,agg=reference(records,env['options'])
    assert env['d'].analysis_result(job)[1]==dict(cells.analyse(1,env['options'],(),agg.for_identity(1,'A')),physical_module_serial='A')


def test_source_rewrite_before_publication_rejects_pair(tmp_path):
    env=setup(tmp_path);append_cells(env,[raw(1)])
    env['d'].consume('test','raw','r1')
    with env['catalog'].write() as db:db.execute("DELETE FROM active WHERE source='raw'")
    env['d'].finish('test')  # bounded prune stage
    with pytest.raises(Exception,match='source changed'):
        env['d'].finish('test')
    with env['d'].view('test') as (view,_):assert not list(view.cells.iter_all_samples())
    assert env['repo'].cursor('diagnostic:test','raw','r1') is None


def test_configured_capacity_resize_is_chunked_and_restartable(tmp_path):
    env=setup(tmp_path,maximum=80);records=[raw(i) for i in range(1,76)]
    append_cells(env,records);run(env);old=env['d'].state('test')['bundle']
    smaller={**env['options'],'cell_diag_history_max_samples':5}
    assert env['d'].configure('test',smaller)['status']=='resizing'
    assert env['d'].finish('test')['changed_rows']==33
    with env['d'].view('test') as (view,_):assert len(view.cells.values_for_module(1))==75
    env['d']=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    for _ in range(10):
        if env['d'].finish('test')['status']=='published':break
    with env['d'].view('test') as (view,meta):
        assert meta['max_samples']==5
        assert [x['timestamp'] for x in view.cells.values_for_module(1)]==list(range(71,76))
    larger={**smaller,'cell_diag_history_max_samples':100}
    env['d'].configure('test',larger)
    for _ in range(10):
        if env['d'].finish('test')['status']=='published':break
    with env['d'].view('test') as (view,meta):
        assert meta['max_samples']==100
        assert len(view.cells.values_for_module(1))==5  # no invented older evidence
    with env['d'].view('test',bundle=old) as (view,_):assert len(view.cells.values_for_module(1))==75


def test_history_identity_uses_real_bounded_epoch_and_guard(tmp_path):
    from test_history_epoch_adapter import setup as epoch_setup,add,drain,position
    env=setup(tmp_path,mode='history');folder=tmp_path/'positions';folder.mkdir()
    reg,epochs,reader,receipt=epoch_setup(folder,kind='position')
    snapshot=position(1,'RESOLVED');receipt=add(reg,receipt,[snapshot]);drain(reader)
    env['d']=DiagnosticRepository(env['repo'],env['catalog'],epochs)
    append_cells(env,[raw(2,serial=None)])
    run(env)
    with env['d'].view('test') as (view,meta):
        assert view.cells.values_for_module(1)[0]['module_serial']=='RESOLVED'
        assert view.cells.values_for_module(1)[0]['position_history_id']==snapshot.position_history_id
        assert meta['delta_provenance']['epoch_dependency'] is not None
    append_cells(env,[raw(3,serial=None)])
    env['d'].consume('test','raw','r1')
    add(reg,receipt,[position(1,'CHANGED',created=4)]);drain(reader)
    env['d'].finish('test')
    with pytest.raises(Exception,match='stale'):env['d'].finish('test')
    with env['d'].view('test') as (view,_):assert len(view.cells.values_for_module(1))==1


def test_real_worker_coalescing_keeps_all_confirmed_input_deltas(tmp_path):
    import threading
    from diagnostic_repository import local_diagnostic_workers
    env=setup(tmp_path);append_cells(env,[raw(1)])
    persistence,_,writer=local_diagnostic_workers(env['d'])
    original=writer.persist_payload;entered=threading.Event();release=threading.Event();calls=[]
    def blocked(ref):
        if not calls:
            entered.set();assert release.wait(5)
        calls.append(ref['target_offset']);return original(ref)
    writer.persist_payload=blocked
    def submit():
        target=env['catalog'].progress('raw','r1')['cursor']
        persistence.submit(dict(name='test',source='raw',revision='r1',target_offset=target),None)
        return target
    first=submit();persistence.start();assert entered.wait(2)
    try:
        append_cells(env,[raw(2)]);submit()
        append_cells(env,[raw(3)]);last=submit()
    finally:
        release.set();assert persistence.stop(5)
    assert calls==[first,last]
    assert persistence.status()['coalesced']==1
    assert_equal(env,[raw(1),raw(2),raw(3)])


@pytest.mark.parametrize('older_samples',[10,100,300])
def test_append_cost_measured_against_older_history(tmp_path,older_samples):
    import tracemalloc,time
    env=setup(tmp_path,maximum=8)
    for start in range(1,older_samples+1,50):
        append_cells(env,[raw(i) for i in range(start,min(start+50,older_samples+1))])
    run(env)
    append_cells(env,[raw(older_samples+1)])
    tracemalloc.start();started=time.monotonic();steps=run(env)
    elapsed=time.monotonic()-started;_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert sum(x['decodes'] for x in steps)==1
    assert max(x.get('changed_rows',0) for x in steps)<128
    metrics={'older_samples':older_samples,'decodes':sum(x['decodes'] for x in steps),
             'raw_bytes':sum(x['raw_bytes'] for x in steps),'peak_python_bytes':peak,
             'wall_seconds':elapsed,'transactions':[x['transaction'] for x in steps if x.get('transaction')]}
    print('DIAGNOSTIC_APPEND_METRICS '+json.dumps(metrics,sort_keys=True))


def test_history_quality_slots_are_not_invented_or_lost(tmp_path):
    env=setup(tmp_path,mode='history')
    invalid=dict(raw(2),voltages_mv=[3300]*14)
    records=[raw(1),invalid,raw(3,serial=None),raw(4)]
    append_cells(env,records);run(env)
    with env['d'].view('test') as (view,_):
        assert [v['timestamp'] for v in view.cells.values_for_module(1)]==[1,4]
        assert view.maps['quality']['invalid_sample']=={'count':1}
        assert view.maps['quality']['identity_unknown']=={'count':1}
    assert env['repo'].cursor('diagnostic:test','raw','r1').offset==env['catalog'].progress('raw','r1')['cursor']


def test_invalid_catalog_slot_keeps_provenance_without_fake_sample(tmp_path):
    from raw_registry import encoded,hashed
    env=setup(tmp_path,mode='history');r=env['rawreceipt']
    env['rawreceipt']=env['rawreg'].append('raw',b'{bad-json}\n',expected_receipt=hashed(encoded(r)),operation='invalid-slot')
    append_cells(env,[raw(1)])
    run(env)
    with env['d'].view('test') as (view,_):
        assert len(view.cells.values_for_module(1))==1
        assert view.maps['quality']['invalid_record']=={'count':1}


def test_completed_analysis_is_reused_after_restart_without_recomputation(tmp_path,monkeypatch):
    env=setup(tmp_path);append_cells(env,[raw(1),raw(2)]);run(env)
    job=env['d'].begin_analysis('test',[1]);drain_analysis(env['d'],job)
    expected=env['d'].analysis_result(job)
    restored=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    def forbidden(*a,**k):raise AssertionError('completed checkpoint must not recompute')
    monkeypatch.setattr(CellDiagnosticStore,'analyse',forbidden)
    monkeypatch.setattr(restored.reader,'read_refs',forbidden)
    assert restored.begin_analysis('test',[1])==job
    assert restored.analyse_reference(restored.analysis_reference(job))[0]==expected


def test_small_append_only_computes_new_condition_contribution(tmp_path,monkeypatch):
    env=setup(tmp_path,maximum=40);records=[raw(i,delta=i%5) for i in range(1,36)]
    append_cells(env,records);run(env);job=env['d'].begin_analysis('test',[1]);drain_analysis(env['d'],job)
    append_cells(env,[raw(36,delta=7)]);run(env)
    calls=[];original=CellDiagnosticStore.accumulate_current_statistics
    def counted(self,state,sample,options):
        calls.append(sample['timestamp']);return original(self,state,sample,options)
    monkeypatch.setattr(CellDiagnosticStore,'accumulate_current_statistics',counted)
    new_job=env['d'].begin_analysis('test',[1]);steps=drain_analysis(env['d'],new_job)
    assert calls==[36]
    assert sum(x['samples_processed'] for x in steps)==1
    cells,agg=reference(records+[raw(36,delta=7)],env['options'])
    assert env['d'].analysis_result(new_job)[1]==dict(cells.analyse(1,env['options'],(),agg.for_identity(1,'A')),physical_module_serial='A')


def test_final_exact_reduction_cooperates_and_does_not_publish_partial_result(tmp_path,monkeypatch):
    import diagnostic_repository
    env=setup(tmp_path,maximum=40);records=[raw(i) for i in range(1,36)]
    append_cells(env,records);run(env);job=env['d'].begin_analysis('test',[1])
    while env['d'].analysis_state(job)['status']=='statistics':env['d'].analysis_step(job)
    calls=[]
    def budget():
        calls.append(1)
        if len(calls)>50:raise RuntimeError('synthetic cooperative cancellation')
    with monkeypatch.context() as patch:
        patch.setattr(diagnostic_repository,'check_budget',budget)
        with pytest.raises(RuntimeError,match='cooperative'):env['d'].analysis_step(job)
    assert env['d'].analysis_state(job)['status']=='reduce'
    with pytest.raises(Exception,match='not complete'):env['d'].analysis_result(job)
    drain_analysis(env['d'],job)
    cells,agg=reference(records,env['options'])
    assert env['d'].analysis_result(job)[1]==dict(cells.analyse(1,env['options'],(),agg.for_identity(1,'A')),physical_module_serial='A')


def test_live_invalid_cell_slot_is_retained_but_not_scientific_evidence(tmp_path):
    env=setup(tmp_path)
    record=dict(raw(1),voltages_mv=[3300]*14)
    append_cells(env,[record]);run(env)
    with env['d'].view('test') as (view,_):
        assert len(view.cells.values_for_module(1))==1
        assert view.cells.analyse(1,env['options'])['sample_count']==0
        assert not dict(view.aggregates.records)
    job=env['d'].begin_analysis('test',[1]);drain_analysis(env['d'],job)
    assert env['d'].analysis_result(job)[1]['sample_count']==0



@pytest.mark.parametrize('first_order',[(1,2,3),(3,2,1)])
def test_historical_slot_identity_replacement_refills_actual_retention(tmp_path,first_order):
    from current_condition_backfill import CurrentConditionBackfill
    env=setup(tmp_path,maximum=2,mode='history')
    records=[raw(i) for i in first_order]
    append_cells(env,records);run(env)
    append_cells(env,[raw(3,serial='B')]);steps=run(env)
    assert sum(x['decodes'] for x in steps)==1
    slots={}
    for record in records+[raw(3,serial='B')]:
        sample=CurrentConditionBackfill._sample(record,([],[]))
        slots[(sample.module,sample.timestamp)]=asdict(sample)
    reference=CellDiagnosticStore.in_memory(2);reference.replace_samples(slots.values())
    with env['d'].view('test') as (view,_):
        assert list(view.cells.identity_samples['A'])==list(reference.identity_samples['A'])
        assert [v['timestamp'] for v in view.cells.identity_samples['A']]==[1,2]
        assert list(view.cells.identity_samples['B'])==list(reference.identity_samples['B'])
    restored=DiagnosticRepository(ProjectionRepository(env['repo'].path),env['catalog'])
    with restored.view('test') as (view,_):assert [v['timestamp'] for v in view.cells.identity_samples['A']]==[1,2]
