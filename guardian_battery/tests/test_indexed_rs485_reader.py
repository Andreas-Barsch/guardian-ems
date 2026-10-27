from datetime import datetime,timezone
import json
import pytest
from test_source_catalog import registration,index_all,write
from test_research_api import snapshot
from research_identity import ResearchIdentityResolver
from projection_repository import ProjectionRepository
from read_metadata_index import PreparedMetadata
from indexed_history_window import IndexedHistoryWindow
from source_catalog import SourceCatalog
from rs485_evidence import Rs485HistorySeries


def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()


def identity(t):
    return {'record_type':'frame','timestamp':iso(t),'adr':2,'direction':'response','paired_command':0x93,
        'checksum_valid':True,'frame_complete':True,'request_matched':True,
        'info_raw':'0248323231303035453232323132353831','decoder_supported':False,'decoded':None}


@pytest.mark.parametrize('older_days',[0,29,364,729])
def test_shared_bus_window_uses_prepared_identity_before_window(tmp_path,older_days):
    t=1800000000;catalog=SourceCatalog(tmp_path/'catalog.sqlite')
    # The identity is far before the requested evidence; preparation, not GET,
    # owns reading it. A checkpoint survives bounded index steps and reopening.
    p=write(tmp_path/'actual.jsonl',[identity(t-86400),
        {'record_type':'frame','timestamp':iso(t),'adr':2,'decoded':{'charge_enable':True,'charge_current_limit_a':7}}])
    catalog.register(**registration(p,context={'record_format':'rs485'}))
    catalog.index_step('s','r1',expected_cursor=0,max_records=1)
    catalog=SourceCatalog(catalog.path);index_all(catalog)
    for day in range(1,older_days+1):
        q=write(tmp_path/f'old-{day}.jsonl',[identity(t-86400*(day+1))])
        catalog.register(**registration(q,source=str(day),context={'record_format':'rs485'}));index_all(catalog,str(day))
    metadata=PreparedMetadata(ProjectionRepository(tmp_path/'metadata.sqlite'),'test');metadata.initialize()
    metadata.prepare_identity(ResearchIdentityResolver([snapshot(iso(t-86400),{3:'H221005E22212581'})]))
    backend=IndexedHistoryWindow(catalog)
    reader=Rs485HistorySeries(tmp_path/'absent',window_backend=backend,identity_backend=metadata)
    result=reader.query_bundles([{'metric':'rs485_charge_enable'},{'metric':'rs485_ccl'}],
        timestamp_from=iso(t),timestamp_to=iso(t),module_numbers=(1,2,3,4,5,6))
    assert [x['points'][0]['value'] for x in result]==[1,7]
    assert all(x['points'][0]['module_number']==3 for x in result)
    assert backend.metrics['decodes']==1 and backend.metrics['files_opened']==1
    assert backend.metrics['sql_vm_steps']<500
    print('BUS_SHARED_WINDOW '+json.dumps({'days':older_days+1,**backend.metrics},sort_keys=True))


def test_bus_without_confirmed_identity_is_explicitly_partial(tmp_path):
    t=1800000000;catalog=SourceCatalog(tmp_path/'catalog.sqlite')
    p=write(tmp_path/'raw.jsonl',[{'record_type':'frame','timestamp':iso(t),'adr':2,'decoded':{'charge_enable':True}}])
    catalog.register(**registration(p,context={'record_format':'rs485'}));index_all(catalog)
    metadata=PreparedMetadata(ProjectionRepository(tmp_path/'metadata.sqlite'),'test');metadata.initialize();metadata.prepare_identity(ResearchIdentityResolver([]))
    reader=Rs485HistorySeries(tmp_path/'absent',window_backend=IndexedHistoryWindow(catalog),identity_backend=metadata)
    result=reader.query_bundles([{'metric':'rs485_charge_enable'}],timestamp_from=iso(t),timestamp_to=iso(t),module_number=1)
    assert result[0]['quality']=='partial_identity' and result[0]['identity_unresolved_records']==1
    assert result[0]['points']==[]
