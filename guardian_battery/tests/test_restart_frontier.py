"""Focused restart/crash boundaries; synthetic data only."""
import json
import time
from pathlib import Path
import pytest
import resume_publication as resume
from display_history_projection import DisplayHistoryProjection, DisplayHistoryProjectionWorker, DisplayProjectionError
from test_display_history_projection import _current_day_projection, NOW, cell_record, append_rows, read_projection, by
from test_diagnostic_startup import worker, finish, raw, write_day
from history_append_receipt import before_append, after_append

@pytest.mark.parametrize("point", ["after_intent", "after_operation_0", "after_operation_1"])
def test_publication_crash_finishes_without_duplicate_append(tmp_path, point):
    data=tmp_path/'data'; data.write_bytes(b'old')
    state=tmp_path/'state'; state.write_bytes(b'0')
    intent=tmp_path/'.state'/'intent.json'
    def crash(at):
        if at==point: raise RuntimeError('power loss')
    with pytest.raises(RuntimeError):
        resume.publish(intent,[resume.append(data,b'new'),resume.replace(state,b'1')],fault=crash)
    assert resume.recover(intent)
    assert not resume.recover(intent)
    assert data.read_bytes()==b'oldnew' and state.read_bytes()==b'1'

@pytest.mark.parametrize("point", ["after_intent", "after_operation_0", "after_operation_4", "after_operation_8"])
def test_display_crash_resume_matches_continuous(tmp_path, monkeypatch, point):
    day,cell,source,rows,out=_current_day_projection(tmp_path)
    before={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*.jsonl.gz')}
    core=DisplayHistoryProjection(cell,tmp_path/'hycube',out,clock=lambda:NOW)
    worker=DisplayHistoryProjectionWorker(core)
    worker._cycle([True])
    assert {str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*.jsonl.gz')}==before
    with append_rows(source) as handle:
        handle.write(json.dumps(cell_record(NOW+125,serial='SERIAL-1'))+'\n')
    original=resume.publish
    def crash(at):
        if at==point: raise RuntimeError('power loss')
    monkeypatch.setattr(resume,'publish',lambda intent,ops: original(intent,ops,fault=crash))
    with pytest.raises(RuntimeError): core.process_chunk(day)
    monkeypatch.setattr(resume,'publish',original)
    restarted=DisplayHistoryProjection(cell,tmp_path/'hycube',out,clock=lambda:NOW)
    result=restarted.process_chunk(day)
    assert result['records_processed']==0
    values=by(read_projection(out,'1m',day),'soc')
    assert len(values)==3 and sum(v['sample_count'] for v in values)==3
    state=json.loads((out/'.state'/f'{day}.json').read_text())
    assert state['offsets'][f'cell:{source.name}']==source.stat().st_size
    assert sum(v['sample_count'] for v in state['buckets'].values() if v['metric']=='soc' and v['resolution']=='1m')==1


def test_display_changed_old_source_is_explicit_no_reset(tmp_path):
    day,cell,source,rows,out=_current_day_projection(tmp_path)
    product=out/'1m'/f'{day}.jsonl.gz'; before=product.read_bytes()
    source.write_text(source.read_text().replace('50','49'))
    core=DisplayHistoryProjection(cell,tmp_path/'hycube',out,clock=lambda:NOW)
    with pytest.raises(DisplayProjectionError,match='source changed'): core.process_chunk(day)
    assert product.read_bytes()==before


def test_diagnostic_restart_reads_no_old_raw_and_only_new_tail(tmp_path, monkeypatch):
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000),raw(1787220060)])
    after_append(path,before_append(path))
    task=worker(tmp_path);task.start();finish(task)
    original=Path.open
    def no_raw(self,*args,**kwargs):
        if self==path: raise AssertionError('valid old raw was reopened')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',no_raw)
    task=worker(tmp_path);task.start();finish(task)
    monkeypatch.setattr(Path,'open',original)
    gen=before_append(path)
    with path.open('a') as handle: handle.write(json.dumps(raw(1787220120))+'\n')
    after_append(path,gen)
    task=worker(tmp_path);task.start();cell,aggregate=finish(task)
    assert len(list(cell.iter_all_samples()))==3
    assert max(v['sample_count'] for v in aggregate.records.values())==3
    checkpoint=json.loads((tmp_path/'cell.resume.json').read_text())
    assert checkpoint['sources'][path.name]['size']==path.stat().st_size


def test_diagnostic_corrupt_pair_reuses_valid_private_preparation(tmp_path, monkeypatch):
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    task=worker(tmp_path);task.start();finish(task)
    (tmp_path/'aggregate.json').write_text('{}')
    original=Path.open
    def no_raw(self,*args,**kwargs):
        if self==path: raise AssertionError('valid staged source was reopened')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',no_raw)
    task=worker(tmp_path);task.start();cell,aggregate=finish(task)
    assert len(list(cell.iter_all_samples()))==1
    assert max(v['sample_count'] for v in aggregate.records.values())==1
    assert 'checkpoint result changed' in task.status()['reports']['preparation']['reason']


@pytest.mark.parametrize("point", ["after_intent", "after_operation_0", "after_operation_1"])
def test_phase_crash_keeps_open_machine_and_exact_frontier(tmp_path, monkeypatch, point):
    from canonical_phase import CanonicalPhaseProjection
    from config_history import ConfigHistory
    from test_canonical_phase import sample, PARAMS
    cell=tmp_path/'cell'; cell.mkdir(); out=tmp_path/'phase'; day='2026-01-01'
    config=tmp_path/'config.jsonl'
    config.write_text(json.dumps({'schema_version':1,'timestamp':'2025-01-01T00:00:00+00:00',
                                'config_id':'cfg','parameters':PARAMS})+'\n')
    path=cell/(day+'.jsonl');path.write_text(json.dumps(sample(1767225600))+'\n')
    after_append(path,before_append(path))
    core=CanonicalPhaseProjection(cell,out,ConfigHistory(config))
    original=resume.publish
    def crash(at):
        if at==point: raise RuntimeError('power loss')
    monkeypatch.setattr(resume,'publish',lambda intent,ops: original(intent,ops,fault=crash))
    with pytest.raises(RuntimeError): core.update_current(day)
    monkeypatch.setattr(resume,'publish',original)
    restarted=CanonicalPhaseProjection(cell,out,ConfigHistory(config))
    assert restarted.update_current(day)['samples']==0
    state=json.loads(restarted._current_state_path().read_text())
    assert state['offset']==path.stat().st_size and state['machines']
    with append_rows(path) as handle: handle.write(json.dumps(sample(1767225660,1.1))+'\n')
    assert restarted.update_current(day)['samples']==1
    assert restarted.update_current(day)['samples']==0


def test_diagnostic_persistence_stop_flushes_consistent_pair(tmp_path):
    from derived_persistence import DerivedPersistenceWorker
    from diagnostic_resume import context, load
    from config_ui import DEFAULTS
    path=write_day(tmp_path/'raw','2026-08-20',[raw(1787220000)])
    task=worker(tmp_path);task.start();cell,aggregate=finish(task)
    aggregate._dirty=True
    sink=DerivedPersistenceWorker(cell,aggregate)
    sink.start()
    sink.submit(cell.persistence_payload(),aggregate.persistence_payload(),
                resume_context=context(task.options,tmp_path/'positions.jsonl',task.resume_sources))
    assert sink.stop(timeout=2)
    assert sink.persisted==1 and sink.last_error is None
    assert load(cell.path,aggregate.path,task.options,tmp_path/'positions.jsonl')['sources'][path.name]['size']==path.stat().st_size


def test_daily_restart_skips_raw_probe_but_detects_late_risk_source(tmp_path):
    from test_daily_diagnostic_worker import worker as daily_worker, write_index
    from daily_diagnostics import DailyDiagnosticSources
    root=tmp_path/'cells';root.mkdir()
    # This is outside the diagnostic day but inside its 14-day risk input.
    source=root/'2026-08-25.jsonl';source.write_text('{}\n')
    daily,harness=daily_worker(tmp_path)
    daily.sources=DailyDiagnosticSources(cell_history_root=root)
    index=write_index(daily.output_root,'2026-09-01','fp-2026-09-01')
    daily.check_once(); assert harness.probes==['2026-09-01']
    daily._save_state()
    restarted,other=daily_worker(tmp_path)
    restarted.sources=daily.sources
    restarted.check_once(); assert other.probes==[]
    source.write_text('{}\n{}\n')
    restarted.check_once(); assert other.probes==['2026-09-01']
