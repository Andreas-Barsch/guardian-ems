"""Explicit offline history-to-live handover; original methods remain the oracle."""
from copy import deepcopy
import pytest
from diagnostic_repository import DiagnosticRepository
from cell_diagnostics import CellDiagnosticStore,CellSample
from diagnostic_aggregates import DiagnosticAggregateStore
from test_diagnostic_repository import setup,raw,run,freeze_evaluation_clock
from test_canonical_repository import append_cells
from projection_repository import Conflict


@pytest.mark.parametrize('stage',[None,'before_handover_page_commit','after_handover_page_commit'])
def test_history_ring_handover_preserves_pair_and_original_next_live_add(tmp_path,stage):
    env=setup(tmp_path,maximum=3,mode='history')
    values=[raw(stamp,serial='SERIAL-A',delta=stamp) for stamp in [30,10,20,40,50]]
    append_cells(env,values);run(env)
    d=env['d'];old=d.state('test')['bundle']
    with d.view('test') as (view,meta):
        retained=list(view.cells.iter_all_samples());aggregate=deepcopy(dict(view.aggregates.records))
    reference=CellDiagnosticStore.in_memory(3);reference.replace_samples(retained)
    aggregates=DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases,30);aggregates.records=deepcopy(aggregate)
    assert d.begin_live_handover('test')['status']=='preparing'
    class Interrupted(Exception):pass
    hit=[]
    def fault(point):
        if point==stage and not hit:hit.append(point);raise Interrupted()
    for _ in range(100):
        try:d.finish('test',fault=fault)
        except Interrupted:
            d=DiagnosticRepository(env['repo'],env['catalog']);env['d']=d
        state=d.state('test')
        if state['ingestion']=='live' and not state['work']:break
    else:pytest.fail('bounded handover did not finish')
    if stage:assert hit
    with d.view('test') as (view,meta):
        assert meta['ingestion']=='live'
        assert list(view.cells.iter_all_samples())==retained
        assert dict(view.aggregates.records)==aggregate
    with d.view('test',bundle=old) as (view,meta):
        assert meta['ingestion']=='history' and list(view.cells.iter_all_samples())==retained
    new=raw(15,serial='SERIAL-A',delta=17)
    sample=CellSample(**{k:new[k] for k in CellSample.__dataclass_fields__})
    reference.add(sample);aggregates.add(sample,env['options'])
    append_cells(env,[new]);run(env)
    with d.view('test') as (view,meta):
        assert list(view.cells.iter_all_samples())==list(reference.iter_all_samples())
        assert dict(view.aggregates.records)==aggregates.records
        assert view.cells.analyse(1,env['options'],(),aggregates.for_identity(1,'SERIAL-A'))==reference.analyse(1,env['options'],(),aggregates.for_identity(1,'SERIAL-A'))
    assert d.begin_live_handover('test')['status']=='live'
