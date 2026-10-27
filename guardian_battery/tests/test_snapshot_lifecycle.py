class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import json
import pytest
from snapshot_lifecycle import SnapshotLifecycle
from test_publication_history_readers import setup
from projection_repository import Conflict


def drain(bridge):
    for _ in range(1000):
        if bridge.bind_step()['complete']:return
    pytest.fail('ownership bind unfinished')


@pytest.mark.parametrize('stage',['after_catalog_owner_commit','before_binding_commit','after_binding_commit'])
def test_real_snapshot_binding_resume_and_incremental_warm_start(tmp_path,stage):
    env,_,_,_=setup(tmp_path);bridge=SnapshotLifecycle(env['repo'],env['catalog']);bridge.initialize()
    def fault(point):
        if point==stage:raise InjectedCrash('power loss')
    with pytest.raises(InjectedCrash):bridge.bind_step(fault=fault)
    bridge=SnapshotLifecycle(env['repo'],env['catalog']);drain(bridge)
    assert bridge.bind_step()=={'complete':True,'rows':0}
    with env['repo']._connection() as db:
        bindings=list(db.execute('SELECT * FROM lifecycle_snapshot_binding'))
        assert bindings
    with env['catalog'].connection() as db:
        for binding in bindings:assert db.execute('SELECT status FROM window_owner WHERE snapshot=?',(binding['snapshot'],)).fetchone()[0]=='active'


@pytest.mark.parametrize('stage',['before_snapshot_release_commit','after_snapshot_release_commit','before_snapshot_ack_commit','after_snapshot_ack_commit'])
def test_handoff_crashes_release_only_selected_derived_snapshot(tmp_path,stage):
    env,_,_,_=setup(tmp_path);bridge=SnapshotLifecycle(env['repo'],env['catalog']);bridge.initialize();drain(bridge)
    with env['repo']._connection() as db:
        binding=dict(db.execute('SELECT * FROM lifecycle_snapshot_binding ORDER BY owner_table LIMIT 1').fetchone())
    table=binding['owner_table'];key=binding['owner_key']
    # Simulates the authorized terminal-owner commit, not a productive GC call.
    with env['repo']._write() as db:
        row=db.execute(f'SELECT * FROM {table} WHERE id=?',(json.loads(key)[0],)).fetchone()
        SnapshotLifecycle.enqueue(db,table,key,row)
        db.execute(f'DELETE FROM {table} WHERE id=?',(row['id'],))
    def fault(point):
        if point==stage:raise InjectedCrash('power loss')
    with pytest.raises(InjectedCrash):bridge.release_step(fault=fault)
    bridge=SnapshotLifecycle(env['repo'],env['catalog'])
    for _ in range(100):
        if bridge.release_step(limit=1)['complete']:break
    else:pytest.fail('release unfinished')
    with env['catalog'].connection() as db:
        assert not db.execute('SELECT 1 FROM window_snapshot WHERE id=?',(binding['snapshot'],)).fetchone()
        assert db.execute('SELECT count(*) FROM source').fetchone()[0]>0
    with env['repo']._connection() as db:assert db.execute("SELECT count(*) FROM lifecycle_snapshot_release WHERE status='done'").fetchone()[0]==1


def test_explicit_lost_request_retirement_preserves_sources_and_job_snapshots(tmp_path):
    from cell_source_resolver import CellSourceResolver
    from canonical_day_repository import bounds
    env,_,_,_=setup(tmp_path);bridge=SnapshotLifecycle(env['repo'],env['catalog']);bridge.initialize();drain(bridge)
    resolver=CellSourceResolver(env['catalog']);start,end=bounds(env['origin'])
    token=resolver.open_window(start,end,modules=(1,))
    refs=resolver.plan_snapshot(token)['refs']
    assert refs
    with env['catalog'].connection() as db:
        before=db.execute('SELECT count(*) FROM source').fetchone()[0]
        owned=db.execute("SELECT snapshot FROM window_owner WHERE owner NOT LIKE 'request:%' LIMIT 1").fetchone()[0]
    with pytest.raises(Conflict,match='durable job'):bridge.retire_request_step(owned,proof={'reason':'test','superseded_by':'test'})
    result=bridge.retire_request_step(token,proof={'reason':'request explicitly ended','superseded_by':'synthetic controller'},limit=1)
    while not result['complete']:result=bridge.retire_request_step(token,proof={'reason':'request explicitly ended','superseded_by':'synthetic controller'},limit=1)
    with pytest.raises(Conflict):resolver.read_refs(refs)
    with env['catalog'].connection() as db:assert db.execute('SELECT count(*) FROM source').fetchone()[0]==before
