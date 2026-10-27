"""Synthetic catalog/resolver tests. No live data or runtime composition."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from projection_repository import Conflict
from source_catalog import SourceCatalog
from cell_source_resolver import CellSourceResolver


def record(stamp,module=1,serial='S1',**extra):
    return {'schema_version':1,'timestamp':stamp,'module':module,'module_serial':serial,**extra}


def write(path,values):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b''.join(json.dumps(v,separators=(',',':')).encode()+b'\n' for v in values))
    return path


def registration(path,source='s',revision='r1',namespace='legacy-local',**extra):
    return dict(source=source,revision=revision,path=str(path),namespace=namespace,
                frontier=path.stat().st_size,writer_epoch='w1',receipt='receipt1',**extra)


@pytest.fixture
def catalog(tmp_path):
    return SourceCatalog(tmp_path/'catalog.sqlite')


def index_all(c,source='s',revision='r1',**budgets):
    metrics=[]
    while True:
        p=c.progress(source,revision)
        result=c.index_step(source,revision,expected_cursor=p['cursor'],**budgets)
        metrics.append(result)
        if result['state']=='caught_up': return metrics
        assert result['cursor']>p['cursor'],result


@pytest.mark.parametrize('namespaces',[('legacy-local',),('utc',),('legacy-local','utc')])
def test_legacy_utc_mix_timestamp_not_filename(catalog,tmp_path,namespaces):
    stamp=datetime(2026,9,19,23,30,tzinfo=timezone.utc).timestamp()
    for i,ns in enumerate(namespaces):
        # A Berlin local filename is next day; UTC timestamp is still Sept19.
        path=write(tmp_path/ns/'2026-09-20.jsonl',[record(stamp,serial='serial'+str(i),value=i)])
        catalog.register(**registration(path,source=str(i),namespace=ns,context={'epoch':i}))
        index_all(catalog,str(i))
    resolver=CellSourceResolver(catalog)
    plan=resolver.plan_window(stamp-1,stamp+1)
    assert len(plan['refs'])==len(namespaces)
    assert {r.storage_convention for r in plan['refs']}==set(namespaces)
    read=resolver.read_refs(plan['refs'])
    assert [v['record']['value'] for v in read['records']]==list(range(len(namespaces)))
    assert all(r.timestamp==stamp for r in plan['refs'])
    assert len({r.identity_context for r in plan['refs']})==len(namespaces)
    assert all(r.source_context for r in plan['refs'])
    assert resolver.plan_window(stamp+3600,stamp+86400)['refs']==[]


@pytest.mark.parametrize('day,hours',[(datetime(2026,3,29),23),(datetime(2026,10,25),25)])
def test_dst_local_day_uses_explicit_utc_window(catalog,tmp_path,day,hours):
    berlin=ZoneInfo('Europe/Berlin')
    start=day.replace(tzinfo=berlin).timestamp()
    end=(day+timedelta(days=1)).replace(tzinfo=berlin).timestamp()
    assert end-start==hours*3600
    values=[record(start+i*3600) for i in range(-1,hours+1)]
    path=write(tmp_path/'arbitrary-local-day.jsonl',values)
    catalog.register(**registration(path));index_all(catalog)
    plan=CellSourceResolver(catalog).plan_window(start,end)
    assert [r.timestamp for r in plan['refs']]==[start+i*3600 for i in range(hours)]


def test_unsorted_conflicts_and_identity_are_not_deduplicated(catalog,tmp_path):
    for source,values in [('a',[record(5,value=1),record(3,value=2),record(5,serial=None,value=3)]),
                          ('b',[record(5,serial='changed',value=9)])]:
        path=write(tmp_path/(source+'.jsonl'),values)
        catalog.register(**registration(path,source=source));index_all(catalog,source)
    r=CellSourceResolver(catalog);p=r.plan_window(0,10)
    assert [x.timestamp for x in p['refs']]==[3,5,5,5]
    assert len({(x.source_id,x.revision,x.start) for x in p['refs']})==4
    assert [v['record']['value'] for v in r.read_refs(p['refs'])['records']]==[2,1,3,9]


def test_bounded_index_resume_partial_line_and_append(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(20)])
    with path.open('ab') as h:h.write(b'{"schema_version":1,"timestamp":21')
    catalog.register(**registration(path))
    decoded=0
    while True:
        cursor=catalog.progress('s','r1')['cursor']
        result=catalog.index_step('s','r1',expected_cursor=cursor,max_bytes=300,max_records=2)
        assert result['bytes']<=300 and result['decodes']<=2
        decoded+=result['decodes'];catalog=SourceCatalog(catalog.path)
        if result['state']=='partial_line_or_budget':break
    assert decoded==20 and catalog.status()['pending']==1
    partial_cursor=catalog.progress('s','r1')['cursor']
    with path.open('ab') as h:h.write(b',"module":1}\n')
    with pytest.raises(Conflict):catalog.index_step('s','r1',expected_cursor=partial_cursor)
    catalog.confirm_append('s','r1',expected_receipt='receipt1',receipt='receipt2',
                           frontier=path.stat().st_size,writer_epoch='w1')
    step=catalog.index_step('s','r1',expected_cursor=partial_cursor)
    assert step['decodes']==1 and step['state']=='caught_up'
    assert catalog.status()['pending']==0
    done=catalog.index_step('s','r1',expected_cursor=step['cursor'])
    assert (done['files'],done['bytes'],done['decodes'])==(0,0,0)


@pytest.mark.parametrize('stage',['after_index_rows','after_index_cursor','after_index_commit'])
def test_index_process_crash_atomic_rows_and_cursor(catalog,tmp_path,stage):
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(5)])
    catalog.register(**registration(path))
    code="""
import os,sys
from source_catalog import SourceCatalog
c=SourceCatalog(sys.argv[1])
c.index_step('s','r1',expected_cursor=0,max_records=2,
 fault=lambda at: os._exit(81) if at==sys.argv[2] else None)
"""
    child=subprocess.run([sys.executable,'-c',code,str(catalog.path),stage],capture_output=True,timeout=15)
    assert child.returncode==81,child.stderr.decode()
    c=SourceCatalog(catalog.path);p=c.progress('s','r1')
    assert p['records']==(2 if stage=='after_index_commit' else 0)
    metrics=index_all(c,max_records=2)
    assert sum(m['decodes'] for m in metrics)==(3 if stage=='after_index_commit' else 5)
    assert len(CellSourceResolver(c).plan_window(0,10)['refs'])==5
    with c.connection() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_paginated_inventory_more_than_512_helpers(catalog,tmp_path):
    path=write(tmp_path/'source.jsonl',[record(10)])
    items=[]
    for i in range(600):
        helper=tmp_path/f'helper-{i}.idx';helper.write_text('helper')
        items.append({'kind':'helper','path':str(helper)})
    items.append({'kind':'source','registration':registration(path)})
    pos=0
    for start in range(0,len(items),73):
        page=items[start:start+73]
        pos=catalog.ingest_page('legacy-manifest',expected_cursor=pos,items=page,finish=start+73>=len(items))
        catalog=SourceCatalog(catalog.path)
        assert catalog.migration_progress('legacy-manifest')['cursor']==pos
    assert catalog.status()['helpers']==600 and catalog.status()['sources']==1
    assert len(catalog.registered(limit=1))==1
    assert catalog.registered(after=('s','r1'))==[]
    assert sum(x['files'] for x in index_all(catalog))==1
    assert len(CellSourceResolver(catalog).plan_window(0,20)['refs'])==1


@pytest.mark.parametrize('stage',['after_registrations','after_migration_cursor','after_migration_commit'])
def test_manifest_cursor_process_crash(catalog,tmp_path,stage):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    item={'kind':'source','registration':registration(path)}
    code="""
import os,sys,json
from source_catalog import SourceCatalog
c=SourceCatalog(sys.argv[1]);items=json.loads(sys.argv[2])
c.ingest_page('job',expected_cursor=0,items=items,
 fault=lambda at: os._exit(82) if at==sys.argv[3] else None)
"""
    child=subprocess.run([sys.executable,'-c',code,str(catalog.path),json.dumps([item]),stage],capture_output=True,timeout=15)
    assert child.returncode==82,child.stderr.decode()
    c=SourceCatalog(catalog.path)
    if stage=='after_migration_commit':
        assert c.migration_progress('job')['cursor']==1 and c.status()['sources']==1
        with pytest.raises(Conflict):c.ingest_page('job',expected_cursor=0,items=[item])
    else:
        assert c.migration_progress('job') is None and c.status()['sources']==0
        c.ingest_page('job',expected_cursor=0,items=[item])
    assert c.status()['sources']==1


def test_rewrite_truncation_rotation_and_explicit_revision(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(10),record(20)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);old=r.plan_window(0,30)['refs']
    write(path,[record(2)])
    with pytest.raises(Conflict):r.read_refs(old)
    with pytest.raises(Conflict):catalog.confirm_append('s','r1',expected_receipt='receipt1',receipt='r2',frontier=path.stat().st_size,writer_epoch='w1')
    catalog.register(**registration(path,revision='r2',expected_revision='r1'));index_all(catalog,revision='r2')
    assert [x.timestamp for x in r.plan_window(0,30)['refs']]==[2]
    rotated=tmp_path/'rotated.jsonl';path.rename(rotated)
    write(path,[record(3)])
    with pytest.raises(Conflict):catalog.index_step('s','r2',expected_cursor=catalog.progress('s','r2')['cursor'])
    catalog.register(**registration(path,source='rotated-stream',namespace='utc'));index_all(catalog,'rotated-stream')
    # No automatic relocation or retirement of old IDs is inferred.
    assert catalog.status()['sources']==2


def test_stat_preserving_rewrite_selected_range_hash_detects(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(10,value=111)])
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);refs=r.plan_window(0,20)['refs'];st=path.stat()
    raw=path.read_bytes().replace(b'111',b'999');path.write_bytes(raw)
    os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns))
    # Metadata-only caught-up cannot prove untouched bytes; selected hash can.
    assert catalog.index_step('s','r1',expected_cursor=len(raw))['state']=='caught_up'
    with pytest.raises(Conflict,match='hash'):r.read_refs(refs)


def test_invalid_line_coverage_and_selection(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(1,module=1),record(2,module=2),record(True)])
    with path.open('ab') as h:h.write(b'broken\n')
    catalog.register(**registration(path));index_all(catalog)
    r=CellSourceResolver(catalog);p=r.plan_window(0,3,modules=(2,))
    assert len(p['refs'])==1 and p['refs'][0].module==2
    assert p['invalid_records']==2 and 'unknown' in p['coverage']


def test_plan_pagination_no_raw_io_no_warm_inventory(catalog,tmp_path,monkeypatch):
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(8)])
    catalog.register(**registration(path));index_all(catalog)
    def forbidden(*args,**kwargs):raise AssertionError('raw/discovery IO forbidden')
    with monkeypatch.context() as m:
        m.setattr(Path,'open',forbidden);m.setattr(Path,'stat',forbidden);m.setattr(os,'scandir',forbidden)
        c=SourceCatalog(catalog.path);r=CellSourceResolver(c)
        p=r.plan_window(0,10,limit=3)
        q=r.plan_window(0,10,limit=3,after=p['next'],expected_generation=p['generation'])
        assert [x.timestamp for x in p['refs']+q['refs']]==list(range(6))
        assert p['files_opened']==p['raw_bytes']==p['decodes']==0
    with path.open('ab') as h:h.write(json.dumps(record(9)).encode()+b'\n')
    catalog.confirm_append('s','r1',expected_receipt='receipt1',receipt='receipt2',frontier=path.stat().st_size,writer_epoch='w1')
    with pytest.raises(Conflict):r.plan_window(0,10,after=p['next'],expected_generation=p['generation'])


def test_cost_same_window_at_1_30_365_730_days(tmp_path):
    measurements=[];target=datetime(2026,9,20,tzinfo=timezone.utc).timestamp()
    for days in (1,30,365,730):
        folder=tmp_path/str(days);folder.mkdir();c=SourceCatalog(folder/'catalog.sqlite')
        for age in range(days):
            stamp=target-age*86400
            path=write(folder/f'{age}.jsonl',[record(stamp+i,module=i,serial=f'S{i}') for i in range(1,7)])
            c.register(**registration(path,source=str(age)));index_all(c,str(age))
        r=CellSourceResolver(SourceCatalog(c.path))
        p=r.plan_window(target,target+86400);out=r.read_refs(p['refs'])
        warm=r.plan_window(target,target+86400)
        assert warm['sql_rows_materialized']==p['sql_rows_materialized']
        assert warm['files_opened']==warm['raw_bytes']==warm['decodes']==0
        single=r.plan_window(target,target+86400,modules=(3,))
        assert len(single['refs'])==1 and single['refs'][0].module==3
        explain=r.explain_window(target,target+86400)
        assert any('SEARCH e USING INDEX entry_window' in step for step in explain),explain
        assert not any('SCAN e' in step for step in explain)
        assert len(out['records'])==6
        measurements.append({'days':days,'sources':c.status()['sources'],'sql_rows':p['sql_rows_materialized'],'sql_steps':p['sql_vm_steps'],
                             'raw_files':out['files_opened'],'bytes':out['raw_bytes'],'decodes':out['decodes'],'sql_plan':explain})
    assert len({(m['sql_rows'],m['raw_files'],m['bytes'],m['decodes']) for m in measurements})==1
    print('catalog_window_scaling='+json.dumps(measurements))


def test_window_budget_failure_is_explicit_no_rebuild(catalog,tmp_path):
    from cell_source_resolver import WindowBudgetExceeded
    path=write(tmp_path/'raw.jsonl',[record(i) for i in range(10)])
    catalog.register(**registration(path));index_all(catalog)
    before=catalog.progress('s','r1')
    with pytest.raises(WindowBudgetExceeded,match='no rebuild'):
        CellSourceResolver(catalog).plan_window(0,20,sql_step_limit=1)
    assert catalog.progress('s','r1')==before


def test_unconfirmed_bytes_not_indexed(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    frontier=path.stat().st_size
    with path.open('ab') as h:h.write(json.dumps(record(2)).encode()+b'\n')
    item=registration(path);item['frontier']=frontier
    catalog.register(**item)
    out=catalog.index_step('s','r1',expected_cursor=0)
    assert out['bytes']==frontier and out['decodes']==1
    assert len(CellSourceResolver(catalog).plan_window(0,3)['refs'])==1
    catalog.confirm_append('s','r1',expected_receipt='receipt1',receipt='receipt2',
                           frontier=path.stat().st_size,writer_epoch='w1')
    assert catalog.index_step('s','r1',expected_cursor=frontier)['decodes']==1


def test_oversized_line_is_explicitly_unfinished(catalog,tmp_path):
    path=write(tmp_path/'large.jsonl',[record(1,payload='x'*70000)])
    catalog.register(**registration(path))
    out=catalog.index_step('s','r1',expected_cursor=0)
    assert out['cursor']==out['decodes']==0 and out['bytes']==65536
    assert out['state']=='partial_line_or_budget'
    plan=CellSourceResolver(catalog).plan_window(0,2)
    assert plan['refs']==[] and plan['pending_sources']==1
    assert 'unknown' in plan['coverage']


def test_failed_inventory_page_rolls_back_registered_source(catalog,tmp_path):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    with pytest.raises(ValueError):
        catalog.ingest_page('manifest',expected_cursor=0,items=[
            {'kind':'source','registration':registration(path)}, {'kind':'unknown'}])
    assert catalog.status()['sources']==0 and catalog.migration_progress('manifest') is None
    assert catalog.registered()==[]


@pytest.mark.parametrize('field,value',[('source',''),('revision',None)])
def test_invalid_identifiers_do_not_break_catalog_pagination(catalog,tmp_path,field,value):
    path=write(tmp_path/'raw.jsonl',[record(1)])
    item=registration(path);item[field]=value
    with pytest.raises(ValueError,match='identifiers'):
        catalog.register(**item)
    assert catalog.registered()==[] and catalog.status()['sources']==0



def test_page_checks_shared_prefix_once_before_and_after_and_hashes_every_record(tmp_path,monkeypatch):
    from contextlib import contextmanager
    c=SourceCatalog(tmp_path/'page-catalog')
    path=write(tmp_path/'raw.jsonl',[record(i,value=i) for i in range(200)])
    c.register(**registration(path));index_all(c)
    resolver=CellSourceResolver(c);snapshot=resolver.open_window(0,201)
    try:
        refs=resolver.plan_snapshot(snapshot)['refs'];connection=c.connection;opened=[]
        @contextmanager
        def measured():
            opened.append(True)
            with connection() as db:yield db
        monkeypatch.setattr(c,'connection',measured)
        result=resolver.read_refs(refs)
        assert result['decodes']==200 and len(opened)==2
        # Tamper a non-first record while preserving size/mtime. Every record
        # hash remains checked despite sharing the per-page metadata proof.
        before=path.stat();path.write_bytes(path.read_bytes().replace(b'"value":199',b'"value":198'))
        os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        with pytest.raises(Conflict,match='record hash'):resolver.read_refs(refs)
    finally:resolver.release_window(snapshot)
