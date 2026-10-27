import time
from dataclasses import replace
import pytest
from research_api import GuardianResearchApi
from source_catalog import SourceCatalog
from indexed_history_window import IndexedHistoryWindow
from metadata_revision import MetadataRevision,RevisionMetadataReader
from projection_repository import ProjectionRepository
from test_source_catalog import registration,index_all
from test_research_api import environment,get,write_jsonl
from test_metadata_revision import publish


@pytest.mark.parametrize('old_count',[1,300])
def test_low_voltage_route_uses_bounded_rs485_window_and_identity_checkpoint(tmp_path,monkeypatch,old_count):
    old,first,second=environment(tmp_path)
    bus=tmp_path/'bus'/'2026-09-11.jsonl'
    identity={'schema_version':1,'record_type':'frame','timestamp':'2026-09-11T09:30:00+00:00',
        'direction':'response','paired_command':0x93,'checksum_valid':True,'frame_complete':True,
        'request_matched':True,'adr':4,'info_raw':'04'+b'SERIAL-M4       '.hex()}
    threshold={'schema_version':1,'record_type':'frame','timestamp':'2026-09-11T10:00:00+00:00',
        'direction':'response','paired_command':0x47,'checksum_valid':True,'frame_complete':True,
        'request_matched':True,'adr':4,'decoded':{'cell_low_voltage_alarm_limit_v':3.0}}
    write_jsonl(bus,[identity]*old_count+[threshold])
    bus_catalog=SourceCatalog(tmp_path/'bus.sqlite')
    bus_catalog.register(**registration(bus,context={'record_format':'rs485'}));index_all(bus_catalog)
    cells=SourceCatalog(tmp_path/'cells.sqlite')
    cells.register(**registration(old.paths.cell_history/'2026-09-11.jsonl'));index_all(cells)
    repo=ProjectionRepository(tmp_path/'metadata.sqlite');revision=MetadataRevision(repo,'identity')
    revision.initialize();revision.begin(provenance={'explicit':'synthetic metadata'})
    revision.apply_page('position',[first.to_dict(),second.to_dict()],source='positions',sequence=1,
        expected_version=0,provenance={'explicit':'synthetic positions'})
    publish(revision)
    paths=replace(old.paths,rs485_history=bus.parent)
    api=GuardianResearchApi(paths,cursor_secret=b'synthetic',window_backend=IndexedHistoryWindow(cells),
        identity_backend=RevisionMetadataReader(repo,'identity'),rs485_window=IndexedHistoryWindow(bus_catalog))
    monkeypatch.setattr(api.series,'_paths',lambda *a:pytest.fail('legacy cell discovery'))
    monkeypatch.setattr(type(bus),'glob',lambda *a:pytest.fail('legacy source discovery'))
    response=get(api,'events/low-voltage?physical_serial=SERIAL-M4       '
        '&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z&max_points=50')
    assert response.status==200,response.body
    result=response.body['data']['evidence'][2]
    assert result['records'][0]['decoded']==threshold['decoded']
    assert len(result['records'])==1 and result['truncated'] is False
    assert api.rs485_window.metrics['decodes']==1
    assert response.body['data']['alarm_asserted'] is None
    with bus_catalog.connection() as db:
        assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0
