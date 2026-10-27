from datetime import datetime,timezone
import pytest
from test_canonical_repository import composed,append_cells,sample,close_day
from test_history_repository_preparation import prep,drain
from canonical_day_repository import bounds
from publication_window_index import PublicationWindowIndex
from repository_history_readers import PublishedHistoryReaders
from history_series import CellHistorySeries
from indexed_history_window import IndexedHistoryWindow
from canonical_phase import CanonicalPhaseReader,clip_interval
from canonical_checkpoint_storage import unpack
from display_history_reader import DisplayHistoryReader


def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()


def setup(tmp_path):
    env=composed(tmp_path);start,_=bounds(env['origin'])
    append_cells(env,[sample(start+i*300,current=(-2 if i%2 else 2),soc=50+i) for i in range(4)])
    preparation=prep(env);preparation.enroll('serial-a');preparation.confirm_frontier(env['origin'],proof={'synthetic':True});drain(preparation,limit=1000)
    index=PublicationWindowIndex(env['repo']);index.initialize()
    for product,head,kind in [(env['project']._product('serial-a',env['origin']),env['project']._head('serial-a',env['origin']),'canonical'),
                              (preparation.display.product(env['origin']),preparation.display.head(env['origin']),'display')]:
        bundle=env['repo'].head(head);index.begin(bundle,product,env['origin'],kind)
        for _ in range(200):
            if index.step(bundle,product,env['origin'])['complete']:break
        else:raise AssertionError('window preparation did not finish')
    series=CellHistorySeries(tmp_path/'unused',window_backend=IndexedHistoryWindow(env['catalog']))
    readers=PublishedHistoryReaders(index,env['project'],preparation.display,streams=['serial-a'],full_series=series)
    return env,preparation,index,readers


def test_actual_canonical_reader_uses_pinned_index_and_original_clipper(tmp_path):
    env,p,index,readers=setup(tmp_path);day=env['origin'];start,_=bounds(day)
    reader=CanonicalPhaseReader(tmp_path/'unused',window_backend=readers)
    result=reader.query(iso(start),iso(start+901),module_number=1)
    assert result['available'],result
    state=env['project'].published_state('serial-a',day)
    expected=[];after=''
    while True:
        page=env['project'].read_page('serial-a',day,bundle=state['bundle'],after_key=after,limit=64)
        with env['repo']._connection() as db:
            for key,row in page['rows']:
                candidates=([row['value']] if key.startswith('I') and row['kind']=='diagnostic' else row.get('diagnostic_intervals',[]) if key=='Zpending' else [])
                for item in candidates:
                    value=clip_interval(unpack(db,item),iso(start),iso(start+901))
                    if value:expected.append(value)
        if len(page['rows'])<64:break
        after=page['rows'][-1][0]
    assert [{k:v for k,v in item.items() if k!='identity_epoch_id'} for item in result['diagnostic_intervals']]==expected
    assert result['data_state']['latest_raw_coverage']=='not_asserted'


def test_actual_display_reader_and_api_multi_channel_share_indexed_raw(tmp_path,monkeypatch):
    from test_history_api import api_env
    from history_api import HistoryApi
    from work_scheduler import WorkScheduler
    env,p,index,readers=setup(tmp_path);start,_=bounds(env['origin'])
    monkeypatch.setattr(readers.full_series,'_paths',lambda *args:pytest.fail('legacy source discovery'))
    _,_,old_api=api_env(tmp_path/'api')
    api=HistoryApi(readers.full_series,old_api.overlays,scheduler=WorkScheduler())
    target='/api/history/series?metrics=soc,current,cell_voltage&selected_modules=1&voltage_cell_numbers=1,2&from='+iso(start)+'&to='+iso(start+900)
    target=target.replace('+00:00','Z')
    response=api.handle('GET',target)
    assert response.status==200,response.body
    assert len(response.body['series'])==3
    assert [p['value'] for p in response.body['series'][0]['points']]==[50,51,52,53]
    assert readers.full_series.window_backend.metrics['decodes']==4
    assert len(response.body['series'][2]['points'])==8


def test_missing_published_read_index_is_explicit_no_fallback(tmp_path,monkeypatch):
    from projection_repository import Unavailable
    from research_budget import ResearchQueryError
    env,p,index,readers=setup(tmp_path);day=env['origin'];start,_=bounds(day)
    bundle=env['repo'].head(p.display.head(day))
    with env['repo']._write() as db:db.execute("UPDATE window_index_job SET complete=0 WHERE bundle=? AND kind='display'",(bundle,))
    monkeypatch.setattr(readers.full_series,'query_bundles',lambda **kw:pytest.fail('unbounded fallback'))
    with pytest.raises(ResearchQueryError,match='incomplete'):
        readers.query_bundles(requests=[{'metric':'soc'}],timestamp_from=iso(start),timestamp_to=iso(start+12*3600),module_number=1,include_samples=False)


def test_published_window_selects_only_requested_channels_and_time(tmp_path):
    env,p,index,readers=setup(tmp_path);day=env['origin'];start,_=bounds(day)
    display=DisplayHistoryReader(tmp_path/'unused',tmp_path/'unused',tmp_path/'unused',readers.full_series,window_backend=readers)
    value=display.query_bundles(requests=[{'metric':'cell_voltage','cell_numbers':(1,)}],timestamp_from=iso(start),timestamp_to=iso(start+12*3600),module_number=1,include_samples=False)
    assert value['display_observability']['history_source_mode']=='pinned_repository_publication'
    assert all(x['cell_number']==1 for x in value['series'][0]['points'])
    assert value['display_observability']['display_selected_decodes']<10


def test_published_reader_cross_day_counts_match_continuous_science(tmp_path):
    from test_canonical_repository import following,PARAMS
    from canonical_phase import CanonicalPhaseMachine
    from display_day_repository import DisplayDayRepository
    env=composed(tmp_path);day=env['origin'];start,end=bounds(day)
    records=[sample(end-180,current=1.1),sample(end-60,current=1.1),
             sample(end,current=1.1),sample(end+60,current=1.1),sample(end+180,current=1.1)]
    append_cells(env,records);close_day(env,day,bootstrap=True);close_day(env,following(day))
    index=PublicationWindowIndex(env['repo']);index.initialize()
    for selected in (day,following(day)):
        product=env['project']._product('serial-a',selected);bundle=env['repo'].head(env['project']._head('serial-a',selected))
        index.begin(bundle,product,selected,'canonical')
        for _ in range(100):
            if index.step(bundle,product,selected)['complete']:break
        else:pytest.fail('canonical window index did not finish')
    display=DisplayDayRepository(env['repo'],env['catalog'],env['project'])
    readers=PublishedHistoryReaders(index,env['project'],display,streams=['serial-a'],full_series=None)
    result=readers.query(iso(end-200),iso(end+200),physical_serial='SERIAL-A')
    reference=CanonicalPhaseMachine('SERIAL-A@observed','SERIAL-A')
    for record in records:reference.process(record,PARAMS,config_id='cfg')
    materialized=reference.materialized()
    for key in ('diagnostic_intervals','visual_intervals'):
        expected=[v for item in materialized[key] if (v:=clip_interval(item,iso(end-200),iso(end+200)))]
        # Source cursors differ by storage implementation, scientific fields do not.
        fields=('from','to','phase','canonical_from','canonical_to','canonical_sample_count','sample_count','config_id')
        assert [{k:x.get(k) for k in fields} for x in result[key]]==[{k:x.get(k) for k in fields} for x in expected]


def test_published_asof_survives_append_without_raw_fallback(tmp_path,monkeypatch):
    env,p,index,readers=setup(tmp_path);start,_=bounds(env['origin'])
    kwargs=dict(requests=[{'metric':'soc'}],timestamp_from=iso(start),timestamp_to=iso(start+43200),module_number=1,include_samples=False)
    before=readers.query_bundles(**kwargs)
    append_cells(env,[sample(start+1200,soc=35)])
    monkeypatch.setattr(readers.full_series,'query_bundles',lambda **k:pytest.fail('raw fallback on append'))
    after=readers.query_bundles(**kwargs)
    assert before['series']==after['series']
    assert before['data_state']==after['data_state']
    assert after['data_state']['latest_raw_coverage']=='not_asserted'


def test_unrelated_unprepared_physical_stream_does_not_block_selected_serial(tmp_path):
    env,p,index,readers=setup(tmp_path);start,_=bounds(env['origin'])
    env['project'].define_stream('other',origin_day=env['origin'],identity_epoch_id='OTHER',physical_serial='OTHER')
    readers.streams=('serial-a','other')
    result=readers.query(iso(start),iso(start+900),physical_serial='SERIAL-A')
    assert result['available'],result
