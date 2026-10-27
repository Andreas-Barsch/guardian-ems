import pytest
from test_research_api import environment,get
from test_source_catalog import registration,index_all
from research_api import GuardianResearchApi
from source_catalog import SourceCatalog
from indexed_history_window import IndexedHistoryWindow
from read_metadata_index import PreparedMetadata
from projection_repository import ProjectionRepository
from work_scheduler import WorkScheduler


def local(tmp_path,monkeypatch):
    old,first,second=environment(tmp_path)
    catalog=SourceCatalog(tmp_path/'index.sqlite')
    catalog.register(**registration(old.paths.cell_history/'2026-09-11.jsonl'));index_all(catalog)
    metadata=PreparedMetadata(ProjectionRepository(tmp_path/'metadata.sqlite'),'test')
    metadata.initialize();metadata.prepare_identity(old.identity)
    api=GuardianResearchApi(old.paths,cursor_secret=b'test',window_backend=IndexedHistoryWindow(catalog),
        identity_backend=metadata,scheduler=WorkScheduler())
    monkeypatch.setattr(api.series,'_paths',lambda *a:pytest.fail('legacy path discovery'))
    monkeypatch.setattr(api,'_directory_fingerprint',lambda *a:pytest.fail('directory scan'))
    monkeypatch.setattr(api,'_file_signature',lambda *a:pytest.fail('identity file scan'))
    return old,api,catalog


@pytest.mark.parametrize('route',[
    'topology?timestamp=2026-09-11T10:00:00Z',
    'identity-epochs?physical_serial=SERIAL-M4&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z',
    'timeseries?physical_serial=SERIAL-M4&metric=soc&resolution=full&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z',
    'coverage?physical_serial=SERIAL-M4&datasets=soc,module_current&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z'])
def test_actual_research_routes_indexed_reference_parity(tmp_path,monkeypatch,route):
    old,api,catalog=local(tmp_path,monkeypatch)
    expected=get(old,route);actual=get(api,route)
    assert actual.status==expected.status==200,actual.body
    a=dict(actual.body['data']);b=dict(expected.body['data'])
    for key in ('source_fingerprint','data_state'):a.pop(key,None);b.pop(key,None)
    assert a==b


def test_research_cancel_no_fallback_and_closed_snapshot(tmp_path,monkeypatch):
    _,api,catalog=local(tmp_path,monkeypatch)
    response=api.handle('GET','/api/research/topology',cancelled=lambda:True)
    assert response.status==503 and response.body['error']['code']=='cancelled'
    assert get(api,'daily-diagnostics?date=2026-09-11').status==503
    with catalog.connection() as db:assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0


def test_local_factory_real_history_and_research(tmp_path):
    from test_publication_history_readers import setup,iso
    from canonical_day_repository import bounds
    from research_identity import ResearchIdentityResolver
    from test_research_api import snapshot
    from research_api import ResearchPaths
    from local_history_composition import compose_local_history
    env,prep,index,readers=setup(tmp_path);start,_=bounds(env['origin'])
    metadata=PreparedMetadata(env['repo'],'identity-test');metadata.initialize()
    metadata.prepare_identity(ResearchIdentityResolver([snapshot(iso(start),{1:'SERIAL-A'})]));metadata.prepare_timeline([])
    paths=ResearchPaths(tmp_path/'cells',tmp_path/'unused-position',tmp_path/'unused-maintenance',
        tmp_path/'canonical',tmp_path/'daily',display_history=tmp_path/'display')
    composed=compose_local_history(paths=paths,cell_catalog=env['catalog'],metadata=metadata,
        publication_index=index,canonical_repository=env['project'],display_repository=prep.display,
        streams=['serial-a'],scheduler=WorkScheduler(),cursor_secret=b'test')
    suffix='from='+iso(start).replace('+00:00','Z')+'&to='+iso(start+900).replace('+00:00','Z')
    response=composed.history.handle('GET','/api/history/series?metrics=soc,current&selected_modules=1&'+suffix)
    assert response.status==200,response.body
    response=get(composed.research,'phases?physical_serial=SERIAL-A&'+suffix)
    assert response.status==200 and response.body['data']['available'],response.body


@pytest.mark.parametrize('kind', ['rs485', 'hycube'])
def test_composed_auxiliary_reads_keep_confirmed_prefix_while_catalog_lags(tmp_path, kind):
    from test_publication_history_readers import setup, iso
    from test_application_writers import binding, consume
    from canonical_day_repository import bounds
    from local_history_composition import compose_local_history
    from research_api import ResearchPaths
    from rs485_evidence import Rs485EvidenceWriter
    from hycube_evidence import HycubeEvidenceWriter
    from research_identity import ResearchIdentityResolver
    from test_research_api import snapshot
    env, prep, index, _ = setup(tmp_path)
    start, _ = bounds(env['origin'])
    registry, root, backend = binding(tmp_path, kind)
    root.mkdir()
    if kind == 'rs485':
        writer = Rs485EvidenceWriter(root, append_backend=backend)
        def append(stamp):
            writer._write_batch([{'schema_version':1, 'record_type':'frame',
                'timestamp':iso(stamp), 'adr':2, 'raw_ascii':'synthetic',
                'physical_serial':'SERIAL-A', 'position':1, 'position_history_id':'synthetic',
                'decoded':{'charge_enable':True}}])
        record_format = 'rs485'
    else:
        writer = HycubeEvidenceWriter(root, append_backend=backend)
        def append(stamp):
            writer.append({'schema_version':1, 'record_type':'hycube_system_observation',
                'received_at':iso(stamp), 'BatteryCapacity':82,
                'parse_quality':'complete', 'configured_interval_seconds':5})
        record_format = 'hycube_system_observation'
    try:
        append(start+10)
        catalog = SourceCatalog(tmp_path/(kind+'-catalog'))
        consume(registry, catalog, record_format)
        metadata = PreparedMetadata(env['repo'], 'auxiliary-test')
        metadata.initialize()
        metadata.prepare_identity(ResearchIdentityResolver([snapshot(iso(start), {1:'SERIAL-A'})]))
        metadata.prepare_timeline([])
        paths = ResearchPaths(tmp_path/'cells', tmp_path/'unused-position', tmp_path/'unused-maintenance',
            tmp_path/'canonical', tmp_path/'daily', display_history=tmp_path/'display',
            hycube_history=root if kind == 'hycube' else None,
            rs485_history=root if kind == 'rs485' else None)
        composition = compose_local_history(paths=paths, cell_catalog=env['catalog'], metadata=metadata,
            publication_index=index, canonical_repository=env['project'], display_repository=prep.display,
            streams=['serial-a'], scheduler=WorkScheduler(), cursor_secret=b'test',
            **{kind+'_catalog':catalog, kind+'_registry':registry})
        metric = 'rs485_charge_enable' if kind == 'rs485' else 'soc'
        target = '/api/history/series?metrics='+metric+'&selected_modules=1&from='+iso(start)+'&to='+iso(start+900)
        target = target.replace('+00:00','Z')
        before = composition.history.handle('GET', target)
        assert before.status == 200, before.body
        for i in range(40):
            append(start+20+i)
        after = composition.history.handle('GET', target)
        assert after.status == 200, after.body
        if kind == 'hycube':
            assert after.body['soc_timeline']['hycube_series']['points'] == before.body['soc_timeline']['hycube_series']['points']
            assert len(after.body['soc_timeline']['hycube_series']['points']) == 1
            series = composition.history.hycube_series
        else:
            assert after.body['series'][0]['points'] == before.body['series'][0]['points']
            assert len(after.body['series'][0]['points']) == 1
            series = composition.history.rs485_series
        assert series.window_backend.metrics['confirmed_prefixes'][0]['pending_confirmed_bytes'] > 0
    finally:
        if kind == 'rs485':
            writer.stop()
