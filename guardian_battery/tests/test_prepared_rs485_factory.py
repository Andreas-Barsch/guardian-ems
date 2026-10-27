"""Real passive reader factory through confirmed storage, synthetic wire only."""
from datetime import datetime
import threading
import time
import pytest
from main import create_rs485_reader
from rs485_evidence import Rs485EvidencePipeline, Rs485EvidenceWriter
from raw_registry import RawRegistry
from application_writer import ApplicationWriter
from source_catalog import SourceCatalog
from indexed_history_window import IndexedHistoryWindow
from test_application_writers import consume
from test_prepared_position_service import prepared
from test_position_history import state
from test_application_metadata import finish
from test_rs485_evidence import frame, management


def test_actual_factory_transport_pipeline_confirmed_catalog(tmp_path, monkeypatch):
    import main
    import rs485_evidence
    event,positions,tail=prepared(tmp_path)
    serial='Y225004C32250226'
    snapshot=positions.record(effective_at='2026-08-19T10:00:00Z',
        maintenance_event_id=event.maintenance_event_id,positions=state(**{'1':serial}),
        expected_latest_snapshot_id=None)
    finish(tail)
    monkeypatch.setattr(positions.log,'read_all',lambda:pytest.fail('position Raw replay'))
    monkeypatch.setattr(rs485_evidence,'documented_position_at',lambda *a:pytest.fail('legacy identity lookup'))
    monkeypatch.setattr(main,'discover_serial_devices',lambda:pytest.fail('hardware discovery'))
    registry=RawRegistry.create(tmp_path/'bus-registry')
    backend=ApplicationWriter(registry,tmp_path/'bus',stream='rs485',writer_epoch='synthetic')
    writer=Rs485EvidenceWriter(backend.root,append_backend=backend,batch_size=1,flush_interval_seconds=.01)
    stamp=datetime.fromisoformat(snapshot.effective_at).timestamp()+10
    pipeline=Rs485EvidencePipeline(writer,wall_clock=lambda:stamp,position_reader=positions)
    payload=b''.join(item.raw_frame for item in (
        frame(adr=6,code=0x93,info=b'\x06'),frame(adr=6,code=0,info=b'\x06'+serial.encode()),
        frame(adr=6,code=0x92),frame(adr=6,code=0,info=management())))
    class Wire:
        def __init__(self):self.data=payload;self.closed=threading.Event()
        def read(self,size):
            if self.data:
                result,self.data=self.data,b''
                return result
            self.closed.wait(.01)
            return b''
        def close(self):self.closed.set()
        def write(self,*args):pytest.fail('passive reader wrote to wire')
    wire=Wire();opened=[]
    def opener(port,baudrate,timeout):
        opened.append((port,baudrate))
        return wire
    reader=create_rs485_reader({'rs485_sniffer_enabled':True},str(tmp_path/'console'),pipeline,
        port_resolver=lambda:str(tmp_path/'synthetic-wire'),serial_opener=opener,wall_clock=lambda:stamp)
    writer.start();reader.start()
    try:
        deadline=time.monotonic()+5
        while writer.status()['records_written']<4 and time.monotonic()<deadline:
            assert writer.last_error is None
            time.sleep(.01)
        assert writer.status()['records_written']==4
        assert reader.management()[6]['discharge_current_limit_a']==-25.0
    finally:
        reader.stop();writer.stop()
    assert wire.closed.is_set() and len(opened)==1
    catalog=SourceCatalog(tmp_path/'bus-catalog');consume(registry,catalog,'rs485')
    with IndexedHistoryWindow(catalog).read(stamp,stamp) as rows:
        values=[value for value,_ in rows]
    assert len(values)==4
    assert values[-1]['physical_serial']==serial and values[-1]['position']==1
    assert values[-1]['position_history_id']==snapshot.position_history_id
    assert b''.join(bytes.fromhex(value['raw_frame']) for value in values)==payload
