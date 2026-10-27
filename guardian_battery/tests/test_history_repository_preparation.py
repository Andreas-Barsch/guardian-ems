import pytest
from test_canonical_repository import composed,append_cells,bounds,sample,following
from display_day_repository import DisplayDayRepository
from history_repository_preparation import RepositoryPreparation,local_composition


def prep(env):
    d=DisplayDayRepository(env['repo'],env['catalog'],env['project']);d.initialize()
    p=RepositoryPreparation(env['repo'],env['catalog'],env['epochs'],env['project'],d);p.initialize();return p


def drain(p,limit=600):
    for _ in range(limit):
        state=p.status()
        if state['state']=='caught_up':return
        assert state['state']!='blocked',state
        p.step()
    raise AssertionError('preparation not finished: '+str(p.status()))


def test_daily_dispatch_restart_and_warm_no_raw(tmp_path,monkeypatch):
    e=composed(tmp_path);day=e['origin'];start,end=bounds(day)
    append_cells(e,[sample(start+10),sample(end+10)])
    p=prep(e);p.enroll('serial-a');p.confirm_frontier(following(day),proof={'fixture_receipt':'confirmed'})
    for _ in range(4):p.step()
    p=prep(e);drain(p)
    assert p.canonical.published_state('serial-a',day)['metadata']['status']=='published_closed'
    assert p.display.state(day)['status']=='published_closed'
    assert p.display.state(following(day))['status']=='caught_up'
    p=local_composition(repository_path=e['repo'].path,catalog_path=e['catalog'].path,epoch_path=e['epochs'].path)
    monkeypatch.setattr(p.canonical.reader,'page',lambda *a,**kw:pytest.fail('warm raw read'))
    monkeypatch.setattr(p.display.reader,'page',lambda *a,**kw:pytest.fail('warm display read'))
    assert p.status()['state']=='caught_up'
    assert p.step()['raw_bytes']==0


def test_frontier_closes_day_without_late_record(tmp_path):
    e=composed(tmp_path);day=e['origin'];start,end=bounds(day);append_cells(e,[sample(start+10)])
    p=prep(e);p.enroll('serial-a');p.confirm_frontier(day,proof={'receipt':1});drain(p)
    old=p.canonical.published_state('serial-a',day)['bundle']
    p.confirm_frontier(following(day),proof={'receipt':2,'confirmed_time_frontier':end});drain(p)
    assert p.canonical.published_state('serial-a',day)['metadata']['status']=='published_closed'
    assert p.canonical.published_state('serial-a',day)['bundle']!=old
    assert p.display.state(day)['status']=='published_closed'


def test_pause_resume_retains_published_data(tmp_path):
    e=composed(tmp_path);day=e['origin'];start,_=bounds(day);append_cells(e,[sample(start+10)])
    p=prep(e);p.enroll('serial-a');p.confirm_frontier(day,proof={'receipt':1});p.step();p.pause()
    q=prep(e);assert q.step()['status']=='paused'
    q.resume();drain(q);old=q.display.repository.head(q.display.head(day));q.pause()
    assert q.display.repository.head(q.display.head(day))==old


def test_missing_predecessor_never_bootstraps_neutrally(tmp_path):
    e=composed(tmp_path);day=e['origin'];p=prep(e);p.enroll('serial-a')
    with e['repo']._write() as db:db.execute('UPDATE preparation_stream SET next_day=?',(following(day),))
    p.confirm_frontier(following(day),proof={'receipt':1})
    for _ in range(30):
        p.step()
        if p.status()['state']=='blocked':break
    assert p.status()['state']=='blocked'
    assert 'awaiting_predecessor' in p.status()['blocked']['error']
    assert e['project'].published_state('serial-a',following(day)) is None


def test_registry_drives_confirmed_append_and_late_correction(tmp_path):
    from raw_registry import encoded,hashed
    e=composed(tmp_path);day=e['origin'];start,end=bounds(day)
    p=prep(e);p.watch_registry(e['rawreg'].root)
    receipt=e['rawreceipt']
    def append(values):
        nonlocal receipt
        receipt=e['rawreg'].append('raw',b''.join(encoded(v)+b'\n' for v in values),expected_receipt=hashed(encoded(receipt)),operation='write-'+str(receipt['seq']))
    def poll_drain():
        for _ in range(300):
            p.step()
            assert p.status()['state']!='blocked',p.status()
            if p.status()['state']=='caught_up' and not p._poll_registry():return
        raise AssertionError('not done')
    append([sample(start+20)]);poll_drain()
    assert p.canonical.published_state('serial-a',day)['metadata']['processed']==1
    append([sample(start+10)]);poll_drain()
    assert p.canonical.published_state('serial-a',day)['metadata']['processed']==2
    assert not p.display.read_page(day)['validity']['stale_dependencies']
    append([sample(end+10)]);poll_drain()
    assert p.canonical.published_state('serial-a',day)['metadata']['status']=='published_closed'


def test_inventory_restart_and_no_warm_enumeration(tmp_path,monkeypatch):
    import json
    e=composed(tmp_path);root=tmp_path/'legacy';root.mkdir()
    # Unknown provenance stays explicit; this fixture does not invent a receipt.
    for i in range(40):(root/(str(i)+'.txt')).write_text('unrelated')
    p=prep(e);job=p.request_inventory([(root,'legacy-local')]);p.step()
    p=prep(e);drain(p)
    monkeypatch.setattr(p.inventory,'step',lambda *a,**k:pytest.fail('warm inventory'))
    assert p.step()['raw_bytes']==0


def test_new_unknown_serial_is_blocked_without_guessed_origin(tmp_path):
    from raw_registry import encoded,hashed
    e=composed(tmp_path);day=e['origin'];start,_=bounds(day);p=prep(e);p.watch_registry(e['rawreg'].root)
    r=e['rawreceipt'];value=dict(sample(start+10),module_serial='UNKNOWN')
    e['rawreg'].append('raw',encoded(value)+b'\n',expected_receipt=hashed(encoded(r)),operation='unknown')
    for _ in range(30):p.step()
    assert p.status()['state']=='blocked'
    assert 'explicit physical identity origin' in p.status()['blocked']['error']


@pytest.mark.parametrize('closed',[False,True])
def test_epoch_dispatch_recalculates_with_real_engine(tmp_path,closed):
    from test_history_epoch_adapter import config,add,drain as epoch_drain
    from test_canonical_phase import PARAMS
    e=composed(tmp_path);day=e['origin'];start,end=bounds(day);append_cells(e,[sample(start+10),sample(start+30)])
    p=prep(e);p.confirm_frontier(following(day) if closed else day,proof={'receipt':1});drain(p)
    old=p.canonical.published_state('serial-a',day)['bundle']
    add(e['epochreg'],e['epoch_receipt'],[dict(config(start+20,12),parameters=PARAMS,config_id='retro')]);epoch_drain(e['epoch_reader'])
    p.step()  # Idle metadata poll discovers the already-ingested epoch change.
    drain(p)
    new=p.canonical.published_state('serial-a',day)
    assert new['bundle']!=old
    assert new['checkpoint']['engine']['config_revision']=='retro'
    assert not p.display.read_page(day)['validity']['stale_dependencies']


def test_audit_one_raw_plus_512_helpers_is_explicit_migration_then_warm(tmp_path,monkeypatch):
    import hashlib
    from raw_registry import encoded
    from history_preparation import create_local_repository_preparation
    from diagnostic_startup import RepositoryStartup
    from diagnostic_startup_merge import repository_predecessor
    from canonical_window_store import repository_page as canonical_page
    from display_window_store import repository_page as display_page
    e=composed(tmp_path);day=e['origin'];start,end=bounds(day);root=tmp_path/'audit-source';root.mkdir()
    raw=root/'arbitrary-local-name.jsonl';raw.write_bytes(encoded(sample(start+10))+b'\n')
    original=hashlib.sha256(raw.read_bytes()).hexdigest()
    for i in range(512):(root/(str(i)+'.idx')).write_text('helper')
    p=prep(e);p.request_inventory([(root,'legacy-local')])
    for _ in range(4):p.step()
    p=create_local_repository_preparation(repository_path=e['repo'].path,catalog_path=e['catalog'].path,epoch_path=e['epochs'].path)
    startup=RepositoryStartup(p,gate=False)
    for _ in range(300):
        if p.status()['state']=='caught_up':break
        assert p.status()['state']!='blocked',p.status()
        startup.step()
    assert p.status()['state']=='caught_up'
    assert p.catalog.status()['helpers']==512
    assert p.canonical.published_state('serial-a',day)['metadata']['processed']==1
    assert len(display_page(p.display,day,limit=1)['rows'])==1
    assert canonical_page(p.canonical,'serial-a',day,limit=1)['rows']
    assert repository_predecessor(p.canonical,'serial-a',day)['checkpoint']['processed']==1
    assert not startup.status()['legacy_cache_pair_ready']
    assert hashlib.sha256(raw.read_bytes()).hexdigest()==original
    monkeypatch.setattr(p.inventory,'step',lambda *a,**k:pytest.fail('warm directory scan'))
    monkeypatch.setattr(p.canonical.reader,'page',lambda *a,**k:pytest.fail('warm raw scan'))
    assert startup.step()['raw_bytes']==0


def test_factory_missing_checkpoint_requires_explicit_initialization(tmp_path):
    from projection_repository import Unavailable
    with pytest.raises(Unavailable,match='explicit initialization'):
        local_composition(repository_path=tmp_path/'p.db',catalog_path=tmp_path/'c.db',epoch_path=tmp_path/'e.db')
    assert not list(tmp_path.iterdir())


def test_correction_dispatch_retry_has_one_durable_identity(tmp_path):
    from test_canonical_repository import close_day
    e=composed(tmp_path);day=e['origin'];start,_=bounds(day);append_cells(e,[sample(start+10)]);close_day(e,day,bootstrap=True)
    p=prep(e);p.request_correction('serial-a',day,reason='retry',token='stable-test')
    def fault(stage):
        if stage=='after_preparation_cursor':raise RuntimeError('lost cursor transaction')
    with pytest.raises(RuntimeError):p.step(fault=fault)
    p=prep(e);p.step()
    with e['repo']._connection() as db:
        assert db.execute("SELECT count(*) FROM canonical_correction WHERE reason='retry'").fetchone()[0]==1


def test_epoch_change_cursor_and_dependency_cursor_rollback_together(tmp_path):
    e=composed(tmp_path);p=prep(e);p.request_epoch_dispatch()
    # Initial change has no publications to dispatch. First step plans it;
    # second acknowledges its empty job page and must atomically clear cursor.
    p.step()
    with e['repo']._connection() as db:before=db.execute('SELECT epoch_after FROM preparation_state WHERE id=1').fetchone()[0]
    def fault(stage):
        if stage=='after_preparation_cursor':raise RuntimeError('crash before commit')
    with pytest.raises(RuntimeError):p.step(fault=fault)
    with e['repo']._connection() as db:assert db.execute('SELECT epoch_after FROM preparation_state WHERE id=1').fetchone()[0]==before
    p=prep(e);drain(p)


def test_registered_revision_dispatch_preserves_old_publication(tmp_path):
    from raw_registry import encoded,hashed
    e=composed(tmp_path);day=e['origin'];start,end=bounds(day)
    append_cells(e,[sample(start+10),sample(end+10)])
    p=prep(e);p.watch_registry(e['rawreg'].root);p.confirm_frontier(following(day),proof={'receipt':'initial'});drain(p)
    old=p.canonical.published_state('serial-a',day)
    from pathlib import Path
    newfile=Path(e['rawreceipt']['path']);newfile.write_bytes(b'')  # Synthetic same-inode rewrite.
    receipt=e['rawreg'].register('raw','r2',newfile,writer_epoch='replacement',namespace='utc',operation='replace',expected_receipt=hashed(encoded(e['rawreceipt'])))
    e['rawreg'].append('raw',encoded(sample(end+20))+b'\n',expected_receipt=hashed(encoded(receipt)),operation='replacement-record')
    for _ in range(400):
        p.step()
        assert p.status()['state']!='blocked',p.status()
        if p.status()['state']=='caught_up' and not p._poll_registry():break
    else:raise AssertionError('not caught up')
    assert p.canonical.published_state('serial-a',day)['metadata']['processed']==0
    assert p.canonical.published_state('serial-a',day,old['bundle'])==old


def test_one_preparation_unit_owner_nonblocking(tmp_path):
    import fcntl
    e=composed(tmp_path);p=prep(e)
    lock=e['repo'].path.with_name(e['repo'].path.name+'.preparation.lock')
    with lock.open('a+b') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert p.step()=={'status':'busy','raw_bytes':0,'decodes':0}
    assert p.step()['raw_bytes']==0
