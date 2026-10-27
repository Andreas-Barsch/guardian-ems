import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from application_writer import ApplicationWriter
from raw_registry import RawRegistry
from raw_registry_catalog import RegistryCatalogConsumer
from source_catalog import SourceCatalog
from application_catalog import CatalogTail
from prepared_rs485_identity import restore_identities
from rs485_evidence import decode_identity_record,Rs485EvidencePipeline
from projection_repository import Unavailable,Conflict


def setup(tmp_path):
    registry=RawRegistry.create(tmp_path/'registry')
    writer=ApplicationWriter(registry,tmp_path/'raw',stream='bus',writer_epoch='test')
    catalog=SourceCatalog(tmp_path/'catalog')
    consumer=RegistryCatalogConsumer(registry,catalog,record_format='rs485')
    tail=CatalogTail(consumer,name='bus');tail.initialize()
    return writer,catalog,consumer,tail


def record(serial='Y225004C32250226'):
    return dict(record_type='frame',direction='response',paired_command=0x93,
        checksum_valid=True,frame_complete=True,request_matched=True,adr=2,
        timestamp='2026-01-01T00:00:00Z',info_raw=(b'\x02'+serial.encode()).hex(),
        decoder_supported=True,decoded={'serial_string':serial})


def drain(tail):
    for _ in range(500):
        if tail.step().get('status')=='caught_up':return
    pytest.fail('bounded fixture preparation did not finish')


def test_restart_uses_index_not_old_raw_files_and_preserves_identity_provenance(tmp_path,monkeypatch):
    writer,catalog,consumer,tail=setup(tmp_path)
    first=record()
    for day in range(100):
        writer.append(writer.root/(f'{day:04}.jsonl'),(json.dumps(first)+'\n').encode())
        drain(tail)
    latest=record('Y225004C32250227')
    writer.append(writer.root/'0099.jsonl',(json.dumps(latest)+'\n').encode());drain(tail)
    opened=Path.open
    def protected(path,*args,**kwargs):
        if path.suffix=='.jsonl':pytest.fail('restart read a Raw file')
        return opened(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',protected)
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('restart enumerated directory'))
    result=restore_identities(SourceCatalog(catalog.path),consumer)
    assert result=={2:decode_identity_record(latest)}
    assert result[2]['identity_known'] and not result[2]['identity_currently_confirmed']
    pipeline=Rs485EvidencePipeline(SimpleNamespace(),position_history_path=tmp_path/'positions')
    pipeline.restore_identities(result)
    assert pipeline._latest_identity[2]['serial_string']==latest['decoded']['serial_string']
    assert isinstance(pipeline._latest_identity[2]['timestamp'],float)
    with pytest.raises(ValueError,match='already active'):pipeline.restore_identities(result)


def test_restart_refuses_unprepared_new_identity_and_unconfirmed_rewrite(tmp_path):
    writer,catalog,consumer,tail=setup(tmp_path)
    path=writer.root/'day.jsonl'
    writer.append(path,(json.dumps(record())+'\n').encode())
    with pytest.raises(Unavailable,match='completed confirmed'):restore_identities(catalog,consumer)
    drain(tail)
    assert restore_identities(catalog,consumer)[2]['serial_string']=='Y225004C32250226'
    path.write_bytes(path.read_bytes().replace(b'Y225004C32250226',b'Y225004C32250229'))
    with pytest.raises(Conflict,match='physical source'):restore_identities(catalog,consumer)


def test_restart_rejects_same_size_restored_mtime_rewrite(tmp_path):
    import os
    writer,catalog,consumer,tail=setup(tmp_path)
    path=writer.root/'day.jsonl'
    writer.append(path,(json.dumps(record())+'\n').encode());drain(tail)
    before=path.stat();path.write_bytes(path.read_bytes().replace(b'Y225004C32250226',b'Y225004C32250229'))
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(Conflict,match='source changed'):restore_identities(catalog,consumer)
