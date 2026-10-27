import json
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from application_writer import ApplicationWriter
from raw_registry import RawRegistry,RegistryConflict
from source_catalog import SourceCatalog
from raw_registry_catalog import RegistryCatalogConsumer
from indexed_history_window import IndexedHistoryWindow
from work_scheduler import WorkScheduler
from cell_history import CellHistoryWriter
from cell_acquisition import acquire_cell_round
from test_cell_acquisition import module,rows
from config_history import ConfigHistory
from test_config_history import options
from position_history import PositionHistoryLog
from test_history_epoch_adapter import position
from rs485_evidence import Rs485EvidenceWriter
from hycube_evidence import HycubeEvidenceWriter


def binding(tmp_path,kind):
    registry=RawRegistry.create(tmp_path/(kind+'-journal'));root=tmp_path/kind
    return registry,root,ApplicationWriter(registry,root,stream=kind,writer_epoch='synthetic-application',scheduler=WorkScheduler())


def consume(registry,catalog,kind):
    consumer=RegistryCatalogConsumer(registry,catalog,record_format=kind);consumer.initialize()
    for _ in range(100):
        result=consumer.step(expected_cursor=consumer.cursor(),max_records=1)
        if result['caught_up']:break
    else:pytest.fail('catalog journal not caught up')
    for source in catalog.registered():
        for _ in range(100):
            state=catalog.progress(source['source'],source['revision'])
            if state['cursor']==state['frontier']:break
            catalog.index_step(source['source'],source['revision'],expected_cursor=state['cursor'],max_records=2)
        else:pytest.fail('source index incomplete')
    return consumer


def test_real_acquisition_and_cell_writer_confirm_raw_before_indexed_read(tmp_path):
    registry,root,backend=binding(tmp_path,'cell');writer=CellHistoryWriter(root,append_backend=backend)
    calls=[]
    class Console:
        def command(self,command):calls.append(command);return command
    identity=SimpleNamespace(identity_at=lambda pos,when:('SERIAL-'+str(pos),'PHS-test'))
    acquired=acquire_cell_round([module(1),module(2)],Console(),identity,writer,parse_bat_fn=rows,wall_clock=lambda:1010)
    assert calls==['bat 1','bat 2'] and len(acquired)==2
    catalog=SourceCatalog(tmp_path/'catalog');consume(registry,catalog,'cell')
    with IndexedHistoryWindow(catalog).read(1000,1020) as records:
        actual=[v for v,_ in records]
    assert [v['module'] for v in actual]==[1,2]
    assert all(v['cell_sample_at']==1010 and v['pwr_sample_at']==1000 for v in actual)
    reopened=ApplicationWriter(RawRegistry(registry.root),root,stream='cell',writer_epoch='synthetic-application')
    CellHistoryWriter(root,append_backend=reopened).append(dict(actual[0],timestamp=1030))
    consume(registry,catalog,'cell')
    with IndexedHistoryWindow(catalog).read(1021,1040) as records:assert [v['timestamp'] for v,_ in records]==[1030]


def test_real_config_and_position_writers_preserve_formats_and_restart_tip(tmp_path):
    registry,root,backend=binding(tmp_path,'metadata');root.mkdir()
    clock=lambda:datetime(2026,10,20,tzinfo=timezone.utc)
    config=ConfigHistory(root/'config.jsonl',append_backend=backend,clock=clock)
    first=config.record_if_changed(options());assert first
    assert config.record_if_changed(options()) is None
    assert ConfigHistory(root/'config.jsonl',append_backend=backend,clock=clock).record_if_changed(options()) is None
    log=PositionHistoryLog(root/'position.jsonl',append_backend=backend);snapshot=position(100,'A');log.append(snapshot)
    assert log.read_all()==[snapshot]
    assert json.loads((root/'config.jsonl').read_text())==first
    assert len(registry.read_page()['records'])==4


@pytest.mark.parametrize('kind',['rs485','hycube_system_observation'])
def test_real_auxiliary_writer_reaches_correct_catalog_decoder(tmp_path,kind):
    registry,root,backend=binding(tmp_path,kind);root.mkdir()
    if kind=='rs485':
        record={'schema_version':1,'record_type':'frame','timestamp':'2026-10-20T00:00:00+00:00','raw_ascii':'test','adr':2}
        writer=Rs485EvidenceWriter(root,append_backend=backend);writer._write_batch([record]);writer.stop()
    else:
        record={'schema_version':1,'record_type':kind,'received_at':'2026-10-20T00:00:00+00:00','data':{}}
        receipt=HycubeEvidenceWriter(root,append_backend=backend).append_with_receipt(record)
        assert receipt.end_offset>receipt.start_offset
    catalog=SourceCatalog(tmp_path/'catalog');consumer=consume(registry,catalog,kind)
    stamp=datetime(2026,10,20,tzinfo=timezone.utc).timestamp()
    with IndexedHistoryWindow(catalog).read(stamp,stamp) as records:assert [v for v,_ in records]==[record]
    with pytest.raises(RegistryConflict):RegistryCatalogConsumer(registry,catalog,record_format='cell').initialize()


def test_candidate_writer_rejects_utc_foreign_path_and_nonempty_legacy(tmp_path):
    registry,root,backend=binding(tmp_path,'cell');root.mkdir()
    with pytest.raises(ValueError):ApplicationWriter(registry,root,stream='cell',writer_epoch='test',namespace='utc')
    with pytest.raises(RegistryConflict):backend.append(tmp_path/'outside.jsonl',b'{}\n')
    old=root/'existing.jsonl';old.write_bytes(b'{"legacy":true}\n')
    with pytest.raises(RegistryConflict):backend.append(old,b'{}\n')
    assert old.read_bytes()==b'{"legacy":true}\n'



def test_config_epoch_fanout_explicitly_excludes_other_metadata_streams(tmp_path):
    from history_epoch_adapter import EpochStore,EpochLogConsumer
    registry,root,backend=binding(tmp_path,'metadata-fanout');root.mkdir()
    config_path=root/'config.jsonl';technical_path=root/'technical.jsonl'
    first=ConfigHistory(config_path,append_backend=backend).record_if_changed(options())
    backend.append(technical_path,b'{"type":"status_changed","timestamp":100}\n')
    store=EpochStore(tmp_path/'epochs.sqlite');consumer=EpochLogConsumer(registry,store)
    mapping={backend.source(config_path):'config',backend.source(technical_path):None}
    consumer.initialize(mapping)
    for _ in range(30):
        if consumer.step(expected_cursor=consumer.cursor(),max_records=1)['caught_up']:break
    else:pytest.fail('config epoch fanout not complete')
    with store.connection() as db:
        rows=db.execute('SELECT kind,identity FROM epoch').fetchall()
    assert [tuple(row) for row in rows]==[('config',first['config_id'])]
    assert consumer.step(expected_cursor=consumer.cursor())['raw_bytes']==0
    backend.append(root/'unmapped.jsonl',b'{}\n')
    with pytest.raises(RegistryConflict,match='unmapped'):
        consumer.step(expected_cursor=consumer.cursor())
