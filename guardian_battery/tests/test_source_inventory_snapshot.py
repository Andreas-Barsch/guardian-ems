"""Phase2b2: explicit discovery, frozen prefixes and bounded grouped raw reads."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from projection_repository import Conflict
from source_catalog import SourceCatalog
from cell_source_resolver import CellSourceResolver
from legacy_source_inventory import LegacySourceInventory
from test_source_catalog import catalog,record,write,registration,index_all


def finish_inventory(discovery,job,page=61):
    calls=[]
    while True:
        result=discovery.step(job,max_entries=page);calls.append(result)
        assert result['examined']<=page
        if result['status']=='complete':break
        assert result['status']=='private'
    discovery.close()
    return calls


def inventory_fixture(tmp_path):
    root=tmp_path/'legacy';root.mkdir()
    for i in range(601):(root/f'{i}.idx').write_text('helper')
    raw=write(root/'2026-09-20.jsonl',[record(1790000000+i) for i in range(6)])
    return root,raw


def test_discovery_resume_scope_and_ingestion(catalog,tmp_path):
    root,raw=inventory_fixture(tmp_path)
    utc=tmp_path/'utc';utc.mkdir();other=write(utc/'wrong-day.jsonl',[record(1790000001,serial='other')])
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw,other)}
    d=LegacySourceInventory(catalog);job=d.create([(root,'legacy-local'),(utc,'utc')])
    for _ in range(3):d.step(job,max_entries=31)
    with pytest.raises(Conflict):d.page(job)
    d.close();d=LegacySourceInventory(SourceCatalog(catalog.path))
    calls=finish_inventory(d,job,31)
    assert sum(x.get('replayed',0) for x in calls)>=93
    state=d.status(job);assert (state['sources'],state['helpers'],state['unknown'])==(2,601,0)
    after=cursor=0;ids=[]
    while True:
        page=d.page(job,after_id=after,limit=47)
        if not page:break
        assert len(page)<=47
        ids.extend(r['id'] for r in page)
        cursor=catalog.ingest_page('inventory:'+job,expected_cursor=cursor,items=[r['item'] for r in page])
        after=page[-1]['id']
    catalog.ingest_page('inventory:'+job,expected_cursor=cursor,items=[],finish=True)
    assert len(ids)==len(set(ids))==603
    assert catalog.status()['sources']==2 and catalog.status()['helpers']==601
    for source in catalog.registered():index_all(catalog,source['source'],source['revision'])
    resolver=CellSourceResolver(catalog);snapshot=resolver.open_window(1790000000,1790000010)
    refs=resolver.plan_snapshot(snapshot)['refs']
    assert len(refs)==7 and {r.storage_convention for r in refs}=={'legacy-local','utc'}
    assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (raw,other)}


@pytest.mark.parametrize('stage',['after_discovery_entries','after_discovery_checkpoint','after_discovery_commit'])
def test_discovery_process_crash(catalog,tmp_path,stage):
    root,_=inventory_fixture(tmp_path)
    d=LegacySourceInventory(catalog);job=d.create([(root,'legacy-local')])
    code="""
import os,sys
from source_catalog import SourceCatalog
from legacy_source_inventory import LegacySourceInventory
d=LegacySourceInventory(SourceCatalog(sys.argv[1]))
d.step(sys.argv[2],max_entries=37,fault=lambda at:os._exit(83) if at==sys.argv[3] else None)
"""
    child=subprocess.run([sys.executable,'-c',code,str(catalog.path),job,stage],capture_output=True,timeout=15)
    assert child.returncode==83,child.stderr.decode()
    state=d.status(job)
    assert state['sources']+state['helpers']==(37 if stage=='after_discovery_commit' else 0)
    calls=finish_inventory(d,job,37)
    state=d.status(job)
    assert (state['sources'],state['helpers'])==(1,601)
    assert sum(c['new'] for c in calls)==(565 if stage=='after_discovery_commit' else 602)


def test_discovery_unknown_symlink_subdirectory_and_changed_root(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();(root/'nested').mkdir()
    outside=write(tmp_path/'outside.jsonl',[record(1)])
    (root/'link.jsonl').symlink_to(outside)
    d=LegacySourceInventory(catalog);job=d.create([(root,'legacy-local')]);finish_inventory(d,job)
    assert d.status(job)['unknown']==2 and d.status(job)['sources']==0
    assert {r['item']['kind'] for r in d.page(job)}=={'unknown'}
    job2=d.create([(root,'legacy-local')]);d.step(job2,max_entries=1)
    (root/'new.idx').write_text('new')
    assert d.step(job2)['status']=='scope_changed'
    with pytest.raises(Conflict):d.page(job2)


def test_observed_inventory_does_not_silently_adopt_changed_raw(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();path=write(root/'raw.jsonl',[record(1)])
    d=LegacySourceInventory(catalog);job=d.create([(root,'legacy-local')]);finish_inventory(d,job)
    item=d.page(job)[0]['item']
    with path.open('ab') as h:h.write(json.dumps(record(2)).encode()+b'\n')
    with pytest.raises(Conflict,match='observation'):
        catalog.ingest_page('manifest',expected_cursor=0,items=[item])
    assert catalog.status()['sources']==0


def append(c,path,values,*,old='receipt1',new='receipt2',source='s'):
    with path.open('ab') as h:
        for value in values:h.write(json.dumps(value,separators=(',',':')).encode()+b'\n')
    c.confirm_append(source,'r1',expected_receipt=old,receipt=new,frontier=path.stat().st_size,writer_epoch='w1')


def test_frozen_pages_survive_append_indexing_and_unrelated_changes(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(6)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);snap=r.open_window(0,100)
    first=r.plan_snapshot(snap,limit=2)
    append(catalog,path,[record(1.5,value='late'),record(50,value='new')]);index_all(catalog)
    other=write(tmp_path/'other.jsonl',[record(2.5,value='new source')])
    catalog.register(**registration(other,source='other'));index_all(catalog,'other')
    another=write(tmp_path/'future.jsonl',[record(900000)])
    catalog.register(**registration(another,source='future'));index_all(catalog,'future')
    r=CellSourceResolver(SourceCatalog(catalog.path))
    refs=list(first['refs']);after=first['next']
    while after is not None:
        page=r.plan_snapshot(snap,after=after,limit=2);refs.extend(page['refs']);after=page['next']
    assert [x.timestamp for x in refs]==list(range(6))
    out=r.read_refs(refs)
    assert [v['record']['timestamp'] for v in out['records']]==list(range(6))
    assert out['files_opened']==out['selected_files']==out['read_calls']==1
    assert out['raw_bytes']==sum(x.end-x.start for x in refs)
    fresh=r.open_window(0,100)
    assert len(r.plan_snapshot(fresh)['refs'])==9
    r.release_window(snap)
    with pytest.raises(Conflict,match='released'):r.plan_snapshot(snap)


def test_frozen_prefix_excludes_later_index_of_existing_bytes(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(6)])
    catalog.register(**registration(path))
    catalog.index_step('s','r1',expected_cursor=0,max_records=2)
    r=CellSourceResolver(catalog);snap=r.open_window(0,10)
    index_all(catalog)
    assert len(r.plan_snapshot(snap)['refs'])==2
    assert len(r.plan_snapshot(r.open_window(0,10))['refs'])==6


@pytest.mark.parametrize('change',['revision','truncate','rotate','stat_preserving'])
def test_frozen_snapshot_rejects_rewrites(catalog,tmp_path,change):
    path=write(tmp_path/'raw.jsonl',[record(1,value=111),record(2,value=222)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);snap=r.open_window(0,10);refs=r.plan_snapshot(snap)['refs']
    if change=='revision':
        replacement=write(tmp_path/'replacement.jsonl',[record(1,value=999)])
        catalog.register(**registration(replacement,revision='r2',expected_revision='r1'))
        with pytest.raises(Conflict,match='revision'):r.plan_snapshot(snap)
    elif change=='truncate':path.write_bytes(b'')
    elif change=='rotate':
        path.rename(tmp_path/'previous.jsonl');write(path,[record(1,value=999)])
    else:
        st=path.stat();path.write_bytes(path.read_bytes().replace(b'111',b'999'))
        os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns))
    with pytest.raises(Conflict):r.read_refs(refs)


def test_frozen_receipt_chain_repeated_appends(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);snap=r.open_window(0,100);refs=r.plan_snapshot(snap)['refs']
    old='receipt1'
    for i in range(12):
        new=f'append{i}';append(catalog,path,[record(i+2)],old=old,new=new);old=new;index_all(catalog)
    out=r.read_refs(refs)
    assert len(out['records'])==1 and out['raw_bytes']==refs[0].end
    with catalog.connection() as db:
        chain=db.execute('SELECT * FROM receipt ORDER BY seq').fetchall()
    assert len(chain)==13 and all(chain[i]['parent']==chain[i-1]['receipt'] for i in range(1,13))
    assert len({x['chain_hash'] for x in chain})==13


def test_grouped_reads_preserve_order_conflicts_channels_and_gaps(catalog,tmp_path,monkeypatch):
    path=write(tmp_path/'raw.jsonl',[
        record(3,module=1,value=1,cell_mv=[3001],temperature=[21]),
        record(2,module=2,value=2,cell_mv=[3002],temperature=[22]),
        record(1,module=1,value=3,cell_mv=[3003],temperature=[23]),
        record(1,module=1,value=4,cell_mv=[3004],temperature=[24])])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);refs=r.plan_snapshot(r.open_window(0,5,modules=(1,)))['refs']
    opened=[];original=Path.open
    def tracked(p,*args,**kwargs):
        if p==path:opened.append(str(p))
        return original(p,*args,**kwargs)
    monkeypatch.setattr(Path,'open',tracked)
    out=r.read_refs(refs)
    assert len(opened)==out['files_opened']==1 and out['read_calls']==2
    assert out['raw_bytes']==sum(x.end-x.start for x in refs)<path.stat().st_size
    assert [v['record']['value'] for v in out['records']]==[3,4,1]
    assert all('cell_mv' in v['record'] and 'temperature' in v['record'] for v in out['records'])


def test_grouped_six_modules_two_channels_physical_extent_scaling(tmp_path,monkeypatch):
    measurements=[];target=1790000000
    for days in (1,30,365,730):
        folder=tmp_path/str(days);folder.mkdir();c=SourceCatalog(folder/'catalog.sqlite')
        for age in range(days):
            for half in (0,1):
                path=write(folder/f'{age}-{half}.jsonl',[
                    record(target-age*86400+i,module=i,serial=f'S{i}',cell_mv=[3000+i],temperature=[20+i])
                    for i in range(half*3+1,half*3+4)])
                source=f'{age}-{half}';c.register(**registration(path,source=source));index_all(c,source)
        r=CellSourceResolver(SourceCatalog(c.path));snap=r.open_window(target,target+100)
        plan=r.plan_snapshot(snap);opened=[];original=Path.open
        def tracked(p,*args,**kwargs):
            opened.append(str(p));return original(p,*args,**kwargs)
        with monkeypatch.context() as m:
            m.setattr(Path,'open',tracked);out=r.read_refs(plan['refs'])
        assert set(opened)=={str(folder/'0-0.jsonl'),str(folder/'0-1.jsonl')}
        assert len(opened)==out['files_opened']==out['selected_files']==out['read_calls']==2
        assert [v['record']['module'] for v in out['records']]==list(range(1,7))
        assert all(len(v['record']['cell_mv'])==len(v['record']['temperature'])==1 for v in out['records'])
        measurements.append({'days':days,'sources':c.status()['sources'],'selected_files':out['selected_files'],
            'opens':out['files_opened'],'read_calls':out['read_calls'],'bytes':out['raw_bytes'],'decodes':out['decodes'],
            'sql_rows':plan['sql_rows_materialized'],'sql_steps':plan['sql_vm_steps']})
    assert len({(m['selected_files'],m['opens'],m['read_calls'],m['bytes'],m['decodes']) for m in measurements})==1
    print('frozen_grouped_window_scaling='+json.dumps(measurements))


def test_no_discovery_or_indexing_on_snapshot_reader(catalog,tmp_path,monkeypatch):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    catalog.register(**registration(path));index_all(catalog)
    def forbidden(*args,**kwargs):raise AssertionError('unexpected discovery/index IO')
    monkeypatch.setattr(os,'scandir',forbidden)
    monkeypatch.setattr(catalog,'index_step',forbidden)
    r=CellSourceResolver(catalog);snap=r.open_window(0,2)
    assert len(r.read_refs(r.plan_snapshot(snap)['refs'])['records'])==1


def test_append_confirmed_during_grouped_read_keeps_frozen_result(catalog,tmp_path,monkeypatch):
    from contextlib import contextmanager
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(4)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);refs=r.plan_snapshot(r.open_window(0,100))['refs']
    original=Path.open;appended=False
    class Wrapped:
        def __init__(self,handle):self.handle=handle
        def fileno(self):return self.handle.fileno()
        def seek(self,*args):return self.handle.seek(*args)
        def read(self,*args):
            nonlocal appended
            raw=self.handle.read(*args)
            if not appended:
                appended=True;append(catalog,path,[record(80)])
            return raw
    @contextmanager
    def wrapped_read(p,*args,**kwargs):
        with original(p,*args,**kwargs) as handle:yield Wrapped(handle)
    def patched(p,*args,**kwargs):
        if p==path and args and args[0]=='rb':return wrapped_read(p,*args,**kwargs)
        return original(p,*args,**kwargs)
    monkeypatch.setattr(Path,'open',patched)
    out=r.read_refs(refs)
    assert appended and [v['record']['timestamp'] for v in out['records']]==[0,1,2,3]
    assert out['files_opened']==out['read_calls']==1


def test_unconfirmed_append_rejected_until_explicit_receipt(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);refs=r.plan_snapshot(r.open_window(0,100))['refs']
    with path.open('ab') as h:h.write(json.dumps(record(2)).encode()+b'\n')
    with pytest.raises(Conflict):r.read_refs(refs)
    catalog.confirm_append('s','r1',expected_receipt='receipt1',receipt='receipt2',
                           frontier=path.stat().st_size,writer_epoch='w1')
    assert len(r.read_refs(refs)['records'])==1


def test_discovery_exception_same_owner_replays_uncommitted_entries(catalog,tmp_path):
    root,_=inventory_fixture(tmp_path)
    d=LegacySourceInventory(catalog);job=d.create([(root,'legacy-local')])
    def fail(stage):
        if stage=='after_discovery_entries':raise RuntimeError('injected')
    with pytest.raises(RuntimeError):d.step(job,max_entries=19,fault=fail)
    assert d.status(job)['sources']+d.status(job)['helpers']==0
    finish_inventory(d,job)
    assert (d.status(job)['sources'],d.status(job)['helpers'])==(1,601)


def test_duplicate_requested_refs_share_physical_read_not_semantic_dedup(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);refs=r.plan_snapshot(r.open_window(0,2))['refs']
    out=r.read_refs(refs+refs)
    assert len(out['records'])==2 and out['records'][0]==out['records'][1]
    assert out['files_opened']==out['read_calls']==1 and out['raw_bytes']==path.stat().st_size


def test_snapshot_source_limit_rolls_back_without_partial_plan(catalog,tmp_path):
    from cell_source_resolver import WindowBudgetExceeded
    for i in range(257):
        path=write(tmp_path/f'{i}.jsonl',[record(1)])
        catalog.register(**registration(path,source=str(i)));index_all(catalog,str(i))
    with pytest.raises(WindowBudgetExceeded):CellSourceResolver(catalog).open_window(0,2)
    with catalog.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM window_snapshot').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM window_source').fetchone()[0]==0
