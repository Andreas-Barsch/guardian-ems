import json
import pytest
from application_writer import ApplicationWriter
from raw_registry import RawRegistry
from raw_registry_catalog import RegistryCatalogConsumer
from source_catalog import SourceCatalog
from application_catalog import CatalogTail
from projection_repository import Conflict


def test_confirmed_later_append_preserves_partial_index_block(tmp_path):
    registry=RawRegistry.create(tmp_path/'registry')
    writer=ApplicationWriter(registry,tmp_path/'raw',stream='cell',writer_epoch='test')
    catalog=SourceCatalog(tmp_path/'catalog')
    tail=CatalogTail(RegistryCatalogConsumer(registry,catalog),name='cell');tail.initialize()
    path=writer.root/'day.jsonl'
    payload=b''.join((json.dumps({'schema_version':1,'timestamp':i,'module':1})+'\n').encode() for i in range(20))
    writer.append(path,payload)
    for _ in range(20):
        tail.step()
        if catalog.status()['pending']:break
    # Current block is confirmed but only its first page is indexed.
    tail.step()
    progress=catalog.progress(writer.source(path),'initial')
    assert 0<progress['cursor']<len(payload)
    writer.append(path,(json.dumps({'schema_version':1,'timestamp':21,'module':1})+'\n').encode())
    for _ in range(100):
        if tail.step().get('status')=='caught_up':break
    else:pytest.fail('confirmed index did not catch up')
    with catalog.connection() as db:
        assert db.execute('SELECT count(*) FROM entry').fetchone()[0]==21
    assert catalog.progress(writer.source(path),'initial')['cursor']==path.stat().st_size


def test_index_refuses_modified_original_block_despite_later_confirmed_append(tmp_path):
    registry=RawRegistry.create(tmp_path/'registry')
    writer=ApplicationWriter(registry,tmp_path/'raw',stream='cell',writer_epoch='test')
    catalog=SourceCatalog(tmp_path/'catalog')
    tail=CatalogTail(RegistryCatalogConsumer(registry,catalog),name='cell');tail.initialize()
    path=writer.root/'day.jsonl';writer.append(path,b'{"schema_version":1,"timestamp":1,"module":1}\n')
    for _ in range(20):
        tail.step()
        if catalog.status()['pending']:break
    writer.append(path,b'{"schema_version":1,"timestamp":2,"module":1}\n')
    data=path.read_bytes();path.write_bytes(data.replace(b'"timestamp":1',b'"timestamp":9'))
    with pytest.raises(Conflict):tail.step()
    assert catalog.progress(writer.source(path),'initial')['cursor']==0


def test_background_catchup_after_300_appends_does_not_need_current_head_proof(tmp_path):
    registry=RawRegistry.create(tmp_path/'registry')
    writer=ApplicationWriter(registry,tmp_path/'raw',stream='cell',writer_epoch='test')
    catalog=SourceCatalog(tmp_path/'catalog')
    tail=CatalogTail(RegistryCatalogConsumer(registry,catalog),name='cell');tail.initialize()
    path=writer.root/'day.jsonl'
    def payload(stamp):return (json.dumps({'schema_version':1,'timestamp':stamp,'module':1})+'\n').encode()
    writer.append(path,payload(0))
    for _ in range(20):
        tail.step()
        if catalog.status()['pending']:break
    for stamp in range(1,301):writer.append(path,payload(stamp))
    # Reopen all components: no in-memory proof/cursor survives.
    catalog=SourceCatalog(catalog.path)
    resumed=CatalogTail(RegistryCatalogConsumer(RawRegistry(registry.root),catalog),name='cell')
    measures=[]
    for _ in range(1600):
        step=resumed.step();measures.append(step)
        if step.get('status')=='caught_up':break
    else:pytest.fail('old confirmed block cannot catch up')
    assert max(step.get('bytes',0) for step in measures)<=65536
    assert max(step.get('raw_bytes',0) for step in measures)<=65536
    with catalog.connection() as db:
        assert db.execute('SELECT count(*) FROM entry').fetchone()[0]==301
