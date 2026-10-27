"""Real writer/schema fixtures in temporary paths, never product data."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import uuid

import pytest
from cell_history import CellHistoryWriter
from cell_diagnostics import CellSample
from config_history import ConfigHistory,config_id
from position_history import PositionSnapshot,PositionHistoryLog,DocumentedIdentityResolver
from cell_window_adapter import CellWindowAdapter,local_day_window,utc_seconds
from history_epoch_adapter import EpochStore,EpochLogConsumer,EpochBudgetExceeded
from source_catalog import SourceCatalog
from raw_registry import RawRegistry,hashed,encoded,RegistryConflict
from raw_registry_catalog import RegistryCatalogConsumer
from history_append_receipt import current_receipt


def config(stamp,value):
    params={'cell_diag_low_soc_percent':value}
    return dict(schema_version=1,timestamp=datetime.fromtimestamp(stamp,timezone.utc).isoformat(),
                config_id=config_id(params),parameters=params,guardian_version='fixture',diagnostic_engine_version='0.4.12')


def position(stamp,serial,created=None):
    return PositionSnapshot(schema_version=1,position_history_id='PHS-'+str(uuid.uuid4()),
            effective_at=datetime.fromtimestamp(stamp,timezone.utc).isoformat(),
            created_at=datetime.fromtimestamp(created or stamp,timezone.utc).isoformat(),maintenance_event_id='MEV-fixture',
            positions={str(i):serial if i==1 else None for i in range(1,7)})


def setup(tmp_path,kind='config'):
    reg=RawRegistry.create(tmp_path/'registry');store=EpochStore(tmp_path/'epochs.db')
    r=reg.register(kind,'r1',tmp_path/(kind+'.jsonl'),writer_epoch='fixture',namespace='legacy-local',operation='register')
    reader=EpochLogConsumer(reg,store);reader.initialize({kind:kind})
    return reg,store,reader,r


def add(reg,r,values):
    data=b''.join(encoded(v.to_dict() if isinstance(v,PositionSnapshot) else v)+b'\n' for v in values)
    return reg.append(r['source'],data,expected_receipt=hashed(encoded(r)),operation=str(uuid.uuid4()))


def drain(reader,max_records=256):
    results=[]
    for _ in range(2000):
        result=reader.step(expected_cursor=reader.cursor(),max_records=max_records);results.append(result)
        if result['caught_up']:return results
    raise AssertionError('consumer did not finish')


def jobs(store,change,limit=64):
    c=next(c for c in store.changes(after=change-1) if c['id']==change)
    cursor=(c['day'],c['after_dependency'])
    while True:
        r=store.plan_jobs(change,expected_cursor=cursor,limit=limit);cursor=r['cursor']
        if r['finished']:return store.jobs(change=change,limit=256)


def test_actual_config_writer_format_and_current_options_are_separate(tmp_path):
    path=tmp_path/'actual_config.jsonl';record=ConfigHistory(path).record_if_changed({'cell_diag_low_soc_percent':12})
    reg,store,reader,r=setup(tmp_path)
    add(reg,r,[record]);drain(reader)
    assert store.at('config',record['timestamp'])==ConfigHistory(path).records()[0]
    assert store.at('config',0) is None
    before=store.fingerprint('current_config',1,2)
    key=store.set_current_options({'cell_diag_low_soc_percent':25},expected_fingerprint=None)
    assert key!=record['config_id'] and store.fingerprint('current_config',1,2)!=before
    assert store.at('config',record['timestamp'])['parameters']['cell_diag_low_soc_percent']==12


def test_audit_futureconfig_old_day_fingerprint_and_neighbors_unchanged(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    old=store.register_dependency('canonical-day1','config',86400,172800,context={'serial':'S1'})
    future=store.register_dependency('canonical-day3','config',3*86400,4*86400,context={'serial':'S1'})
    current_key=store.set_current_options({'cell_diag_low_soc_percent':20},expected_fingerprint=None)
    current=store.register_dependency('current-ring','current_config',86400,172800,context={'serial':'S1'})
    fp=store.fingerprint('config',86400,172800);last=store.changes()[-1]['id']
    r=add(reg,r,[config(3*86400,30)]);drain(reader)
    assert store.fingerprint('config',86400,172800)==fp
    changes=store.changes(after=last);assert len(changes)==1
    result=jobs(store,changes[0]['id'])
    assert [j['dependency'] for j in result]==[future] and old!=future
    last=changes[0]['id']
    store.set_current_options({'cell_diag_low_soc_percent':40},expected_fingerprint=current_key)
    result=jobs(store,store.changes(after=last)[0]['id'])
    assert [j['dependency'] for j in result]==[current]


def test_retroactive_epoch_jobs_exact_until_next_effective_epoch(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10),config(300,30)]);drain(reader)
    ids=[store.register_dependency('pub'+str(i),'config',i*100,(i+1)*100,context={}) for i in range(5)]
    before=[store.fingerprint('config',i*100,(i+1)*100) for i in range(5)];last=store.changes()[-1]['id']
    add(reg,r,[config(150,20)]);drain(reader)
    change=store.changes(after=last)[0]
    assert (change['start'],change['end'])==(150,300)
    result=jobs(store,change['id'],limit=1)
    assert [(x['dependency'],x['start'],x['end']) for x in result]==[(ids[1],150,200),(ids[2],200,300)]
    for i in (0,3,4):assert store.fingerprint('config',i*100,(i+1)*100)==before[i]
    assert store.at('config',149)['parameters']['cell_diag_low_soc_percent']==10
    assert store.at('config',150)['parameters']['cell_diag_low_soc_percent']==20
    assert store.at('config',300)['parameters']['cell_diag_low_soc_percent']==30


def test_real_position_format_preserves_serials_effective_time_and_provenance(tmp_path):
    reg,store,reader,r=setup(tmp_path,'position');a=position(100,'SERIAL-A');b=position(200,'SERIAL-B',300)
    path=tmp_path/'original-positions.jsonl';log=PositionHistoryLog(path);log.append(a);log.append(b)
    original=path.read_bytes();r=add(reg,r,[a,b]);drain(reader)
    reference=DocumentedIdentityResolver(log.read_all())
    for stamp in (0,100,199,200,400):
        assert store.position_at(1,stamp)==reference.identity_at(1,datetime.fromtimestamp(stamp,timezone.utc))
    before=store.fingerprint('position',0,100)
    dep=store.register_dependency('pub','position',100,200,context={'observed_serial':'SERIAL-A'})
    last=store.changes()[-1]['id'];c=position(150,'SERIAL-C',400);add(reg,r,[c]);drain(reader)
    changes=store.changes(after=last);assert len(changes)==1
    assert [(j['dependency'],j['start'],j['end']) for j in jobs(store,changes[0]['id'])]==[(dep,150,200)]
    assert store.fingerprint('position',0,100)==before and path.read_bytes()==original
    assert store.position_at(1,175)==('SERIAL-C',c.position_history_id)


def test_duplicate_position_id_stops_without_cursor_or_epoch_advance(tmp_path):
    reg,store,reader,r=setup(tmp_path,'position');a=position(100,'S1');r=add(reg,r,[a]);drain(reader)
    r=add(reg,r,[a]);cursor=reader.cursor()
    with pytest.raises(RegistryConflict,match='duplicate position'):reader.step(expected_cursor=cursor)
    assert reader.cursor()==cursor and store.position_at(1,100)==('S1',a.position_history_id)


def test_same_time_position_tie_matches_existing_sort_order(tmp_path):
    reg,store,reader,r=setup(tmp_path,'position');a=position(100,'S1',101);b=position(100,'S2',102)
    add(reg,r,[b,a]);drain(reader)
    reference=DocumentedIdentityResolver([b,a])
    assert store.position_at(1,100)==reference.identity_at(1,datetime.fromtimestamp(100,timezone.utc))


def test_resume_in_large_receipt_without_total_record_limit(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(i,10+i) for i in range(200)])
    first=reader.step(expected_cursor=reader.cursor());assert first['decodes']==0
    first=reader.step(expected_cursor=reader.cursor(),max_records=7)
    assert first['decodes']==7 and first['cursor'][1]>0
    reader=EpochLogConsumer(RawRegistry(reg.root),EpochStore(store.path));steps=drain(reader,7)
    assert sum(x['decodes'] for x in steps)==193
    with store.connection() as db:assert db.execute('SELECT count(*) FROM provenance').fetchone()[0]==200
    assert store.at('config',199)['parameters']['cell_diag_low_soc_percent']==209


@pytest.mark.parametrize('stage',['after_epochs','after_log_cursor','after_log_commit'])
def test_epoch_source_cursor_and_changes_atomic_process_crash(tmp_path,stage):
    reg,store,reader,r=setup(tmp_path);add(reg,r,[config(100,10)]);reader.step(expected_cursor=reader.cursor())
    result=subprocess.run([sys.executable,'-c','''import os,sys
from raw_registry import RawRegistry
from history_epoch_adapter import EpochStore,EpochLogConsumer
c=EpochLogConsumer(RawRegistry(sys.argv[1]),EpochStore(sys.argv[2]))
c.step(expected_cursor=c.cursor(),fault=lambda s:os._exit(77) if s==sys.argv[3] else None)
''',str(reg.root),str(store.path),stage],capture_output=True,text=True)
    assert result.returncode==77,result.stderr
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM epoch').fetchone()[0]==(1 if stage=='after_log_commit' else 0)
        assert db.execute('SELECT count(*) FROM change').fetchone()[0]==(1 if stage=='after_log_commit' else 0)
    drain(reader)
    assert reader.cursor()[0]==3


@pytest.mark.parametrize('stage',['after_jobs','after_job_cursor','after_job_commit'])
def test_dependency_jobs_cursor_atomic_process_crash(tmp_path,stage):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    ids=[store.register_dependency('pub'+str(i),'config',i*100,(i+1)*100,context={}) for i in range(5)]
    add(reg,r,[config(50,20)]);drain(reader);change=store.changes()[-1]
    result=subprocess.run([sys.executable,'-c','''import os,sys
from history_epoch_adapter import EpochStore
s=EpochStore(sys.argv[1]);c=s.changes(after=int(sys.argv[2])-1)[0]
s.plan_jobs(c['id'],expected_cursor=(c['day'],c['after_dependency']),limit=2,fault=lambda x:os._exit(77) if x==sys.argv[3] else None)
''',str(store.path),str(change['id']),stage],capture_output=True,text=True)
    assert result.returncode==77,result.stderr
    assert len(store.jobs(change=change['id']))==(2 if stage=='after_job_commit' else 0)
    assert len(jobs(store,change['id'],limit=2))==5


@pytest.mark.parametrize('value',['2026-01-01T12:00:00',True,float('nan')])
def test_ambiguous_or_invalid_time_never_inferred(value):
    with pytest.raises((ValueError,TypeError)):utc_seconds(value)


def cell_fixture(tmp_path,stamps,*,namespace='legacy-local',filename='unrelated.jsonl',modules=None):
    directory=tmp_path/'writer';writer=CellHistoryWriter(directory)
    for i,stamp in enumerate(stamps):
        writer.append(CellSample(stamp,modules[i] if modules else 1,[3300+i,3400+i],1.5,50,[20.0],[False,True],module_serial='S1',position_history_id='PHS-original'))
    # Real writer uses host local filenames. Preserve byte content but choose an
    # intentionally unrelated explicit fixture path, to prohibit name inference.
    raw=tmp_path/filename
    data=b''.join(p.read_bytes() for p in sorted(directory.glob('*.jsonl')));raw.write_bytes(data)
    reg=RawRegistry.create(tmp_path/'cells-registry')
    r=reg.register('cells','r1',raw,writer_epoch='fixture',namespace=namespace,operation='register',allow_existing=True)
    while r['end']<len(data):r=reg.adopt_tail('cells',expected_receipt=hashed(encoded(r)),operation=str(uuid.uuid4()))
    cat=SourceCatalog(tmp_path/'cells.db');consumer=RegistryCatalogConsumer(reg,cat);consumer.initialize()
    while not consumer.step(expected_cursor=consumer.cursor())['caught_up']:pass
    while cat.index_step('cells','r1',expected_cursor=cat.progress('cells','r1')['cursor'])['state']!='caught_up':pass
    return CellWindowAdapter(cat),raw,data,writer


@pytest.mark.parametrize('namespace',['legacy-local','utc'])
def test_audit_utc_2330_next_local_filename_reads_real_cell_channels_once(tmp_path,namespace,monkeypatch):
    stamp=utc_seconds('2026-01-01T23:30:00Z')
    adapter,raw,data,writer=cell_fixture(tmp_path,[stamp],namespace=namespace,filename='2026-01-02.jsonl')
    def forbidden(*args,**kwargs):raise AssertionError('warm reader must not discover files')
    monkeypatch.setattr(os,'scandir',forbidden)
    snapshot=adapter.open('2026-01-01T00:00:00Z','2026-01-02T00:00:00Z',modules=(1,))
    p=adapter.page(snapshot,fields=('voltages_mv','current_a','soc_percent'))
    assert p['decodes']==1 and p['files_opened']==1 and p['read_calls']==1
    assert p['records'][0]['timestamp']==stamp and p['records'][0]['position_history_id']=='PHS-original'
    assert p['records'][0]['measurements']=={'voltages_mv':[3300,3400],'current_a':1.5,'soc_percent':50}
    assert p['records'][0]['namespace']==namespace and raw.read_bytes()==data
    adapter.close(snapshot)


@pytest.mark.parametrize('day,hours',[('2026-03-29',23),('2026-10-25',25)])
def test_dst_days_real_schema_and_half_open_boundary(tmp_path,day,hours):
    start,end=local_day_window(day,'Europe/Berlin');assert end-start==hours*3600
    adapter,raw,data,_=cell_fixture(tmp_path,[start-1,start,end-1,end])
    snap=adapter.open(start,end);rows=adapter.page(snap)['records']
    assert [r['timestamp'] for r in rows]==[start,end-1]
    assert raw.read_bytes()==data


def test_inclusive_api_end_once_across_pages_and_nextafter_not_included(tmp_path):
    import math
    end=utc_seconds('2026-01-02T00:00:00Z')
    adapter,raw,data,_=cell_fixture(tmp_path,[end-1,end,math.nextafter(end,math.inf)])
    snap=adapter.open(end-1,end,inclusive_api_end=True);a=adapter.page(snap,limit=1)
    b=adapter.page(snap,after=a['next'],limit=1)
    assert [r['timestamp'] for r in a['records']+b['records']]==[end-1,end]
    assert b['complete'] and b['next'] is None and raw.read_bytes()==data


def test_old_append_sidecar_is_not_registry_confirmation(tmp_path):
    writer=CellHistoryWriter(tmp_path/'writer');writer.append(CellSample(100,1,[3300],0,50,[20],[False]))
    raw=next(writer.directory.glob('*.jsonl'));old=current_receipt(raw)
    assert old and old['generation']
    cat=SourceCatalog(tmp_path/'empty.db');adapter=CellWindowAdapter(cat)
    snap=adapter.open(0,200);page=adapter.page(snap)
    assert page['records']==[] and 'unknown' in page['coverage']
    assert current_receipt(raw)==old


def test_cell_page_budget_preserves_cursor_and_can_resume(tmp_path):
    adapter,raw,data,_=cell_fixture(tmp_path,[1,2,3])
    snap=adapter.open(0,4);blocked=adapter.page(snap,max_bytes=1)
    assert blocked['budget_blocked'] and blocked['next'] is None and not blocked['complete']
    first=adapter.page(snap,limit=1);rest=adapter.page(snap,after=first['next'])
    assert [r['timestamp'] for r in first['records']+rest['records']]==[1,2,3]


def test_missing_epochs_are_unknown_not_default_or_module_number(tmp_path):
    store=EpochStore(tmp_path/'empty.db')
    assert store.at('config',100) is None and store.position_at(1,100)==(None,None)
    assert store.fingerprint('config',100,200)


def test_multiple_selected_modules_share_one_physical_scan(tmp_path):
    adapter,raw,data,_=cell_fixture(tmp_path,[100]*6,modules=[1,2,3,4,5,6])
    snap=adapter.open(99,101,modules=(1,3,6));page=adapter.page(snap,fields=('voltages_mv','soc_percent'))
    assert [r['module'] for r in page['records']]==[1,3,6]
    assert page['files_opened']==1 and page['decodes']==3
    assert page['raw_bytes']<len(data)
    assert all(set(r['measurements'])=={'voltages_mv','soc_percent'} for r in page['records'])


@pytest.mark.parametrize('days',[1,30,365,730])
def test_actual_schema_adapter_warm_window_cost_not_history_age(tmp_path,days,monkeypatch):
    stamps=[i*86400+100 for i in range(days)]
    adapter,raw,data,_=cell_fixture(tmp_path,stamps)
    def forbidden(*args,**kwargs):raise AssertionError('no warm discovery')
    monkeypatch.setattr(os,'scandir',forbidden)
    snap=adapter.open(0,86400);page=adapter.page(snap,fields=('voltages_mv','soc_percent'))
    assert page['decodes']==1 and page['files_opened']==1 and page['raw_bytes']<500
    assert page['records'][0]['measurements']=={'voltages_mv':[3300,3400],'soc_percent':50}
    print({'days':days,'adapter_raw_bytes':page['raw_bytes'],'decodes':page['decodes'],'sql_steps':page['sql_vm_steps']})


def test_dependency_planning_more_than_one_page_survives_restart(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    for i in range(300):store.register_dependency('pub'+str(i),'config',0,100,context={'serial':str(i)})
    add(reg,r,[config(50,20)]);drain(reader);c=store.changes()[-1]
    result=store.plan_jobs(c['id'],expected_cursor=(c['day'],c['after_dependency']),limit=64)
    assert not result['finished']
    store=EpochStore(store.path)
    while not result['finished']:result=store.plan_jobs(c['id'],expected_cursor=result['cursor'],limit=64)
    alljobs=[];after=0
    while True:
        page=store.jobs(change=c['id'],after=after,limit=64);alljobs.extend(page)
        if not page:break
        after=page[-1]['dependency']
    assert len(alljobs)==300 and all(j['start']==50 and j['end']==100 for j in alljobs)
    with pytest.raises(RegistryConflict):store.plan_jobs(c['id'],expected_cursor=(c['day'],c['after_dependency']))


def test_dependency_created_after_epoch_change_not_invalidated_by_older_change(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    add(reg,r,[config(50,20)]);drain(reader);c=store.changes()[-1]
    store.register_dependency('new-pub','config',0,100,context={})
    assert jobs(store,c['id'])==[]


def test_epoch_window_budget_fails_explicitly_instead_of_truncating(tmp_path):
    reg,store,reader,r=setup(tmp_path)
    r=add(reg,r,[config(i,10+i) for i in range(200)]);r=add(reg,r,[config(i,10+i) for i in range(200,300)])
    drain(reader)
    with pytest.raises(EpochBudgetExceeded):store.fingerprint('config',0,400)
    assert store.fingerprint('config',250,300)


def test_conflicting_same_time_config_keeps_cursor_and_original(tmp_path):
    reg,store,reader,r=setup(tmp_path);a=config(100,10);r=add(reg,r,[a]);drain(reader)
    ambiguous=dict(a,parameters={'cell_diag_low_soc_percent':99});add(reg,r,[ambiguous]);cursor=reader.cursor()
    with pytest.raises(RegistryConflict,match='ambiguous'):reader.step(expected_cursor=cursor)
    assert reader.cursor()==cursor and store.at('config',100)==a


def test_missing_legacy_timestamp_is_not_recovered_from_filename(tmp_path):
    from test_source_catalog import registration,index_all,write
    cat=SourceCatalog(tmp_path/'legacy.db')
    raw=write(tmp_path/'2026-01-01.jsonl',[{'schema_version':1,'module':1,'voltages_mv':[3300]}])
    before=raw.read_bytes();cat.register(**registration(raw));index_all(cat)
    adapter=CellWindowAdapter(cat);snap=adapter.open('2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')
    page=adapter.page(snap)
    assert not page['records'] and page['invalid_records']==1 and 'unknown' in page['coverage']
    assert raw.read_bytes()==before


def test_unknown_legacy_physical_identity_stays_unknown(tmp_path):
    from test_source_catalog import registration,index_all,write
    cat=SourceCatalog(tmp_path/'legacy.db');raw=write(tmp_path/'any.jsonl',[{'schema_version':1,'timestamp':10,'module':1,'voltages_mv':[3300]}])
    cat.register(**registration(raw));index_all(cat)
    adapter=CellWindowAdapter(cat);snap=adapter.open(0,20);row=adapter.page(snap)['records'][0]
    assert row['module_serial'] is None and row['position_history_id'] is None
    assert row['missing_fields'] and row['source_context']=={}


def test_invalid_log_record_has_no_silent_skip_or_fabricated_epoch(tmp_path):
    reg,store,reader,r=setup(tmp_path);add(reg,r,[dict(config(100,10),timestamp='2026-01-01T00:00:00')])
    reader.step(expected_cursor=reader.cursor());cursor=reader.cursor()
    with pytest.raises(ValueError):reader.step(expected_cursor=cursor)
    assert reader.cursor()==cursor and store.at('config',200) is None
