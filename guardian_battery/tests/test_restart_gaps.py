"""Only the two remaining restart/stop gaps; no production paths or hardware."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_diagnostic_startup import worker, raw, write_day, wait_for
from current_condition_backfill import CurrentConditionBackfill
from diagnostic_backfill import DiagnosticAggregateBackfill
from cell_diagnostics import CellDiagnosticStore
from diagnostic_aggregates import DiagnosticAggregateStore
from history_append_receipt import before_append, after_append


def ready(task):
    wait_for(lambda: task.status()['state'] in ('ready', 'blocked'), timeout=30)
    assert task.status()['state'] == 'ready', task.status()
    stores = task.take_ready()
    assert task.stop()
    return stores


def interrupt_after_units(task, units):
    original = task._preparation_checkpoint
    def boundary():
        if task.status()['reports'].get('preparation', {}).get('units', 0) >= units:
            task._stop.set()
        original()
    task._preparation_checkpoint = boundary


def test_large_missing_checkpoint_resumes_durable_units_and_matches_existing_formulas(tmp_path, monkeypatch):
    values = [raw(1787220000+i) for i in range(5000)]
    # Retained cells must still be sorted even when source order is not.
    values[:100] = reversed(values[:100])
    path = write_day(tmp_path/'raw', '2026-08-20', values)
    assert path.stat().st_size > 1024*1024
    task = worker(tmp_path); interrupt_after_units(task, 2); task.start()
    wait_for(lambda: task.status()['state'] == 'cancelled')
    assert task.stop()
    status = task.status()['reports']['preparation']
    assert status['units'] == 2 and status['source_offset'] > 0
    assert not (tmp_path/'cell.json').exists()
    calls = []
    original = CurrentConditionBackfill._sample
    monkeypatch.setattr(CurrentConditionBackfill, '_sample', staticmethod(lambda r,i: (calls.append(r['timestamp']), original(r,i))[1]))
    task = worker(tmp_path); task.start(); cell, aggregate = ready(task)
    assert len(calls) < len(values) - 256
    expected_cell = CellDiagnosticStore(tmp_path/'expected-cell.json', 20)
    CurrentConditionBackfill(tmp_path/'raw', tmp_path/'positions.jsonl').run(expected_cell)
    expected_aggregate = DiagnosticAggregateStore(tmp_path/'expected.json', CellDiagnosticStore.phases)
    DiagnosticAggregateBackfill(tmp_path/'raw', tmp_path/'positions.jsonl').run(expected_aggregate, task.options)
    assert list(cell.iter_all_samples()) == list(expected_cell.iter_all_samples())
    assert aggregate.records == expected_aggregate.records
    assert task.status()['reports']['preparation']['reason'] == 'missing_diagnostic_checkpoint'


@pytest.mark.parametrize('damage', ['cache', 'source', 'config'])
def test_invalid_shared_checkpoint_rebuilds_privately_then_publishes(tmp_path, damage):
    path = write_day(tmp_path/'raw', '2026-08-20', [raw(1787220000+i) for i in range(600)])
    first = worker(tmp_path); first.start(); ready(first)
    if damage == 'cache': (tmp_path/'aggregate.json').write_text('{}')
    elif damage == 'source':
        values = [raw(1787220000+i) for i in range(601)]
        path.write_text(''.join(json.dumps(v)+'\n' for v in values))
    else:
        checkpoint = tmp_path/'cell.resume.json'
        state = json.loads(checkpoint.read_text()); state['config_id'] = 'incompatible'
        checkpoint.write_text(json.dumps(state))
    before = {p: p.read_bytes() for p in [tmp_path/'cell.json', tmp_path/'aggregate.json']}
    task = worker(tmp_path); interrupt_after_units(task, 1); task.start()
    wait_for(lambda: task.status()['state'] == 'cancelled'); assert task.stop()
    assert all(p.read_bytes() == content for p,content in before.items())
    task = worker(tmp_path); task.start(); cell, aggregate = ready(task)
    assert max(v['sample_count'] for v in aggregate.records.values()) == (601 if damage=='source' else 600)
    from diagnostic_resume import load
    assert load(tmp_path/'cell.json', tmp_path/'aggregate.json', task.options, tmp_path/'positions.jsonl')


def test_large_tail_is_checkpointed_and_restart_does_not_read_confirmed_prefix(tmp_path, monkeypatch):
    path = write_day(tmp_path/'raw', '2026-08-20', [raw(1787220000)])
    after_append(path, before_append(path))
    task = worker(tmp_path); task.start(); ready(task)
    gen = before_append(path)
    with path.open('a') as stream:
        for i in range(1, 5000): stream.write(json.dumps(raw(1787220000+i))+'\n')
    after_append(path, gen)
    task = worker(tmp_path)
    task._preparation_checkpoint = lambda: task._stop.set()
    task.start(); wait_for(lambda: task.status()['state']=='cancelled'); assert task.stop()
    state = json.loads((tmp_path/'cell.resume.json').read_text())
    assert 0 < state['sources'][path.name]['offset'] < path.stat().st_size
    calls = []; original = CurrentConditionBackfill._sample
    monkeypatch.setattr(CurrentConditionBackfill, '_sample', staticmethod(lambda r,i: (calls.append(r['timestamp']),original(r,i))[1]))
    task = worker(tmp_path); task.start(); _, aggregate = ready(task)
    assert len(calls) < 4999
    assert max(v['sample_count'] for v in aggregate.records.values()) == 5000


def test_shutdown_signals_all_then_drains_sink_after_producer():
    from derived_persistence import stop_background_workers
    calls=[]
    class Worker:
        def __init__(self, name): self.name=name
        def stop(self, timeout): calls.append((self.name,timeout)); return timeout>0
    result=stop_background_workers([('history',Worker('history'),5),('persistence',Worker('persistence'),10)],
                                    reader=Worker('reader'),writer=Worker('writer'))
    assert all(result.values())
    assert calls[:2]==[('history',0),('persistence',0)]
    assert calls.index(('reader',5)) < calls.index(('writer',5))
    assert ('persistence',10) in calls


def test_shutdown_timeout_does_not_terminate_sink_of_live_producer():
    from derived_persistence import stop_background_workers
    calls=[]
    result=stop_background_workers([], reader=SimpleNamespace(stop=lambda timeout: False),
        writer=SimpleNamespace(stop=lambda timeout: calls.append(timeout)))
    assert result=={'rs485_reader':False,'rs485_writer':False} and not calls


def test_rs485_stop_flushes_dequeued_partial_batch(tmp_path):
    from rs485_evidence import Rs485EvidenceWriter
    writer=Rs485EvidenceWriter(tmp_path/'raw')
    value={'timestamp':'2026-08-20T12:00:00+00:00'}
    writer._queue.put(value)
    original=writer._queue.get; batches=[]
    def get(*args,**kwargs):
        result=original(*args,**kwargs); writer._stop.set(); return result
    writer._queue.get=get
    writer._write_batch=lambda batch: batches.append(list(batch))
    writer._run()
    assert batches==[[value]] and writer.stop()


def test_hycube_timeout_keeps_thread_and_flushes_after_collection_only():
    from hycube_evidence import HycubeCollector
    collector=HycubeCollector.__new__(HycubeCollector)
    collector._stop=threading.Event(); entered=threading.Event(); release=threading.Event(); actions=[]
    def collect(): entered.set(); release.wait(2); actions.append('collection_complete')
    collector._collect_loop=collect
    collector._set=lambda **kw: actions.append(kw['state'])
    collector.projection_store=SimpleNamespace(close=lambda: actions.append('flush'))
    collector._thread=threading.Thread(target=collector._run); collector._thread.start()
    assert entered.wait(1)
    assert not collector.stop(.01) and collector._thread.is_alive() and not actions
    release.set()
    assert collector.stop(1)
    assert actions==['collection_complete','flush','disabled']


def test_abort_inside_unit_rolls_back_offset_and_results_together(tmp_path, monkeypatch):
    from diagnostic_startup_io import StartupIncomplete
    write_day(tmp_path/'raw', '2026-08-20', [raw(1787220000+i) for i in range(800)])
    task=worker(tmp_path); calls=[]; original=CurrentConditionBackfill._sample
    def sample(record, index):
        calls.append(record['timestamp'])
        if len(calls)==300:
            task._stop.set(); raise StartupIncomplete('synthetic interrupt inside unit')
        return original(record,index)
    monkeypatch.setattr(CurrentConditionBackfill,'_sample',staticmethod(sample))
    task.start(); wait_for(lambda:task.status()['state']=='cancelled'); assert task.stop()
    assert task.status()['reports']['preparation']['units']==1
    monkeypatch.setattr(CurrentConditionBackfill,'_sample',original)
    task=worker(tmp_path); task.start(); _,aggregate=ready(task)
    assert max(v['sample_count'] for v in aggregate.records.values())==800


def test_raw_backed_live_queue_overflow_continues_from_durable_history(tmp_path):
    from history_append_receipt import signature
    values=[raw(1787220000+i) for i in range(5000)]
    path=write_day(tmp_path/'raw','2026-08-20',values)
    task=worker(tmp_path)
    samples=[CurrentConditionBackfill._sample(v,([],[])) for v in values]
    assert task.submit(samples[:2500],frontiers={path.name:signature(path)})
    assert task.submit(samples[2500:],frontiers={path.name:signature(path)})
    assert task.status()['pending_samples']<=task.MAX_PENDING_SAMPLES
    task.start(); cell,aggregate=ready(task)
    assert max(v['sample_count'] for v in aggregate.records.values())==5000
    assert len(list(cell.iter_all_samples()))==20


def test_stop_drains_active_and_latest_pending_diagnostic_generation(tmp_path, monkeypatch):
    from derived_persistence import DerivedPersistenceWorker
    import diagnostic_resume
    write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    task=worker(tmp_path);task.start();cell,aggregate=ready(task)
    sink=DerivedPersistenceWorker(cell,aggregate); entered=threading.Event(); release=threading.Event()
    original=diagnostic_resume.save; saved=[]
    def save(*args):
        if not saved:
            entered.set(); assert release.wait(2)
        original(*args); saved.append(args[2])
    monkeypatch.setattr(diagnostic_resume,'save',save)
    sink.start(); aggregate._dirty=True
    sink.submit(cell.persistence_payload(),aggregate.persistence_payload(),resume_context=task.resume_context())
    assert entered.wait(1)
    sample=CurrentConditionBackfill._sample(raw(1787220001),([],[]));cell.add(sample);aggregate.add(sample,task.options)
    sink.submit(cell.persistence_payload(),aggregate.persistence_payload(),resume_context=task.resume_context())
    assert not sink.stop(.01)
    release.set(); assert sink.stop(2)
    assert len(saved)==2 and sink.status()['persisted']==2 and not sink.status()['pending']
    restored=CellDiagnosticStore(tmp_path/'cell.json',20)
    assert len(list(restored.iter_all_samples()))==2


def test_persistence_stop_reports_failed_write(tmp_path):
    from derived_persistence import DerivedPersistenceWorker
    def fail(payload): raise OSError('synthetic disk failure')
    sink=DerivedPersistenceWorker(SimpleNamespace(persist_payload=fail),None)
    sink.start();sink.submit({'sample':1},None)
    assert not sink.stop(1)
    assert 'synthetic disk failure' in sink.status()['last_error']


def test_hycube_failed_final_flush_is_not_reported_as_clean_stop():
    from hycube_evidence import HycubeCollector
    collector=HycubeCollector.__new__(HycubeCollector)
    collector._stop=threading.Event(); collector._collect_loop=lambda:None;collector._set=lambda **kw:None
    def fail(): raise OSError('synthetic flush failure')
    collector.projection_store=SimpleNamespace(close=fail)
    collector._thread=threading.Thread(target=collector._run);collector._thread.start()
    assert not collector.stop(1)
