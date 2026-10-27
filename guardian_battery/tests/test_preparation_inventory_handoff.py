"""Controller 2c2 repros: durable catalog-to-queue ACK and actual bindings."""
import json
import multiprocessing
import os
from pathlib import Path

import pytest
from raw_registry import encoded
from test_canonical_repository import composed,bounds,sample
from test_history_repository_preparation import prep,drain

STAGES=['after_migration_commit','before_inventory_enqueue','after_inventory_enqueue',
        'before_inventory_queue_cursor','after_inventory_dispatch_commit']


def fixture(tmp_path,*,helpers=0,known_neighbor=False):
    e=composed(tmp_path);root=tmp_path/'legacy';root.mkdir();start,_=bounds(e['origin'])
    raw=root/'raw.jsonl';raw.write_bytes(encoded(sample(start+10))+b'\n')
    if known_neighbor:(root/'neighbor.jsonl').write_bytes(encoded(sample(start+30))+b'\n')
    for i in range(helpers):(root/f'helper{i}.idx').write_text('auxiliary')
    p=prep(e);job=p.request_inventory([(root,'legacy-local')])
    while True:
        with e['repo']._connection() as db:payload=json.loads(db.execute('SELECT payload FROM preparation_job WHERE id=?',(job,)).fetchone()[0])
        if payload['phase']=='ingest':break
        p.step()
    return e,p,root,raw,job,payload


def jobs(e,kind):
    with e['repo']._connection() as db:return [dict(r) for r in db.execute('SELECT * FROM preparation_job WHERE kind=? ORDER BY id',(kind,))]


def source(e,path):return next(s for s in e['catalog'].registered() if s['path']==str(path))


@pytest.mark.parametrize('stage',STAGES)
@pytest.mark.parametrize('process_exit',[False,True])
def test_committed_inventory_page_handoff_survives_all_windows(tmp_path,stage,process_exit):
    e,p,root,raw,job,payload=fixture(tmp_path)
    before=raw.read_bytes()
    def interrupt():
        def fault(s):
            if s==stage:
                if process_exit:os._exit(73)
                raise RuntimeError('injected handoff interruption')
        prep(e).step(fault=fault)
    if process_exit:
        child=multiprocessing.get_context('fork').Process(target=interrupt);child.start();child.join(10)
        if child.is_alive():child.kill();child.join();pytest.fail('process fixture timed out')
        assert child.exitcode==73
    else:
        with pytest.raises(RuntimeError,match='handoff'):interrupt()
    progress=e['catalog'].migration_progress('inventory:'+payload['inventory'])
    assert progress['cursor']==1
    queued=jobs(e,'catalog_index')
    assert len(queued)==(1 if stage=='after_inventory_dispatch_commit' else 0)
    p=prep(e);drain(p)
    current=source(e,raw)
    assert current['cursor']==current['frontier']==len(before) and current['records']==1
    queued=jobs(e,'catalog_index');assert len(queued)==1 and queued[0]['status']=='done'
    binding=json.loads(queued[0]['payload'])['binding']
    assert binding['catalog_revision']==current['revision']
    assert p.canonical.published_state('serial-a',e['origin'])['metadata']['processed']==1
    assert p.status()['state']=='caught_up'
    assert raw.read_bytes()==before


def test_finished_catalog_still_replays_unacknowledged_jobs(tmp_path):
    e,p,root,raw,job,payload=fixture(tmp_path,helpers=40)
    cursor=after=0
    while True:
        page=p.inventory.page(payload['inventory'],after_id=after,limit=32)
        e['catalog'].ingest_page('inventory:'+payload['inventory'],expected_cursor=cursor,
                                items=[r['item'] for r in page],finish=not page)
        if not page:break
        cursor+=len(page);after=page[-1]['id']
    assert cursor==41 and not jobs(e,'catalog_index')
    p=prep(e);drain(p)
    assert len(jobs(e,'catalog_index'))==1
    assert source(e,raw)['records']==1
    with e['repo']._connection() as db:
        done=json.loads(db.execute('SELECT payload FROM preparation_job WHERE id=?',(job,)).fetchone()[0])
    assert done['cursor']==41 and done['after']==after


def test_reinventory_confirmed_append_uses_bound_revision_and_frontier(tmp_path):
    e,p,root,raw,job,payload=fixture(tmp_path);drain(p);first=source(e,raw)
    start,_=bounds(e['origin'])
    with raw.open('ab') as h:h.write(encoded(sample(start+20))+b'\n')
    e['catalog'].confirm_append(first['source'],first['revision'],expected_receipt=first['receipt'],
        receipt='confirmed-append',frontier=raw.stat().st_size,writer_epoch=first['writer_epoch'])
    p.request_inventory([(root,'legacy-local')]);p=prep(e);drain(p)
    current=source(e,raw);index_jobs=jobs(e,'catalog_index');assert len(index_jobs)==2
    latest=json.loads(index_jobs[-1]['payload']);binding=latest['binding']
    assert binding['observed_revision']!=binding['catalog_revision']==first['revision']==latest['revision']
    assert binding['receipt']=='confirmed-append' and binding['frontier']==raw.stat().st_size
    assert current['records']==2 and current['cursor']==current['frontier']
    assert p.canonical.published_state('serial-a',e['origin'])['metadata']['processed']==2
    assert p.status()['state']=='caught_up'


def test_pending_observation_does_not_index_candidate_and_keeps_neighbors_running(tmp_path):
    e,p,root,raw,job,payload=fixture(tmp_path,known_neighbor=True);drain(p);old=source(e,raw)
    start,_=bounds(e['origin']);known=root/'neighbor.jsonl';known_before=source(e,known)
    neighbor=root/'added.jsonl';neighbor.write_bytes(encoded(sample(start+40))+b'\n')
    with raw.open('ab') as h:h.write(encoded(sample(start+20))+b'\n')
    p.request_inventory([(root,'legacy-local')])
    for _ in range(300):
        p.step()
        if p.status()['job'] is None:break
    else:pytest.fail('neighbor jobs did not finish')
    pending=jobs(e,'inventory_decision');assert len(pending)==1
    decision=json.loads(pending[0]['payload'])['binding']
    assert pending[0]['status']=='blocked' and decision['outcome']=='needs_receipt_or_revision'
    assert decision['catalog_revision']==old['revision'] and decision['frontier']==old['frontier']
    recent=[json.loads(j['payload']) for j in jobs(e,'catalog_index') if json.loads(j['payload'])['binding']['inventory']==decision['inventory']]
    assert len(recent)==2 and {v['source'] for v in recent}=={source(e,neighbor)['source'],known_before['source']}
    assert source(e,known)==known_before
    assert {v['binding']['outcome'] for v in recent}=={'registered','known_prefix'}
    assert source(e,neighbor)['cursor']==source(e,neighbor)['frontier'] and source(e,neighbor)['records']==1
    assert source(e,raw)['cursor']==old['cursor'] and source(e,raw)['frontier']==old['frontier']
    assert p.status()['state']=='blocked'


def test_bound_prefix_is_not_widened_by_later_confirmation(tmp_path):
    e,p,root,raw,job,payload=fixture(tmp_path)
    p.step()  # Catalog and dispatch ACK bind only the first record.
    original=source(e,raw);start,_=bounds(e['origin'])
    with raw.open('ab') as h:h.write(encoded(sample(start+20))+b'\n')
    e['catalog'].confirm_append(original['source'],original['revision'],expected_receipt=original['receipt'],
        receipt='later-confirmation',frontier=raw.stat().st_size,writer_epoch=original['writer_epoch'])
    # Execute just the inventory/index tasks; later derived work is not this test.
    for _ in range(10):
        p.step()
        if jobs(e,'catalog_index')[0]['status']=='done':break
    indexed=source(e,raw)
    assert indexed['cursor']==original['frontier']<indexed['frontier'] and indexed['records']==1
    ack=json.loads(jobs(e,'catalog_index')[0]['payload'])
    assert ack['binding']['receipt']==original['receipt'] and ack['binding']['frontier']==original['frontier']


def test_known_unconfirmed_tail_indexes_only_bound_prefix_and_stays_visible(tmp_path):
    e,p,root,raw,job,payload=fixture(tmp_path);drain(p);old=source(e,raw);start,_=bounds(e['origin'])
    with raw.open('ab') as h:h.write(encoded(sample(start+20))+b'\n')
    # Explicitly confirmed metadata with the unchanged known durable prefix;
    # the newly observed tail is deliberately NOT confirmed.
    e['catalog'].confirm_append(old['source'],old['revision'],expected_receipt=old['receipt'],
        receipt='prefix-only',frontier=old['frontier'],writer_epoch=old['writer_epoch'])
    p.request_inventory([(root,'legacy-local')])
    for _ in range(200):
        p.step()
        if p.status()['job'] is None:break
    decision=json.loads(jobs(e,'inventory_decision')[0]['payload'])['binding']
    assert decision['outcome']=='known_prefix_unconfirmed_tail'
    latest=json.loads(jobs(e,'catalog_index')[-1]['payload'])
    assert latest['binding']['frontier']==old['frontier'] and latest['revision']==old['revision']
    assert source(e,raw)['records']==1 and source(e,raw)['cursor']==old['frontier']
    assert p.status()['state']=='blocked'
