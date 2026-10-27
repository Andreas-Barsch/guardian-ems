import json
import pytest
from display_day_repository import DisplayDayRepository
from test_canonical_repository import composed,append_cells,close_day,bounds,sample,following
from display_history_projection import _channels,_timestamp,_channel_key,_bucket_start,_new_bucket,_add,_public,RESOLUTIONS
from projection_repository import Conflict


def display(env):
    d=DisplayDayRepository(env['repo'],env['catalog'],env['project']);d.initialize();return d


def finish(d,day):
    raw_bytes=decodes=0
    for _ in range(300):
        result=d.step(day)
        raw_bytes+=result['raw_bytes'];decodes+=result['decodes']
        if result['status'] in ('caught_up','published_closed'):return dict(result,raw_bytes=raw_bytes,decodes=decodes)
    raise AssertionError('not finished')


def records(d,day,bundle=None):
    result=[];after=''
    while True:
        p=d.read_page(day,bundle=bundle,after_key=after,limit=4);result.extend(v for k,v in p['rows'])
        if len(p['rows'])<4:return result
        after=p['rows'][-1][0]


def reference(values):
    import statistics
    buckets={};cadences={}
    for order,raw in enumerate(values,1):
        ts=_timestamp(raw,'cell');epoch=raw['timestamp']
        for channel,metric,value in _channels(raw,'cell'):
            ck=_channel_key(channel,metric,'source',0)
            ctx=cadences.setdefault(ck,{'last':epoch,'gaps':[]})
            if epoch>ctx['last']:ctx.update(last=epoch,gaps=(ctx['gaps']+[epoch-ctx['last']])[-256:])
            cadence=statistics.median(ctx['gaps']) if ctx['gaps'] else None
            for resolution,seconds in RESOLUTIONS.items():
                key=_channel_key(channel,metric,resolution,_bucket_start(epoch,seconds))
                if key in buckets:_add(buckets[key],ts,value,order)
                else:buckets[key]=_new_bucket({**channel,'metric':metric},resolution,ts,value,order)
                buckets[key]['_source_cadence']=cadence
    return [_public(v) for v in buckets.values()]


def normalize(rows):return sorted((json.dumps(r,sort_keys=True) for r in rows))


def test_reference_display_and_warm_restart_without_raw(tmp_path,monkeypatch):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    values=[sample(start+10),dict(sample(start+20),soc_percent=80),dict(sample(start+20),soc_percent=79),sample(start+150),sample(start+3601)]
    append_cells(env,values);close_day(env,day,bootstrap=True)
    d=display(env);d.begin(day,['serial-a']);finish(d,day)
    assert normalize(records(d,day))==normalize(reference(values))
    d=display(env)
    monkeypatch.setattr(d.reader,'page',lambda *a,**k:pytest.fail('warm read'))
    assert d.step(day)=={'status':'published_closed','raw_bytes':0,'decodes':0}


def test_append_only_reads_one_new_record_and_preserves_old_revision(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day);first=sample(start+10)
    append_cells(env,[first]);p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job'];p.step(job)
    d=display(env);d.begin(day,['serial-a']);finish(d,day);old=d.repository.head(d.head(day));oldrows=records(d,day)
    second=sample(start+20);append_cells(env,[second]);p.refresh(job);p.step(job)
    assert not d.read_page(day)['validity']['stale_dependencies']
    assert d.read_page(day)['validity']['newer_canonical_available']
    d.refresh(day,['serial-a']);result=finish(d,day)
    assert result['decodes']==1 and result['raw_bytes']>0
    assert normalize(records(d,day))==normalize(reference([first,second]))
    assert records(d,day,old)==oldrows


@pytest.mark.parametrize('stage',['after_display_prepared','after_display_prepared_commit','before_display_publish','after_display_cursor','after_display_commit'])
def test_transaction_failure_replay(tmp_path,stage):
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);values=[sample(start+10)]
    append_cells(env,values);close_day(env,day,bootstrap=True);d=display(env);d.begin(day,['serial-a'])
    def fault(s):
        if s==stage:raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError,match='interrupted'):d.step(day,fault=fault)
    d=display(env);finish(d,day)
    assert normalize(records(d,day))==normalize(reference(values))


def test_correction_private_until_complete_and_old_revision_retained(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);values=[sample(start+20),sample(start+30)]
    append_cells(env,values);close_day(env,day,bootstrap=True);d=display(env);d.begin(day,['serial-a']);finish(d,day)
    old=d.repository.head(d.head(day));oldrows=records(d,day)
    late=sample(start+10);append_cells(env,[late]);p=env['project'];correction=p.request_correction('serial-a',day,reason='late')
    for _ in range(30):
        r=p.correction_step(correction)
        if r.get('correction_status') in ('converged','confirmed_open_frontier') or r['status'] in ('confirmed_frontier','converged_or_frontier'):break
    assert not p.publication_validity('serial-a',day,
        p.published_state('serial-a',day)['bundle'],
        p.published_state('serial-a',day)['metadata'])['correction_pending']
    assert d.read_page(day)['validity']['stale_dependencies']
    d.begin(day,['serial-a'],correction=True);d.step(day)
    assert d.repository.head(d.head(day))==old
    finish(d,day)
    assert normalize(records(d,day))==normalize(reference([late,*values]))
    assert records(d,day,old)==oldrows


def test_changed_canonical_before_publish_rejects_display(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);append_cells(env,[sample(start+10)])
    p=env['project'];job=p.begin_day('serial-a',day,bootstrap=True)['job'];p.step(job)
    d=display(env);d.begin(day,['serial-a'])
    def fault(stage):
        if stage=='before_display_publish':
            append_cells(env,[sample(start+20)]);p.refresh(job);p.step(job)
    with pytest.raises(Conflict,match='canonical publication changed'):d.step(day,fault=fault)
    assert d.repository.head(d.head(day)) is None


@pytest.mark.parametrize('stage',['after_display_prepared','after_display_prepared_commit','before_display_publish','after_display_cursor','after_display_commit'])
def test_actual_process_abort_reopens_exactly_once(tmp_path,stage):
    import multiprocessing,os
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);values=[sample(start+10)]
    append_cells(env,values);close_day(env,day,bootstrap=True);d=display(env);d.begin(day,['serial-a'])
    def child():
        def fault(s):
            if s==stage:os._exit(71)
        display(env).step(day,fault=fault)
    process=multiprocessing.get_context('fork').Process(target=child);process.start();process.join(10)
    if process.is_alive():process.kill();process.join();pytest.fail('crash fixture timeout')
    assert process.exitcode==71
    d=display(env);finish(d,day)
    assert normalize(records(d,day))==normalize(reference(values))


def test_cancel_after_prepare_preserves_previous_publication_and_resumes(tmp_path):
    from research_budget import cancellation_scope,ResearchQueryError
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);values=[sample(start+10)]
    append_cells(env,values);close_day(env,day,bootstrap=True);d=display(env);d.begin(day,['serial-a'])
    cancelled=[False]
    def fault(stage):
        if stage=='after_display_prepared_commit':cancelled[0]=True
    with pytest.raises(ResearchQueryError),cancellation_scope(lambda:cancelled[0]):d.step(day,fault=fault)
    assert d.repository.head(d.head(day)) is None
    result=finish(d,day);assert result['raw_bytes']==0 and result['decodes']==0
    assert normalize(records(d,day))==normalize(reference(values))


def test_unassigned_identity_is_not_silently_complete(tmp_path):
    from projection_repository import Unavailable
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day)
    append_cells(env,[dict(sample(start+10),module_serial='OTHER')]);close_day(env,day,bootstrap=True)
    d=display(env);d.begin(day,['serial-a'])
    with pytest.raises(Unavailable,match='identity dispatch incomplete'):finish(d,day)
    assert d.state(day)['status']!='published_closed'


def test_channel_checkpoint_bound_and_single_raw_decode(tmp_path):
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day);append_cells(env,[sample(start+10)])
    close_day(env,day,bootstrap=True);d=display(env);d.begin(day,['serial-a'])
    reads=decodes=0;steps=0
    while True:
        result=d.step(day);steps+=1;reads+=result['raw_bytes'];decodes+=result['decodes']
        row=d.state(day)
        assert len((row['prepared'] or '').encode())<=1024*1024
        assert len((row['pending'] or '').encode())<65536
        if result['status']=='published_closed':break
        assert steps<100
    assert steps>1 and reads>0 and decodes==1


@pytest.mark.parametrize('stage',[None,'after_display_prepared_commit','after_display_commit'])
def test_channel_quantum_is_bounded_resumable_and_exact(tmp_path,monkeypatch,stage):
    env=composed(tmp_path);day=env['origin'];start,_=bounds(day)
    values=[dict(sample(start+10),voltages_mv=[3300+i for i in range(15)],temperatures_c=[20]*15),
            dict(sample(start+20),voltages_mv=[3320+i for i in range(15)],temperatures_c=[21]*15)]
    append_cells(env,values);close_day(env,day,bootstrap=True)
    d=display(env);d.begin(day,['serial-a'])
    def fault(value):
        if value==stage:raise RuntimeError('power interruption')
    if stage:
        with pytest.raises(RuntimeError,match='power interruption'):
            d.step(day,max_channels=8,fault=fault)
        d=display(env)
    calls=[];page=d.reader.page
    monkeypatch.setattr(d.reader,'page',lambda *a,**kw:(calls.append(kw['limit']),page(*a,**kw))[1])
    for turn in range(12):
        before=len(calls);old=d.state(day)
        result=d.step(day,max_channels=8)
        new=d.state(day)
        assert len(calls)-before<=1
        if old['pending'] or old['prepared']:assert len(calls)==before
        if new['pending']:
            previous=json.loads(old['pending'])['channel'] if old['pending'] else 0
            assert json.loads(new['pending'])['channel']-previous<=8
        assert result['decodes']<=1 and result['raw_bytes']<=65536
        if result['status']=='published_closed':break
    else:pytest.fail('finite channel quantum failed to complete')
    assert all(limit==1 for limit in calls)
    assert normalize(records(d,day))==normalize(reference(values))
