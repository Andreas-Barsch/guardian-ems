"""Actual preparation -> pair/publication-index pipeline; synthetic files only."""
import pytest
from application_tail import ApplicationTail
from diagnostic_repository import DiagnosticRepository
from publication_window_index import PublicationWindowIndex
from config_ui import DEFAULTS
from test_canonical_repository import composed, sample, bounds
from test_history_repository_preparation import prep
from raw_registry import encoded, hashed


class InjectedCrash(Exception):
    pass


def setup(tmp_path):
    env = composed(tmp_path)
    preparation = prep(env)
    preparation.watch_registry(env['rawreg'].root)
    diagnostics = DiagnosticRepository(env['repo'], env['catalog'])
    diagnostics.initialize()
    diagnostics.create('application', DEFAULTS, ingestion='live')
    index = PublicationWindowIndex(env['repo'])
    index.initialize()
    tail = ApplicationTail(preparation, diagnostics, index, name='application')
    tail.initialize()
    return env, preparation, tail


def append(env, stamp):
    receipt = env['rawreg'].state('raw')
    return env['rawreg'].append('raw', encoded(sample(stamp)) + b'\n',
        expected_receipt=hashed(encoded(receipt)), operation='sample:' + str(stamp))


def drain(preparation, tail):
    idle = 0
    for _ in range(1500):
        preparation.step()
        result = tail.step()
        assert preparation.status()['state'] != 'blocked', preparation.status()
        idle = idle + 1 if result.get('status') == 'caught_up' and preparation.status()['state'] == 'caught_up' else 0
        if idle == 2:
            return
    pytest.fail('application pipeline not caught up')


def test_confirmed_writer_receipt_reaches_pair_and_actual_read_index(tmp_path):
    env, preparation, tail = setup(tmp_path)
    start, _ = bounds(env['origin'])
    append(env, start + 10)
    drain(preparation, tail)
    with tail.diagnostics.view('application') as (view, _):
        assert view.cells.values_for_module(1)[-1]['timestamp'] == start + 10
    day = env['origin']
    bundle = env['repo'].head(preparation.display.head(day))
    assert tail.index.state(bundle, preparation.display.product(day), day)['complete']
    old = tail.state()['after_job']
    reopened = ApplicationTail(preparation, tail.diagnostics, tail.index, name='application')
    assert reopened.step()['raw_bytes'] == 0
    assert reopened.state()['after_job'] == old
    append(env, start + 20)
    drain(preparation, reopened)
    with reopened.diagnostics.view('application') as (view, _):
        assert [row['timestamp'] for row in view.cells.values_for_module(1)] == [start + 10, start + 20]


@pytest.mark.parametrize('stage', ['after_tail_dispatch', 'after_tail_child_commit', 'after_tail_ack'])
def test_child_commit_before_ack_survives_restart(tmp_path, stage):
    env, preparation, tail = setup(tmp_path)
    start, _ = bounds(env['origin'])
    append(env, start + 10)
    seen = []
    def fault(point):
        if point == stage:
            seen.append(point)
            raise InjectedCrash(point)
    with pytest.raises(InjectedCrash):
        for _ in range(1000):
            preparation.step()
            tail.step(fault=fault)
    assert seen
    resumed = ApplicationTail(preparation, tail.diagnostics, tail.index, name='application')
    drain(preparation, resumed)
    with resumed.diagnostics.view('application') as (view, _):
        assert len(view.cells.values_for_module(1)) == 1
