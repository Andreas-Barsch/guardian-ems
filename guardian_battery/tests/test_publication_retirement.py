import json
import pytest
from test_publication_history_readers import setup
from test_canonical_repository import append_cells,sample
from test_history_repository_preparation import drain
from canonical_day_repository import bounds
from store_retirement import StoreRetirement
from snapshot_lifecycle import SnapshotLifecycle
from test_snapshot_lifecycle import drain as bind
from test_store_retirement import drain_index,POLICY
from test_diagnostic_retirement import select_rows,finish


def test_completed_old_jobs_release_without_destroying_retained_publications(tmp_path):
    env,preparation,index,readers=setup(tmp_path);repo=env['repo'];day=env['origin']
    with repo._connection() as db:
        old_canonical=[dict(r) for r in db.execute('SELECT * FROM canonical_day_job')]
        old_display=[dict(r) for r in db.execute('SELECT * FROM display_job')]
    # Real late correction supersedes the existing day publications.
    start,_=bounds(day);append_cells(env,[sample(start+150,current=1,soc=61)])
    preparation.request_correction('serial-a',day,reason='synthetic late observation',token='late')
    drain(preparation,limit=1000)
    bridge=SnapshotLifecycle(repo,env['catalog']);bridge.initialize();bind(bridge)
    life=StoreRetirement(repo);life.initialize();drain_index(life);life.begin('jobs',policy=POLICY)
    with repo._connection() as db:
        current_canonical=dict(db.execute('SELECT * FROM canonical_day_job ORDER BY revision DESC LIMIT 1').fetchone())
        current_display=db.execute('SELECT job FROM display_current WHERE day=?',(day,)).fetchone()[0]
        old_canonical=[r for r in old_canonical if r['id']!=current_canonical['id']]
        old_display=[r for r in old_display if r['id']!=current_display]
        display_ids={r['id'] for r in old_display}
        children={table:[dict(r) for r in db.execute('SELECT * FROM '+table) if r['job'] in display_ids] for table in ('display_bucket','display_cadence','display_replacement')}
        # Read current rows because original capture predates correction state changes.
        old_canonical=[dict(db.execute('SELECT * FROM canonical_day_job WHERE id=?',(r['id'],)).fetchone()) for r in old_canonical]
        old_display=[dict(db.execute('SELECT * FROM display_job WHERE id=?',(r['id'],)).fetchone()) for r in old_display]
    assert old_canonical and old_display
    # Completed correction audit owns old jobs; retire its exact group as well.
    with repo._connection() as db:
        corrections={r[0] for r in db.execute('SELECT id FROM canonical_correction WHERE finished=1')}
        correction_rows={t:[dict(r) for r in db.execute('SELECT * FROM '+t)] for t in ('canonical_correction','canonical_correction_done','canonical_correction_request','canonical_epoch_job')}
    for table,rows in correction_rows.items():
        selected=[r for r in rows if r['id' if table in ('canonical_correction','canonical_correction_request') else 'correction'] in corrections]
        select_rows(life,'jobs',table,selected)
    select_rows(life,'jobs','canonical_day_job',old_canonical);select_rows(life,'jobs','display_job',old_display)
    for table,rows in children.items():select_rows(life,'jobs',table,rows)
    finish(life,'jobs')
    while not bridge.release_step(limit=1)['complete']:pass
    with repo._connection() as db:
        assert db.execute('SELECT 1 FROM canonical_day_job WHERE id=?',(current_canonical['id'],)).fetchone()
        assert db.execute('SELECT 1 FROM display_job WHERE id=?',(current_display,)).fetchone()
    # Old immutable bundles and their indexes remain queryable for rollback.
    for old in old_canonical:
        assert env['project'].published_state('serial-a',day,old['bundle'])
