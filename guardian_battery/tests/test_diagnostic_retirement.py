import json
import pytest
from test_diagnostic_repository import setup,raw,run,append_cells
from store_retirement import StoreRetirement
from projection_repository import Conflict
from test_store_retirement import drain_index,POLICY


def finish(life,plan):
    for _ in range(10000):
        if life.fence_step(plan,limit=8)['complete']:break
    else:pytest.fail('fence not finished')
    for _ in range(10000):
        if life.validate_step(plan,limit=8)['complete']:break
    else:pytest.fail('validation not finished')
    for _ in range(10000):
        result=life.delete_step(plan,limit=8)
        assert result['examined']<=8 and life.last_sql_vm_steps<200000
        if result['complete']:return
    pytest.fail('retirement not finished')


def select_rows(life,plan,table,rows):
    with life.repository._connection() as db:keys=json.loads(db.execute('SELECT keys FROM lifecycle_layout WHERE name=?',(table,)).fetchone()[0])
    for row in rows:life.select(plan,table,[row[k] for k in keys])


def test_many_actual_history_revisions_release_retired_slots_samples_and_maps(tmp_path):
    env=setup(tmp_path,maximum=4,mode='history')
    records=[raw(i%4+1,serial='A' if i%2 else 'B',delta=i%3) for i in range(36)]
    append_cells(env,records);run(env)
    d=env['d'];repo=env['repo'];head=d.state('test')['bundle']
    with d.view('test') as (view,_):before=list(view.cells.iter_all_samples())
    life=StoreRetirement(repo);life.initialize();drain_index(life)
    life.begin('publications',policy=POLICY)
    with repo._connection() as db:
        old_bundles=[r[0] for r in db.execute('SELECT id FROM bundle WHERE id!=?',(head,))]
        current_pubs={r[0] for r in db.execute('SELECT publication_id FROM member WHERE bundle_id=?',(head,))}
        rows={t:[dict(r) for r in db.execute('SELECT * FROM '+t)] for t in ('bundle','member','bundle_cursor','publication')}
    select_rows(life,'publications','bundle',[r for r in rows['bundle'] if r['id'] in old_bundles])
    for table in ('member','bundle_cursor'):select_rows(life,'publications',table,[r for r in rows[table] if r['bundle_id'] in old_bundles])
    select_rows(life,'publications','publication',[r for r in rows['publication'] if r['id'] not in current_pubs])
    finish(life,'publications')
    life.begin('versions',policy=POLICY)
    with repo._connection() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM diagnostic_row WHERE retired IS NOT NULL')]
        original_count=db.execute('SELECT count(*) FROM diagnostic_row').fetchone()[0]
    assert len(rows)>100
    select_rows(life,'versions','diagnostic_row',rows)
    finish(life,'versions')
    with repo._connection() as db:
        after_count=db.execute('SELECT count(*) FROM diagnostic_row').fetchone()[0]
        assert after_count==original_count-len(rows)
        assert not db.execute('SELECT 1 FROM diagnostic_row WHERE retired IS NOT NULL').fetchone()
        current=dict(db.execute('SELECT * FROM diagnostic_row LIMIT 1').fetchone())
    with d.view('test') as (view,_):assert list(view.cells.iter_all_samples())==before
    life.begin('forbidden',policy=POLICY)
    with pytest.raises(Conflict,match='current logical'):select_rows(life,'forbidden','diagnostic_row',[current])
    # Further real ingestion must use the surviving current slot pointers.
    append_cells(env,[raw(3,serial='C',delta=1)]);run(env)
    with d.view('test') as (view,_):assert any(s['module_serial']=='C' for s in view.cells.iter_all_samples())


def test_complete_analysis_result_and_statistics_retirement_preserves_current(tmp_path):
    from test_diagnostic_repository import drain_analysis
    env=setup(tmp_path,maximum=8);append_cells(env,[raw(i) for i in range(1,9)]);run(env)
    repo=env['repo'];d=env['d'];job=d.begin_analysis('test',[1]);d.analysis_step(job)
    life=StoreRetirement(repo);life.initialize();drain_index(life);life.begin('active',policy=POLICY)
    with pytest.raises(Conflict,match='active analysis'):life.select('active','diagnostic_analysis_job',[job])
    with repo._connection() as db:caches=[dict(r) for r in db.execute('SELECT * FROM diagnostic_condition_cache')]
    assert caches
    with pytest.raises(Conflict,match='active analysis'):select_rows(life,'active','diagnostic_condition_cache',caches[:1])
    while not life.abort_step('active')['complete']:pass
    drain_analysis(d,job);expected=d.analysis_result(job)
    old=d.analysis_state(job)['bundle'];append_cells(env,[raw(9)]);run(env)
    life.begin('analysis',policy=POLICY)
    with repo._connection() as db:
        rows={table:[dict(r) for r in db.execute('SELECT * FROM '+table)] for table in ('bundle','member','bundle_cursor','publication','pin','diagnostic_analysis_job','diagnostic_analysis_result')}
    pubs={row['publication_id'] for row in rows['member'] if row['bundle_id']==old}
    for table in ('bundle','member','bundle_cursor','publication','pin','diagnostic_analysis_job','diagnostic_analysis_result'):
        if table=='bundle':selected=[r for r in rows[table] if r['id']==old]
        elif table=='publication':selected=[r for r in rows[table] if r['id'] in pubs]
        elif table=='diagnostic_analysis_job':selected=[r for r in rows[table] if r['id']==job]
        elif table=='diagnostic_analysis_result':selected=[r for r in rows[table] if r['job']==job]
        else:selected=[r for r in rows[table] if r['bundle_id']==old]
        select_rows(life,'analysis',table,selected)
    assert d.analysis_result(job)==expected
    finish(life,'analysis')
    life.begin('statistics',policy=POLICY);select_rows(life,'statistics','diagnostic_condition_cache',caches);finish(life,'statistics')
    with repo._connection() as db:
        assert not db.execute('SELECT 1 FROM diagnostic_analysis_result WHERE job=?',(job,)).fetchone()
        assert not db.execute('SELECT 1 FROM pin WHERE token=?',('diagnostic-analysis:'+job,)).fetchone()
    # A new analysis recomputes the selected contributions and remains correct.
    next_job=d.begin_analysis('test',[1]);drain_analysis(d,next_job)
    assert d.analysis_result(next_job)
