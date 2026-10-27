import json
import threading
import time
from pathlib import Path
import pytest

import diagnostic_startup_io as io
import history_live_priority as priority
from diagnostic_startup import DiagnosticStartup
from cell_diagnostics import CellDiagnosticStore
from diagnostic_aggregates import DiagnosticAggregateStore
from current_condition_backfill import CurrentConditionBackfill
from diagnostic_backfill import DiagnosticAggregateBackfill
from config_ui import DEFAULTS
from test_current_condition_backfill import raw, write_day


def wait_for(predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.005)
    assert predicate()


def worker(root, gate=False):
    return DiagnosticStartup(root/'cell.json',root/'aggregate.json',root/'raw',
        root/'positions.jsonl',{**DEFAULTS,'cell_diag_history_max_samples':20},gate=gate)


def finish(task):
    wait_for(lambda:task.status()['state'] in ('ready','blocked'))
    assert task.status()['state']=='ready',task.status()
    result=task.take_ready()
    assert result is not None
    assert task.stop()
    return result


@pytest.mark.parametrize('payload', [None,True,False,123,-1.5,1.2e34,'ä\\"'*4000,
    {'a':[{'b':list(range(100))} for _ in range(50)],'z':'end'}, {}, []])
def test_incremental_json_matches_standard_decoder(tmp_path,payload):
    path=tmp_path/'value.json';path.write_text(json.dumps(payload,ensure_ascii=False))
    with io.startup_context(io.StartupBudget(threading.Event(),gate=False)):
        assert io.load(path)==payload


def test_budget_failure_not_swallowed_or_written(tmp_path,monkeypatch):
    path=tmp_path/'cell.json';path.write_text('{"identity_samples":{}}')
    before=path.read_bytes()
    monkeypatch.setattr(io,'MAX_CACHE_BYTES',4)
    task=worker(tmp_path);task.start()
    wait_for(lambda:task.status()['state']=='blocked')
    assert 'cache_byte_budget' in task.status()['error']
    assert task.take_ready() is None
    assert path.read_bytes()==before
    assert task.stop()


def test_gate_defers_all_history_until_first_live_idle_and_raw_backlog_clear(tmp_path):
    write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    priority.cycle_started()
    task=worker(tmp_path,True);task.start()
    try:
        time.sleep(.06)
        assert task.status()['bytes_read']==0
        class Writer: pass
        import queue
        writer=Writer();writer._queue=queue.Queue();writer._queue.put('raw')
        priority.register_raw_writer(writer)
        priority.idle_until(time.monotonic()+10)
        time.sleep(.06)
        assert task.status()['bytes_read']==0
        writer._queue.get()
        finish(task)
    finally:
        task.stop();priority.collector_stopped()


def test_cancel_while_waiting_never_touches_original_cache(tmp_path):
    path=tmp_path/'cell.json';path.write_text('{"identity_samples":{}}')
    priority.cycle_started()
    task=worker(tmp_path,True);task.start()
    try:
        assert task.stop(.5)
        assert path.read_text()=='{"identity_samples":{}}'
        assert task.status()['state']=='cancelled'
    finally: priority.collector_stopped()


def test_live_replay_and_raw_overlap_have_no_duplicates(tmp_path):
    values=[raw(1787220000+i*60) for i in range(10)]
    write_day(tmp_path/'raw','2026-08-20',values)
    task=worker(tmp_path)
    task.submit([CurrentConditionBackfill._sample(v,([],[])) for v in values[-2:]])
    newer=raw(1787220600)
    task.submit([CurrentConditionBackfill._sample(newer,([],[]))])
    task.start();cell,aggregate=finish(task)
    assert [v['timestamp'] for v in cell.iter_all_samples()]==[v['timestamp'] for v in values]+[newer['timestamp']]
    assert all(v['sample_count']==11 for v in aggregate.records.values())
    from diagnostic_resume import load
    saved = load(tmp_path/'cell.json', tmp_path/'aggregate.json', task.options, tmp_path/'positions.jsonl')
    assert saved is not None and saved['sources']
    assert json.loads((tmp_path/'cell.json').read_text()) == cell.persistence_payload()
    assert json.loads((tmp_path/'aggregate.json').read_text())['records'] == aggregate.records
    assert not list(tmp_path.glob('.diagnostic-startup-*'))


def test_queue_overflow_keeps_raw_and_old_cache(tmp_path):
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    before=path.read_bytes();task=worker(tmp_path)
    task.MAX_PENDING_SAMPLES=1
    sample=CurrentConditionBackfill._sample(raw(1787220000),([],[]))
    assert not task.submit([sample,sample])
    task.start();assert task.stop()
    assert task.status()['state']=='blocked'
    assert path.read_bytes()==before


def test_large_cold_reconciliation_uses_durable_bounded_preparation(tmp_path):
    values=[raw(1787220000+i,1,'SN-A') for i in range(6000)]
    path=write_day(tmp_path/'raw','2026-08-20',values)
    before=path.read_bytes()
    task=worker(tmp_path);task.start()
    cell, aggregate=finish(task)
    assert path.read_bytes()==before
    assert len(list(cell.iter_all_samples()))==20
    assert max(v['sample_count'] for v in aggregate.records.values())==6000
    assert task.status()['reports']['preparation']['units']>1


def test_restart_from_retained_raw_after_cancel(tmp_path):
    write_day(tmp_path/'raw','2026-08-20',[raw(1787220000+i*60) for i in range(20)])
    priority.cycle_started()
    task=worker(tmp_path,True);task.start();assert task.stop()
    priority.collector_stopped()
    second=worker(tmp_path);second.start();cell,_=finish(second)
    assert len(list(cell.iter_all_samples()))==20


def test_open_partial_line_fails_closed(tmp_path):
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    with path.open('a') as handle: handle.write('{"schema_version":')
    task=worker(tmp_path);task.start()
    wait_for(lambda:task.status()['state']=='blocked')
    assert 'source_partial_line' in task.status()['error']
    assert task.take_ready() is None
    assert task.stop()
    assert not (tmp_path/'cell.json').exists()


def test_running_backfill_pauses_for_live_append_then_replays_exactly(tmp_path, monkeypatch):
    values=[raw(1787220000+i*60) for i in range(500)]
    path=write_day(tmp_path/'raw','2026-08-20',values)
    from history_append_receipt import before_append, after_append, signature
    after_append(path, before_append(path))
    task=worker(tmp_path,True)
    paused=threading.Event()
    original=task._budget.checkpoint
    calls=0
    def checkpoint():
        nonlocal calls
        if task._budget.stage=='reconcile_current_cache':
            calls+=1
            if calls==60:
                priority.cycle_started()
                paused.set()
        original()
    monkeypatch.setattr(task._budget,'checkpoint',checkpoint)
    priority.cycle_started();priority.idle_until(time.monotonic()+30)
    task.start()
    try:
        assert paused.wait(3)
        reads=task.status()['bytes_read']
        @priority.raw_priority
        def append_live():
            item=raw(1787251000)
            generation = before_append(path)
            with path.open('a') as handle:
                handle.write(json.dumps(item)+'\n')
            after_append(path, generation)
            assert task.submit([CurrentConditionBackfill._sample(item,([],[]))], frontiers={path.name: signature(path)})
        append_live()
        time.sleep(.06)
        assert task.status()['bytes_read']==reads
        assert path.read_text().splitlines()[-1]==json.dumps(raw(1787251000))
        priority.idle_until(time.monotonic()+30)
        cell,aggregate=finish(task)
        timestamps=[v['timestamp'] for v in cell.iter_all_samples()]
        assert timestamps[-1]==1787251000
        assert timestamps.count(1787251000)==1
        assert len(timestamps)==20
        assert all(v['sample_count']==501 for v in aggregate.records.values())
    finally:
        task.stop();priority.collector_stopped()


def test_source_budget_is_explicit_and_preserves_originals(tmp_path,monkeypatch):
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    before=path.read_bytes()
    monkeypatch.setattr(io,'MAX_SOURCE_BYTES',10)
    task=worker(tmp_path);task.start()
    wait_for(lambda:task.status()['state']=='blocked')
    assert task.status()['error']=='source_file_budget'
    assert path.read_bytes()==before
    assert task.stop()


def test_position_change_blocks_handover_without_overwriting_cache(tmp_path):
    task=worker(tmp_path);task.start()
    wait_for(lambda:task.status()['state']=='ready')
    before = (tmp_path/'cell.json').read_bytes()
    (tmp_path/'positions.jsonl').write_text('changed')
    task.submit([CurrentConditionBackfill._sample(raw(1787220000),([],[]))])
    wait_for(lambda:task.status()['state']=='blocked')
    assert 'position_history_changed' in task.status()['error']
    assert task.take_ready() is None
    assert (tmp_path/'cell.json').read_bytes() == before
    assert task.stop()


def test_crash_leftover_uses_one_owned_scratch_slot(tmp_path):
    import subprocess
    import sys
    from diagnostic_startup_merge import StartupMerge
    script = "from diagnostic_startup_merge import StartupMerge; import os,sys; m=StartupMerge(sys.argv[1],20); m[('A',1,1.0)]={'timestamp':1,'module':1,'module_serial':'A'}; os._exit(0)"
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'.diagnostic-startup'/'merge.sqlite').exists()
    write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    task=worker(tmp_path);task.start();cell,_=finish(task)
    assert len(list(cell.iter_all_samples()))==1
    assert not (tmp_path/'.diagnostic-startup'/'merge.sqlite').exists()
    assert sorted(p.name for p in (tmp_path/'.diagnostic-startup').iterdir())==['OWNER','lock']


def test_unowned_scratch_is_not_overwritten(tmp_path):
    directory=tmp_path/'.diagnostic-startup';directory.mkdir()
    (directory/'merge.sqlite').write_bytes(b'unrelated')
    write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    task=worker(tmp_path);task.start()
    wait_for(lambda:task.status()['state']=='blocked')
    assert task.status()['error']=='unowned_scratch_database'
    assert (directory/'merge.sqlite').read_bytes()==b'unrelated'
    assert task.stop()


def test_retained_byte_budget_fails_without_partial_publication(tmp_path,monkeypatch):
    import diagnostic_startup_merge
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    before=path.read_bytes()
    monkeypatch.setattr(diagnostic_startup_merge,'MAX_MATERIALIZED_BYTES',10)
    task=worker(tmp_path);task.start()
    wait_for(lambda:task.status()['state']=='blocked')
    assert task.status()['error']=='retained_byte_budget'
    assert path.read_bytes()==before
    assert not (tmp_path/'cell.json').exists()
    assert task.stop()
