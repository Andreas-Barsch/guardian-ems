import json
import pytest
from projection_repository import Conflict, Unavailable
from store_retirement import StoreRetirement
from test_store_retirement import drain_index, POLICY
from test_application_tail import setup, append, drain, bounds
from test_application_metadata import initial
from test_metadata_log_consumer import append as append_metadata
from test_history_epoch_adapter import position
from application_tail import ApplicationTail
from application_metadata import MetadataTail


class InjectedCrash(Exception):
    pass


def test_unacknowledged_preparation_is_protected_until_application_consumes_it(tmp_path):
    env, preparation, tail = setup(tmp_path)
    start, _ = bounds(env['origin'])
    append(env, start+10)
    for _ in range(500):
        preparation.step()
        with env['repo']._connection() as db:
            job=db.execute("SELECT id FROM preparation_job WHERE kind='registry' AND status='done' ORDER BY id DESC LIMIT 1").fetchone()
        if job:
            break
    assert job
    life=StoreRetirement(env['repo'])
    life.initialize()
    drain_index(life)
    life.begin('gc',policy=POLICY)
    with pytest.raises(Conflict,match='subscriber'):
        life.select('gc','preparation_job',[job[0]])
    drain(preparation,tail)
    life.select('gc','preparation_job',[job[0]])
    with env['repo']._connection() as db:
        assert not db.execute('SELECT 1 FROM application_tail_task LIMIT 1').fetchone()


def test_metadata_parent_remains_owned_across_publish_before_application_ack(tmp_path):
    registry,receipt,repo,tail=initial(tmp_path)
    life=StoreRetirement(repo)
    life.initialize()
    drain_index(life)
    append_metadata(registry,receipt,[position(200,'B')])
    def fault(stage):
        if stage=='after_metadata_publish_commit':
            raise InjectedCrash(stage)
    with pytest.raises(InjectedCrash):
        for _ in range(100):
            tail.step(fault=fault)
    with repo._connection() as db:
        parent=db.execute('SELECT parent FROM application_metadata').fetchone()[0]
        assert parent=='initial'
        assert db.execute("SELECT 1 FROM lifecycle_edge WHERE owner_table='application_metadata' AND target_table='metadata_revision' AND target_key=?",(json.dumps([parent],separators=(',',':')),)).fetchone()
    assert tail.step()['status']=='published'
    with repo._connection() as db:
        assert not db.execute("SELECT 1 FROM lifecycle_edge WHERE owner_table='application_metadata' AND target_table='metadata_revision'").fetchone()


def test_old_application_table_without_ownership_contract_is_not_silently_opened(tmp_path):
    env,preparation,tail=setup(tmp_path)
    with env['repo']._write() as db:
        db.execute('DROP TABLE application_tail_task')
    reopened=ApplicationTail(preparation,tail.diagnostics,tail.index,name='application')
    with pytest.raises(Unavailable,match='explicit migration'):
        reopened.step()
    assert preparation.status()['frontier'] is None


def test_completed_read_index_stays_owned_until_application_task_ack(tmp_path):
    env, preparation, tail = setup(tmp_path)
    start, _ = bounds(env['origin'])
    append(env, start+10)
    life = StoreRetirement(env['repo'])
    life.initialize()
    drain_index(life)
    selected = None

    def fault(stage):
        nonlocal selected
        if stage != 'after_tail_child_commit':
            return
        with env['repo']._connection() as db:
            row = db.execute('''SELECT t.bundle,t.product,t.partition_key FROM application_tail_task t
                JOIN application_tail a ON a.name=t.name AND a.position=t.position
                JOIN window_index_job w USING(bundle,product,partition_key)
                WHERE w.complete=1 LIMIT 1''').fetchone()
            if row:
                selected = list(row)
                raise InjectedCrash(stage)

    with pytest.raises(InjectedCrash):
        for _ in range(1000):
            preparation.step()
            tail.step(fault=fault)
    assert selected is not None
    from test_store_retirement import drain_fences, drain_validation
    life.begin('gc', policy=POLICY)
    with env['repo']._connection() as db:
        assert db.execute("SELECT 1 FROM lifecycle_edge WHERE owner_table='application_tail_task' AND target_table='window_index_job' AND target_key=?",
                          (json.dumps(selected, separators=(',', ':')),)).fetchone()
    life.select('gc', 'bundle', [selected[0]])
    life.select('gc', 'window_index_job', selected)
    drain_fences(life)
    with pytest.raises(Conflict, match='external owner'):
        drain_validation(life)
    while not life.abort_step('gc', limit=2)['complete']:
        pass
    resumed = ApplicationTail(preparation, tail.diagnostics, tail.index, name='application')
    drain(preparation, resumed)
    with env['repo']._connection() as db:
        assert not db.execute('SELECT 1 FROM application_tail_task LIMIT 1').fetchone()
        key = json.dumps(selected, separators=(',', ':'))
        assert not db.execute("SELECT 1 FROM lifecycle_edge WHERE owner_table='application_tail_task' AND target_key=?", (key,)).fetchone()


@pytest.mark.parametrize('name', ['application_tail_frontier','application_tail_target','window_index_ready_target'])
def test_missing_bounded_reference_index_is_not_silently_accepted(tmp_path, name):
    from publication_window_index import PublicationWindowIndex
    env, preparation, tail = setup(tmp_path)
    with env['repo']._write() as db:
        db.execute('DROP INDEX '+name)
    with pytest.raises(Unavailable,match='index .* requires explicit migration'):
        if name.startswith('window_'):
            PublicationWindowIndex(env['repo']).require_schema()
        else:
            ApplicationTail(preparation,tail.diagnostics,tail.index,name='application').state()
