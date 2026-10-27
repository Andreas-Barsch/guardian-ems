"""Synthetic real-engine persistent projection and checkpoint regressions."""
import json
import math
import sqlite3

import pytest
from canonical_phase import CanonicalPhaseMachine,CanonicalPhaseProjection,clip_interval
from canonical_checkpoint_storage import CountSequence,SCHEMA,pack,unpack
from test_canonical_phase import PARAMS,sample,feed
from test_history_epoch_adapter import setup,config,add,drain
from history_epoch_adapter import EpochStore
from raw_registry import RegistryConflict


def counts_db(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db


def expand(value):
    if isinstance(value,CountSequence):return list(value)
    if isinstance(value,dict):return {k:expand(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [expand(v) for v in value]
    return value


def test_persistent_counts_balance_concat_and_snapshot_size(tmp_path):
    db=counts_db(tmp_path/'counts.db');a=CountSequence(db);b=CountSequence(db)
    for i in range(4096):a.append(i)
    frozen=a.descriptor()
    for i in range(4096,8192):b.append(i)
    a.extend(b)
    assert len(a)==8192 and a[4095]==4095 and a[8191]==8191
    assert a._node(a.root)['height']<=2*math.ceil(math.log2(len(a)))
    assert len(json.dumps(pack({'times':a})))<250
    assert len(unpack(db,frozen))==4096
    assert a.page(after=4090,limit=12)['values']==list(range(4090,4102))
    db.commit();db.close()


def test_actual_engine_external_counts_snapshot_restore_exact(tmp_path):
    db=counts_db(tmp_path/'counts.db');factory=lambda values:CountSequence.from_values(db,values)
    values=[sample(0,1),sample(600,0),sample(601,0),sample(660,1),sample(661,1),sample(720,0)]
    parameters={'minimum_duration_seconds':0,'current_hysteresis_a':0,'short_gap_seconds':90}
    reference=CanonicalPhaseMachine('epoch','SERIAL-A',parameters=parameters)
    persisted=CanonicalPhaseMachine('epoch','SERIAL-A',parameters=parameters,timestamp_sequence_factory=factory)
    feed(reference,values);feed(persisted,values[:3])
    before_diag=expand(persisted.final_diagnostic);before_visual=expand(persisted.final_visual);before_end=expand(persisted.endpoints)
    state=json.loads(json.dumps(pack(persisted.snapshot())))
    persisted=CanonicalPhaseMachine.restore(unpack(db,state),parameters=parameters,timestamp_sequence_factory=factory)
    feed(persisted,values[3:]);out=expand(persisted.materialized());expected=reference.materialized()
    assert before_diag+out['diagnostic_intervals']==expected['diagnostic_intervals']
    assert before_visual+out['visual_intervals']==expected['visual_intervals']
    assert before_end+out['relative_endpoints']==expected['relative_endpoints']
    assert out['checkpoint']==expand(expected['checkpoint'])
    # Existing clipping code can binary-search the exact persistent sequence.
    for item in persisted.materialized()['visual_intervals']:
        assert clip_interval(item,0,720)==clip_interval(expand(item),0,720)
    CanonicalPhaseProjection._clear_daily_counts(persisted)
    assert isinstance(persisted.pending_diagnostic['count_timestamps'],CountSequence)
    assert not len(persisted.pending_diagnostic['count_timestamps'])
    db.close()


def test_consumed_epoch_changed_before_dependency_registration_is_stale(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    captured=store.capture_window('config',0,100)
    add(reg,r,[config(50,20)]);drain(reader)
    with pytest.raises(RegistryConflict,match='stale'):
        store.register_dependency('candidate','config',0,100,context={},expected_fingerprint=captured['fingerprint'])
    with store.connection() as db:assert db.execute('SELECT count(*) FROM dependency').fetchone()[0]==0


def test_consumed_epoch_changed_after_dependency_registration_blocks_publication(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    captured=store.capture_window('config',0,100)
    dep=store.register_dependency('candidate','config',0,100,context={},expected_fingerprint=captured['fingerprint'])
    add(reg,r,[config(50,20)]);drain(reader)
    with pytest.raises(RegistryConflict,match='stale'):
        with store.publication_guard([dep]):pytest.fail('stale candidate must not publish')
    assert store.changes()[-1]['max_dependency']>=dep


def test_future_epoch_does_not_block_guard_for_unchanged_consumed_window(tmp_path):
    reg,store,reader,r=setup(tmp_path);r=add(reg,r,[config(0,10)]);drain(reader)
    captured=store.capture_window('config',0,100)
    dep=store.register_dependency('candidate','config',0,100,context={},expected_fingerprint=captured['fingerprint'])
    add(reg,r,[config(200,20)]);drain(reader)
    with store.publication_guard([dep]):pass

from datetime import datetime,timezone,timedelta
from pathlib import Path
from projection_repository import ProjectionRepository
from canonical_day_repository import CanonicalDayRepository,bounds,following
from raw_registry_catalog import RegistryCatalogConsumer
from source_catalog import SourceCatalog
from raw_registry import RawRegistry,hashed,encoded
import uuid


def composed(tmp_path):
    (tmp_path/'epochs').mkdir();reg,epochs,epoch_reader,receipt=setup(tmp_path/'epochs')
    day='2026-01-01';start,_=bounds(day)
    cfg=dict(config(start,30),parameters=PARAMS,config_id='cfg')
    receipt=add(reg,receipt,[cfg]);drain(epoch_reader)
    rawreg=RawRegistry.create(tmp_path/'raw-registry')
    rawreceipt=rawreg.register('raw','r1',tmp_path/'actual-cell.jsonl',writer_epoch='test',namespace='legacy-local',operation='register')
    catalog=SourceCatalog(tmp_path/'catalog.db');consumer=RegistryCatalogConsumer(rawreg,catalog);consumer.initialize()
    repo=ProjectionRepository(tmp_path/'projection.db');project=CanonicalDayRepository(repo,catalog,epochs);project.initialize()
    project.define_stream('serial-a',origin_day=day,identity_epoch_id='SERIAL-A@observed',physical_serial='SERIAL-A')
    return dict(project=project,repo=repo,catalog=catalog,rawreg=rawreg,rawreceipt=rawreceipt,consumer=consumer,
                epochs=epochs,epochreg=reg,epoch_reader=epoch_reader,epoch_receipt=receipt,origin=day)


def append_cells(env,records):
    r=env['rawreceipt'];data=b''.join(encoded(x)+b'\n' for x in records)
    r=env['rawreg'].append('raw',data,expected_receipt=hashed(encoded(r)),operation=str(uuid.uuid4()));env['rawreceipt']=r
    while not env['consumer'].step(expected_cursor=env['consumer'].cursor())['caught_up']:pass
    cat=env['catalog']
    while cat.index_step('raw','r1',expected_cursor=cat.progress('raw','r1')['cursor'])['state']!='caught_up':pass


def close_day(env,day,*,bootstrap=False,limit=2):
    p=env['project'];begin=p.begin_day('serial-a',day,bootstrap=bootstrap)
    assert 'job' in begin,begin
    job=begin['job'];p.request_close(job)
    for _ in range(1000):
        result=p.step(job,limit=limit)
        assert result['raw_bytes']<=65536 and result['decodes']<=limit
        if result['status']=='published_closed':return p.published_state('serial-a',day)
    raise AssertionError('not closed')


def test_real_daily_midnight_checkpoint_equals_continuous_reference(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    records=[sample(end-180,0),sample(end-60,1.1),sample(end,1.1),sample(end+60,1.1),sample(end+180,1.1)]
    append_cells(env,records)
    first=close_day(env,day,bootstrap=True)
    assert first['metadata']['processed']==2 and first['metadata']['status']=='published_closed'
    # Fresh object, only the immediately preceding confirmed checkpoint.
    env['project']=CanonicalDayRepository(ProjectionRepository(env['repo'].path),env['catalog'],env['epochs'])
    second=close_day(env,following(day))
    reference=CanonicalPhaseMachine('SERIAL-A@observed','SERIAL-A')
    offsets=[];offset=0
    for record in records:offsets.append(offset);offset+=len(encoded(record))+1
    for i,record in enumerate(records):
        if i==2:CanonicalPhaseProjection._clear_daily_counts(reference)
        reference.process(record,PARAMS,config_id='cfg',source_cursor={'source_day':'raw','source_offset':offsets[i]},
                          source_signature={'source_id':'raw','revision':'r1','record_hash':hashed(encoded(record)+b'\n')})
    CanonicalPhaseProjection._clear_daily_counts(reference)
    with env['repo']._connection() as db:actual=expand(unpack(db,second['checkpoint']['engine']))
    assert actual==expand(reference.snapshot())
    assert actual['visual_state']['stable_axis']=='charge'
    old=env['project'].published_state('serial-a',day,first['bundle'])
    assert old==first
    warm=env['project'].step(env['project'].state('serial-a',day)['id'])
    assert warm['raw_bytes']==warm['decodes']==0


def test_missing_predecessor_and_explicit_origin_bootstrap(tmp_path):
    env=composed(tmp_path);p=env['project'];day=env['origin']
    assert p.begin_day('serial-a',following(day))['status']=='awaiting_predecessor'
    assert p.begin_day('serial-a',day)['status']=='awaiting_predecessor'
    assert p.begin_day('serial-a',following(day),bootstrap=True)['status']=='awaiting_predecessor'
    append_cells(env,[sample(bounds(day)[0]+10)])
    assert p.begin_day('serial-a',day,bootstrap=True)['status']=='open'


def test_empty_day_closes_without_fabricated_samples(tmp_path):
    env=composed(tmp_path);day=env['origin'];append_cells(env,[sample(bounds(day)[0]+10,1)])
    first=close_day(env,day,bootstrap=True);empty=close_day(env,following(day))
    assert empty['metadata']['processed']==0
    assert 'unknown' in empty['metadata']['coverage']
    assert empty['checkpoint']['engine']['last_sample_timestamp']==first['checkpoint']['engine']['last_sample_timestamp']


def test_open_append_processes_only_confirmed_delta(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);append_cells(env,[sample(start+10)])
    p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job'];first=p.step(job)
    assert first['status']=='open' and first['decodes']==1
    append_cells(env,[sample(start+20)]);p.refresh(job);second=p.step(job)
    assert second['decodes']==1 and p.published_state('serial-a',day)['metadata']['processed']==2
    assert p.published_state('serial-a',day,first['bundle'])['metadata']['processed']==1


def finish_correction(env,identifier,limit=2):
    results=[]
    for _ in range(1000):
        result=env['project'].correction_step(identifier,limit=limit);results.append(result)
        assert result.get('raw_bytes',0)<=65536 and result.get('decodes',0)<=limit
        if result.get('status') in ('converged_or_frontier','confirmed_frontier'):return results
    raise AssertionError('correction did not reach convergence/frontier')


def test_late_record_corrects_successors_until_real_state_and_vector_converge(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    records=[sample(start+10,1),sample(start+20,1),sample(end+10,0),sample(end+1000,0),sample(end+86400+10,0)]
    append_cells(env,records)
    old=[close_day(env,day,bootstrap=True),close_day(env,following(day)),close_day(env,following(following(day)))]
    append_cells(env,[sample(start+15,1)])
    correction=env['project'].request_correction('serial-a',day,reason='late confirmed raw record')
    results=finish_correction(env,correction)
    new=env['project'].published_state('serial-a',day)
    assert new['metadata']['processed']==3 and new['metadata']['revision']==2
    assert env['project'].published_state('serial-a',day,old[0]['bundle'])==old[0]
    assert any(x.get('correction_status')=='continue_successor' for x in results)
    # Older closed revisions stay readable even after explicit repository GC.
    env['repo'].collect(page_size=1024)
    assert env['project'].published_state('serial-a',day,old[0]['bundle'])==old[0]


def test_retroactive_configuration_rebuilds_real_engine_and_preserves_later_neighbor(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    # Day2 explicitly restores baseline parameters; the correction can converge.
    restore=dict(config(end,30),parameters=PARAMS,config_id='cfg')
    env['epoch_receipt']=add(env['epochreg'],env['epoch_receipt'],[restore]);drain(env['epoch_reader'])
    append_cells(env,[sample(start+10,1),sample(start+20,1),sample(end+10,0),sample(end+1000,0),
                      sample(end+2000,1),sample(end+2300,1),sample(end+3000,-1),sample(end+3300,-1),
                      sample(end+4000,0),sample(end+4300,0),sample(end+86400+10,0)])
    old=[close_day(env,day,bootstrap=True),close_day(env,following(day)),close_day(env,following(following(day)))]
    changed=dict(config(start+5,25),parameters={**PARAMS,'cell_diag_charge_current_a':2},config_id='retro')
    add(env['epochreg'],env['epoch_receipt'],[changed]);drain(env['epoch_reader'])
    correction=env['project'].request_correction('serial-a',day,reason='retroactive config epoch job')
    results=finish_correction(env,correction)
    assert env['project'].published_state('serial-a',day)['checkpoint']['engine']['config_revision']=='retro'
    assert any(x.get('correction_status')=='converged' for x in results)
    assert env['project'].published_state('serial-a',following(following(day)))==old[2]


def test_publication_race_preserves_previous_head_and_requires_explicit_retry(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day);append_cells(env,[sample(start+10)])
    p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job'];old=p.step(job)
    append_cells(env,[sample(start+20)]);p.refresh(job)
    changed=dict(config(start+15,25),parameters={**PARAMS,'cell_diag_charge_current_a':2},config_id='race')
    def race(stage):
        if stage=='before_epoch_guard':
            add(env['epochreg'],env['epoch_receipt'],[changed]);drain(env['epoch_reader'])
    with pytest.raises(RegistryConflict,match='stale'):p.step(job,fault=race)
    assert p.published_state('serial-a',day)['bundle']==old['bundle']
    p.invalidate(job)
    fresh=p.begin_day('serial-a',day,bootstrap=True,correction=True)['job']
    p.request_close(fresh)
    for _ in range(30):
        result=p.step(fresh)
        if result['status']=='published_closed':break
    assert result['status']=='published_closed'
    assert p.published_state('serial-a',day)['checkpoint']['engine']['config_revision']=='race'


def test_unpublished_correction_pages_do_not_replace_old_head(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    append_cells(env,[sample(start+i*60,1 if i%4<2 else 0) for i in range(30)])
    old=close_day(env,day,bootstrap=True,limit=10)
    append_cells(env,[sample(start+1,1)])
    correction=env['project'].request_correction('serial-a',day,reason='late')
    first=env['project'].correction_step(correction,limit=1)
    assert first['status']=='correcting'
    assert env['project'].published_state('serial-a',day)==old
    finish_correction(env,correction,limit=5)
    assert env['project'].published_state('serial-a',day)['bundle']!=old['bundle']


@pytest.mark.parametrize('stage',['after_private_checkpoint','after_private_commit','after_rows','after_cursors','after_publications','after_bundle','after_head','after_canonical_cursor','after_canonical_commit'])
def test_canonical_publication_and_engine_cursor_process_crash(tmp_path,stage):
    import subprocess,sys
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day);append_cells(env,[sample(start+10,1)])
    p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job'];p.request_close(job)
    result=subprocess.run([sys.executable,'-c','''import os,sys
from projection_repository import ProjectionRepository
from source_catalog import SourceCatalog
from history_epoch_adapter import EpochStore
from canonical_day_repository import CanonicalDayRepository
p=CanonicalDayRepository(ProjectionRepository(sys.argv[1]),SourceCatalog(sys.argv[2]),EpochStore(sys.argv[3]))
p.step(sys.argv[4],fault=lambda s:os._exit(77) if s==sys.argv[5] else None)
''',str(env['repo'].path),str(env['catalog'].path),str(env['epochs'].path),job,stage],capture_output=True,text=True)
    assert result.returncode==77,result.stderr
    published=p.published_state('serial-a',day)
    if stage=='after_canonical_commit':assert published['metadata']['processed']==1
    else:assert published is None
    final=p.step(job)
    assert final['status']=='published_closed'
    assert final['decodes']==(1 if stage=='after_private_checkpoint' else 0)
    assert p.published_state('serial-a',day)['metadata']['processed']==1
    assert env['repo'].inputs(final['bundle'])[('canonical:serial-a:'+day,'raw','r1')].offset==env['rawreceipt']['end']

from test_history_epoch_adapter import position,jobs
from history_epoch_adapter import EpochLogConsumer


def test_epoch_job_bridge_and_retroactive_position_keep_occupancy_epochs_separate(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    registry=RawRegistry.create(tmp_path/'positions-registry')
    r=registry.register('positions','r1',tmp_path/'positions.jsonl',writer_epoch='test',namespace='legacy-local',operation='register')
    reader=EpochLogConsumer(registry,env['epochs']);reader.initialize({'positions':'position'})
    initial=position(start,'SERIAL-A');later=position(end,'SERIAL-A')
    r=add(registry,r,[initial,later]);drain(reader)
    append_cells(env,[sample(start+10,1),sample(start+100,1),sample(end+10,0),sample(end+1000,0)])
    old=close_day(env,day,bootstrap=True);close_day(env,following(day))
    last=env['epochs'].changes()[-1]['id']
    missing=position(start+50,None,created=end+5000);add(registry,r,[missing]);drain(reader)
    change=env['epochs'].changes(after=last)[0];planned=jobs(env['epochs'],change['id'])
    queued=[]
    for event in planned:
        q=env['project'].enqueue_epoch_job(change['id'],event['dependency'])
        assert env['project'].enqueue_epoch_job(change['id'],event['dependency'])==q
        if q['status']=='queued':queued.append(q['correction'])
    assert len(queued)==1
    finish_correction(env,queued[0])
    corrected=env['project'].published_state('serial-a',following(day))
    assert corrected['metadata']['processed']==0  # Same serial now has a different documented occupancy epoch.
    assert env['project'].published_state('serial-a',day,old['bundle'])==old
    prior=env['project'].published_state('serial-a',day)
    new_identity='SERIAL-A@'+later.effective_at
    p=env['project'];p.define_stream('serial-a-returned',origin_day=following(day),identity_epoch_id=new_identity,physical_serial='SERIAL-A')
    begin=p.begin_day('serial-a-returned',following(day),bootstrap=True,identity_predecessor=('serial-a',prior['bundle']))
    assert begin['status']=='open';p.request_close(begin['job'])
    for _ in range(20):
        result=p.step(begin['job'])
        if result['status']=='published_closed':break
    returned=p.published_state('serial-a-returned',following(day))
    assert returned['metadata']['processed']==2 and returned['checkpoint']['engine']['identity_epoch_id']==new_identity


def test_correction_has_no_arbitrary_successor_day_limit(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    records=[sample(start+i*86400+10,1) for i in range(9)]
    # Day7 supplies enough new stable intervals to remove the old pending suffix.
    t=start+7*86400
    records.extend([sample(t+1000,0),sample(t+1300,0),sample(t+2000,-1),sample(t+2300,-1),sample(t+3000,0),sample(t+3300,0)])
    append_cells(env,records)
    days=[(datetime.fromisoformat(day)+timedelta(days=i)).date().isoformat() for i in range(9)]
    originals=[close_day(env,d,bootstrap=i==0,limit=8) for i,d in enumerate(days)]
    append_cells(env,[sample(start+15,1)])
    identifier=env['project'].request_correction('serial-a',day,reason='late sample across long continuous phase')
    results=finish_correction(env,identifier,limit=8)
    assert env['project'].state('serial-a',days[6])['revision']==2
    assert any(r.get('correction_status')=='converged' for r in results)
    assert env['project'].published_state('serial-a',days[8])==originals[8]


def test_known_late_record_cannot_be_silently_skipped_by_open_cursor(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    append_cells(env,[sample(start+20)]);p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job'];old=p.step(job)
    append_cells(env,[sample(start+10)]);p.refresh(job)
    from projection_repository import Conflict
    with pytest.raises(Conflict,match='late'):p.step(job)
    assert p.published_state('serial-a',day)['bundle']==old['bundle']


def test_shared_timestamp_source_ids_use_catalog_order_not_joined_revision_strings(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    append_cells(env,[sample(start+10)])
    # Additional explicit sources with prefix IDs, same timestamp and serial.
    for source in ('a','a-'):
        r=env['rawreg'].register(source,'r1',tmp_path/(source+'.jsonl'),writer_epoch='test',namespace='utc',operation=source)
        env['rawreg'].append(source,encoded(sample(start+10))+b'\n',expected_receipt=hashed(encoded(r)),operation=source+'-append')
    while not env['consumer'].step(expected_cursor=env['consumer'].cursor())['caught_up']:pass
    for source in ('a','a-'):
        while env['catalog'].index_step(source,'r1',expected_cursor=env['catalog'].progress(source,'r1')['cursor'])['state']!='caught_up':pass
    closed=close_day(env,day,bootstrap=True,limit=1)
    assert closed['metadata']['processed']==3


def test_correction_restarts_open_successor_without_falsely_closing_it(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    append_cells(env,[sample(start+10,1),sample(start+20,1),sample(end+10,1)])
    close_day(env,day,bootstrap=True)
    p=env['project'];job=p.begin_day('serial-a',following(day))['job'];p.step(job)
    old=p.published_state('serial-a',following(day))
    append_cells(env,[sample(start+15,1)])
    correction=p.request_correction('serial-a',day,reason='late record with open successor')
    result=finish_correction(env,correction)
    current=p.published_state('serial-a',following(day))
    assert current['metadata']['status']=='open' and current['metadata']['revision']==2
    assert current['checkpoint']['engine']['pending_diagnostic_interval']['canonical_sample_count']==4
    assert p.published_state('serial-a',following(day),old['bundle'])==old
    assert any(x.get('correction_status')=='confirmed_open_frontier' for x in result)
    append_cells(env,[sample(end+20,1)]);newjob=p.state('serial-a',following(day))['id'];p.refresh(newjob)
    assert p.step(newjob)['decodes']==1
    assert p.published_state('serial-a',following(day))['metadata']['processed']==2


def test_predecessor_changed_before_publish_fails_with_old_head_preserved(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    append_cells(env,[sample(start+10,1),sample(end+10,1)])
    close_day(env,day,bootstrap=True)
    p=env['project'];nextjob=p.begin_day('serial-a',following(day))['job']
    # Replace only the predecessor before publishing the already prepared child.
    correction=p.request_correction('serial-a',day,reason='explicit predecessor revision')
    # Initial day converges immediately, so the currently open successor is not rebuilt.
    finish_correction(env,correction)
    from projection_repository import Conflict
    with pytest.raises(Conflict,match='predecessor'):p.step(nextjob)
    assert p.published_state('serial-a',following(day)) is None


def test_existing_unresolved_identity_rule_is_preserved(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    record=dict(sample(start+10,0),module_serial=None,module=3);append_cells(env,[record])
    p=env['project'];p.define_stream('unknown-3',origin_day=day,identity_epoch_id='unresolved-position-3',physical_serial=None)
    job=p.begin_day('unknown-3',day,bootstrap=True)['job'];p.request_close(job)
    assert p.step(job)['status']=='published_closed'
    checkpoint=p.published_state('unknown-3',day)['checkpoint']['engine']
    assert checkpoint['physical_serial'] is None and checkpoint['identity_epoch_id']=='unresolved-position-3'
    reference=CanonicalPhaseMachine('unresolved-position-3',None);reference.process(record,PARAMS,config_id='cfg')
    assert checkpoint['visual_state']==reference.snapshot()['visual_state']


def test_changed_epoch_is_labeled_stale_before_job_dispatch(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day);append_cells(env,[sample(start+10)])
    closed=close_day(env,day,bootstrap=True)
    assert not env['project'].read_page('serial-a',day)['validity']['stale_dependencies']
    add(env['epochreg'],env['epoch_receipt'],[dict(config(start+5,20),parameters=PARAMS,config_id='new')]);drain(env['epoch_reader'])
    page=env['project'].read_page('serial-a',day)
    assert page['validity']['stale_dependencies']
    assert env['project'].published_state('serial-a',day,closed['bundle'])==closed


def test_source_revision_pairs_do_not_collide_on_separator_characters(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    for i,(source,revision) in enumerate([('a|b','c'),('a','b|c')]):
        r=env['rawreg'].register(source,revision,tmp_path/('pair'+str(i)+'.jsonl'),writer_epoch='test',namespace='utc',operation='pair'+str(i))
        env['rawreg'].append(source,encoded(sample(start+10))+b'\n',expected_receipt=hashed(encoded(r)),operation='pairappend'+str(i))
    while not env['consumer'].step(expected_cursor=env['consumer'].cursor())['caught_up']:pass
    for source,revision in [('a|b','c'),('a','b|c')]:
        while env['catalog'].index_step(source,revision,expected_cursor=env['catalog'].progress(source,revision)['cursor'])['state']!='caught_up':pass
    closed=close_day(env,day,bootstrap=True,limit=1)
    inputs=env['repo'].inputs(closed['bundle'])
    assert set((key[1],key[2]) for key in inputs)=={('a|b','c'),('a','b|c')}


def test_timestamp_sequence_damage_is_explicit_and_updates_are_forbidden(tmp_path):
    db=counts_db(tmp_path/'counts.db');seq=CountSequence.from_values(db,[1,2,3]);descriptor=seq.descriptor()
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):db.execute('UPDATE canonical_counts SET value=99 WHERE value=2')
    # Synthetic disk/content damage simulation only, in the temporary test DB.
    db.execute('DROP TRIGGER canonical_counts_immutable');db.execute('UPDATE canonical_counts SET value=99 WHERE value=2')
    with pytest.raises(ValueError,match='corrupt'):unpack(db,descriptor)[1]
    db.close()


def test_same_effective_position_transitions_match_real_identity_resolver(tmp_path):
    from canonical_phase import IdentityEpochResolver
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    registry=RawRegistry.create(tmp_path/'ties-registry');r=registry.register('positions','r1',tmp_path/'ties.jsonl',writer_epoch='test',namespace='legacy-local',operation='register')
    reader=EpochLogConsumer(registry,env['epochs']);reader.initialize({'positions':'position'})
    snapshots=[position(start,'SERIAL-A',start+1),position(start,None,start+2),position(start,'SERIAL-A',start+3)]
    add(registry,r,snapshots);drain(reader)
    reference=IdentityEpochResolver();reference.snapshots=snapshots
    record=sample(start+10);identity,serial=reference.resolve(record,record['timestamp'])
    assert identity!='SERIAL-A@observed'
    append_cells(env,[record]);p=env['project'];p.define_stream('tie-epoch',origin_day=day,identity_epoch_id=identity,physical_serial=serial)
    job=p.begin_day('tie-epoch',day,bootstrap=True)['job'];p.request_close(job);p.step(job)
    checkpoint=p.published_state('tie-epoch',day)
    assert checkpoint['metadata']['processed']==1 and checkpoint['checkpoint']['engine']['identity_epoch_id']==identity


def test_losing_position_tie_still_invalidates_consumed_identity_trace(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    registry=RawRegistry.create(tmp_path/'ties-registry');r=registry.register('positions','r1',tmp_path/'ties.jsonl',writer_epoch='test',namespace='legacy-local',operation='register')
    reader=EpochLogConsumer(registry,env['epochs']);reader.initialize({'positions':'position'})
    r=add(registry,r,[position(start,'SERIAL-A',start+1),position(start,'SERIAL-A',start+3)]);drain(reader)
    append_cells(env,[sample(start+10)]);p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job']
    winner=env['epochs'].position_at(1,start)
    def race(stage):
        if stage=='before_epoch_guard':add(registry,r,[position(start,None,start+2)]);drain(reader)
    with pytest.raises(RegistryConflict,match='stale'):p.step(job,fault=race)
    assert env['epochs'].position_at(1,start)==winner
    assert p.published_state('serial-a',day) is None
