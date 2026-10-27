import json
from datetime import datetime,timezone
from pathlib import Path
import pytest
from application_writer import ApplicationWriter
from raw_registry import RawRegistry
from source_catalog import SourceCatalog
from raw_registry_catalog import RegistryCatalogConsumer
from confirmed_prefix_reader import ConfirmedPrefixReader
from application_catalog import CatalogTail
from prepared_policy_reader import PreparedPolicyReader
from hycube_evidence import HycubePolicyHistory,policy_observation
from test_application_catalog import finish
from projection_repository import Unavailable


def setup(tmp_path):
    registry=RawRegistry.create(tmp_path/'registry')
    writer=ApplicationWriter(registry,tmp_path/'policy',stream='policy',writer_epoch='synthetic')
    catalog=SourceCatalog(tmp_path/'catalog')
    consumer=RegistryCatalogConsumer(registry,catalog,record_format='hycube_policy_observation')
    tail=CatalogTail(consumer,name='policy');tail.initialize()
    reader=PreparedPolicyReader(catalog,prefix_reader=ConfirmedPrefixReader(registry),consumer=consumer)
    return HycubePolicyHistory(writer.root,append_backend=writer,window_backend=reader),reader,tail


def observation(stamp,normal=82):
    return policy_observation(json.dumps({'normalMode':normal,'bufferMode':3,
        'emergency':92-normal,'batProtection':5}).encode(),stamp)


def iso(stamp):return datetime.fromtimestamp(stamp,timezone.utc).isoformat()


def test_policy_writer_restart_change_and_selected_segments_match_original(tmp_path,monkeypatch):
    history,reader,tail=setup(tmp_path)
    baseline=HycubePolicyHistory(tmp_path/'legacy')
    stamp=1788307200
    for offset,normal in [(0,82),(300,82),(600,72)]:
        record=observation(stamp+offset,normal)
        history.append(record);baseline.append(record)
        finish(tail)
        history=HycubePolicyHistory(history.directory,append_backend=history.append_backend,window_backend=reader)
    assert next(history.directory.glob('*.jsonl')).read_bytes()==next(baseline.directory.glob('*.jsonl')).read_bytes()
    args=dict(timestamp_from=iso(stamp+300),timestamp_to=iso(stamp+900))
    expected=baseline.query(**args)
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('policy discovered old files'))
    assert history.query(**args)==expected
    assert reader.predecessor_decodes==0
    assert reader.window.metrics['decodes']==2
    history.append(observation(stamp+1000,72))
    with pytest.raises(Unavailable,match='indexing pending'):
        HycubePolicyHistory(history.directory,append_backend=history.append_backend,window_backend=reader).append(observation(stamp+1100,72))
    # Same running writer can persist subsequent observations before catch-up.

    history.append(observation(stamp+1100,72));finish(tail)
    assert history._latest_valid()['effective_at']==iso(stamp+600)


@pytest.mark.parametrize('older_days',[1,300])
def test_policy_predecessor_cost_skips_unrelated_age(tmp_path,monkeypatch,older_days):
    history,reader,tail=setup(tmp_path);stamp=1788307200
    # Explicit synthetic setup uses confirmed append bytes already carrying
    # original policy fields; live writer parity is covered separately above.
    for day in range(older_days):
        record=observation(stamp-(older_days-day)*86400)
        record.update(policy_changed=True,effective_at=record['observed_at'])
        history.append_backend.append(history.directory/(str(day)+'.jsonl'),
            (json.dumps(record)+'\n').encode())
    for _ in range(5000):
        if tail.step().get('status')=='caught_up':break
    else:pytest.fail('explicit synthetic policy preparation did not finish')
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('policy age scan'))
    segments=history.query(timestamp_from=iso(stamp),timestamp_to=iso(stamp+60))
    assert len(segments)==1 and segments[0]['boundary_normal_passive']==18
    assert reader.predecessor_decodes==1 and reader.window.metrics['decodes']==1
    with reader.catalog.connection() as db:
        assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0


def test_existing_hycube_factory_writes_raw_policy_and_projection(tmp_path,monkeypatch):
    from hycube_evidence import collector_from_options,HycubeEvidenceWriter
    from hycube_projection import HycubeProjectionStore
    from test_hycube_evidence import Response,PAYLOAD
    from research_api import GuardianResearchApi
    from test_research_api import environment,get
    from dataclasses import replace
    history,reader,tail=setup(tmp_path)
    registry=RawRegistry.create(tmp_path/'system-registry')
    backend=ApplicationWriter(registry,tmp_path/'system',stream='hycube',writer_epoch='synthetic')
    system=HycubeEvidenceWriter(backend.root,append_backend=backend)
    projection_root=tmp_path/'projection'
    projection_registry=RawRegistry.create(tmp_path/'projection-registry')
    projection_backend=ApplicationWriter(projection_registry,projection_root,stream='hycube-projection',writer_epoch='synthetic')
    inventory=tmp_path/'projection-inventory.json'
    inventory.write_text(json.dumps({'directory':str(projection_root.resolve()),'origin':{'explicit':'empty synthetic projection root'},
        'counts':{'projection_days':0,'projection_bytes':0,'complete_days':0,'open_days':0,'invalid_days':0}}))
    projection=HycubeProjectionStore(projection_root,flush_records=1,append_backend=projection_backend,inventory_path=inventory)
    stamp=1788307200
    requests=[]
    class Transport:
        def open(self,request,timeout):
            requests.append(request)
            assert request.get_method()=='GET' and request.data is None
            return Response(PAYLOAD if request.full_url.endswith('/data_row/') else
                {'normalMode':82,'bufferMode':3,'emergency':10,'batProtection':5})
    collector=collector_from_options({'hycube_evidence_enabled':True,'hycube_base_url':'http://localhost'},
        system.directory,history.directory,projection.directory,writer=system,policy_writer=history,
        projection_store=projection,opener=Transport(),clock=lambda:stamp)
    collector.collect_once();collector.collect_policy_once();finish(tail)
    assert len(requests)==2 and projection.status()['records_written']==1
    assert registry.state(backend.source(next(system.directory.glob('*.jsonl'))))['end']>0
    old,_,_=environment(tmp_path/'research')
    api=GuardianResearchApi(replace(old.paths,hycube_history=system.directory,hycube_policy=history.directory),
        window_backend=object(),identity_backend=old.identity,policy_reader=reader)
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('Research policy scanned directories'))
    route='timeseries?source=guardian.hycube&metric=policy&from='+iso(stamp).replace('+00:00','Z')+'&to='+iso(stamp+60).replace('+00:00','Z')
    response=get(api,route)
    assert response.status==200,response.body
    assert response.body['data']['segments'][0]['boundary_normal_passive']==18
    coverage=get(api,'coverage?physical_serial=SERIAL-M4&datasets=policy&from='+iso(stamp).replace('+00:00','Z')+'&to='+iso(stamp+60).replace('+00:00','Z'))
    assert coverage.status==200,coverage.body
    assert coverage.body['data']['datasets'][0]['quality']=='complete'
    collector.stop()
    restored=HycubeProjectionStore(projection_root,append_backend=projection_backend,inventory_path=inventory)
    assert restored.status()['projection_days']==1
    assert restored.status()['projection_bytes']==projection.status()['projection_bytes']
    from test_application_writers import consume
    projection_catalog=SourceCatalog(tmp_path/'projection-catalog')
    consume(projection_registry,projection_catalog,'hycube_history_projection')
    from indexed_history_window import IndexedHistoryWindow
    with IndexedHistoryWindow(projection_catalog).read(stamp,stamp) as rows:
        values=[record for record,_ in rows]
    assert len(values)==1 and values[0]['battery_capacity']==PAYLOAD['BatteryCapacity']


@pytest.mark.parametrize('mutation',['append','revision'])
def test_policy_query_holds_one_source_frontier_across_predecessor_and_window(tmp_path,monkeypatch,mutation):
    from test_source_catalog import registration
    from research_budget import ResearchQueryError
    history,reader,tail=setup(tmp_path);stamp=1788307200
    history.append(observation(stamp));finish(tail)
    history.append(observation(stamp+600,72));finish(tail)
    args=dict(timestamp_from=iso(stamp+300),timestamp_to=iso(stamp+900))
    expected=history.query(**args)
    original=reader.window.resolver.read_refs
    changed=False
    def race(refs):
        nonlocal changed
        if not changed and refs:
            changed=True
            if mutation=='append':
                history.append(observation(stamp+750,62));finish(tail)
            else:
                ref=refs[0]
                reader.catalog.register(**registration(Path(ref.path),source=ref.source_id,
                    revision='explicit-replacement',expected_revision=ref.revision,context={'record_format':'hycube_policy_observation'}))
        return original(refs)
    monkeypatch.setattr(reader.window.resolver,'read_refs',race)
    if mutation=='revision':
        with pytest.raises(ResearchQueryError,match='revision changed'):
            history.query(**args)
    else:
        assert history.query(**args)==expected
    assert changed
    with reader.catalog.connection() as db:
        assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0



def test_full_first_page_plus_predecessor_has_no_loss_or_duplicate(tmp_path,monkeypatch):
    from types import SimpleNamespace
    history,reader,tail=setup(tmp_path);stamp=1788307200
    history.append(observation(stamp));finish(tail)
    records=[{'schema_version':1,'record_type':'hycube_policy_observation',
        'observed_at':iso(stamp+i+1),'parse_quality':'invalid'} for i in range(256)]
    path=next(history.directory.glob('*.jsonl'))
    history.append_backend.append(path,b''.join((json.dumps(record)+'\n').encode() for record in records))
    finish(tail)
    original=reader.window.resolver.read_refs;seen=[];counts={}
    def read(refs):
        assert len(refs)<=256
        seen.extend(ref.key for ref in refs)
        return original(refs)
    monkeypatch.setattr(reader.window.resolver,'read_refs',read)
    result=history.query(timestamp_from=iso(stamp+.5),timestamp_to=iso(stamp+300),
        timing=SimpleNamespace(counts=lambda **values:counts.update(values)))
    assert len(result)==1
    assert len(seen)==len(set(seen))==257
    assert reader.window.metrics['pages']==2
    assert counts['policy_files']==reader.window.metrics['files_opened']==2
    assert counts['policy_records']==257
