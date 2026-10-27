import json
from pathlib import Path
import pytest
from application_writer import ApplicationWriter
from raw_registry import RawRegistry
from hycube_projection import HycubeProjectionStore, load_projection_metadata
from hycube_evidence import HycubeEvidenceWriter
from test_hycube_projection import raw_record
from projection_repository import Unavailable


class PowerLoss(BaseException):
    pass


def setup(tmp_path, fault=None):
    root = tmp_path/'projection'
    registry = RawRegistry.create(tmp_path/'registry')
    backend = ApplicationWriter(registry, root, stream='projection', writer_epoch='test')
    inventory = tmp_path/'inventory.json'
    inventory.write_text(json.dumps(dict(directory=str(root.resolve()), origin={'test':True},
        counts=dict(projection_days=0,projection_bytes=0,complete_days=0,open_days=0,invalid_days=0))))
    store = HycubeProjectionStore(root, append_backend=backend, inventory_path=inventory,
        flush_records=1, publication_fault=fault)
    return store, backend, inventory


@pytest.mark.parametrize('point',['after_intent','after_projection','after_metadata','after_inventory'])
@pytest.mark.parametrize('capacity',[42,None])
def test_restart_finishes_exact_publication_without_scan_or_duplicate(tmp_path,monkeypatch,point,capacity):
    def fault(stage):
        if stage == point:
            raise PowerLoss(stage)
    store,backend,inventory = setup(tmp_path,fault)
    writer=HycubeEvidenceWriter(tmp_path/'raw')
    record=raw_record('2026-09-02T10:00:00+00:00',capacity)
    receipt=writer.append_with_receipt(record)
    with pytest.raises(PowerLoss):
        store.append_live(record,receipt)
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('recovery scanned history'))
    monkeypatch.setattr(Path,'iterdir',lambda *a:pytest.fail('recovery enumerated history'))
    # Fresh writer and registry objects, with no old Python pending buffer.
    backend=ApplicationWriter(RawRegistry(backend.registry.root),backend.root,
        stream='projection',writer_epoch='test')
    restored=HycubeProjectionStore(store.directory,append_backend=backend,
        inventory_path=inventory,flush_records=1)
    assert restored.status()['state']=='recovered'
    path=store.projection_path('2026-09-02')
    before=path.read_bytes()
    assert len(before.splitlines()) == (0 if capacity is None else 1)
    meta=load_projection_metadata(store.directory,'2026-09-02')
    assert meta['confirmed_raw_end_offset']==receipt.end_offset
    assert meta['projection_end_offset']==len(before)
    assert restored.status()['projection_days']==1
    assert restored.status()['projection_bytes']==len(before)
    second=raw_record('2026-09-02T10:00:05+00:00',43)
    assert restored.append_live(second,writer.append_with_receipt(second))
    assert path.read_bytes().startswith(before)
    assert len(path.read_bytes().splitlines())==(1 if capacity is None else 2)
    assert restored.status()['gap_skips']==0
    assert restored.status()['projection_days']==1
    assert restored.status()['projection_bytes']==path.stat().st_size
    reopened=HycubeProjectionStore(store.directory,append_backend=backend,inventory_path=inventory)
    assert reopened.status()['projection_bytes']==path.stat().st_size
    assert not inventory.with_name(inventory.name+'.publication.json').exists()


def test_independent_inventory_change_is_not_overwritten(tmp_path):
    def fault(stage):
        if stage=='after_projection':raise PowerLoss()
    store,backend,inventory=setup(tmp_path,fault)
    writer=HycubeEvidenceWriter(tmp_path/'raw')
    row=raw_record('2026-09-02T10:00:00+00:00',42)
    with pytest.raises(PowerLoss):store.append_live(row,writer.append_with_receipt(row))
    changed=json.loads(inventory.read_text());changed['origin']={'other':True}
    inventory.write_text(json.dumps(changed));before=inventory.read_bytes()
    with pytest.raises(Unavailable,match='changed independently'):
        HycubeProjectionStore(store.directory,append_backend=backend,inventory_path=inventory)
    assert inventory.read_bytes()==before


@pytest.mark.parametrize('point',['after_projection','after_metadata','after_inventory'])
def test_same_process_flush_recovers_after_io_error(tmp_path,point):
    armed=[True]
    def fault(stage):
        if armed[0] and stage==point:
            armed[0]=False
            raise OSError('synthetic interrupted publication')
    store,backend,inventory=setup(tmp_path,fault)
    writer=HycubeEvidenceWriter(tmp_path/'raw')
    row=raw_record('2026-09-02T10:00:00+00:00',42)
    assert not store.append_live(row,writer.append_with_receipt(row))
    assert store.status()['state']=='error'
    assert 'synthetic interrupted' in store.status()['last_error']
    store.flush()
    assert store.status()['state']=='recovered'
    path=store.projection_path('2026-09-02')
    assert len(path.read_bytes().splitlines())==1
    assert store.status()['projection_bytes']==path.stat().st_size


@pytest.mark.parametrize('point',['after_projection','after_metadata','after_inventory'])
def test_actual_process_exit_then_fresh_process_recovers(tmp_path,point):
    import os
    import subprocess
    import sys
    store,backend,inventory=setup(tmp_path)
    script=r'''
import os, sys
from pathlib import Path
from application_writer import ApplicationWriter
from raw_registry import RawRegistry
from hycube_evidence import HycubeEvidenceWriter
from hycube_projection import HycubeProjectionStore
root=Path(sys.argv[1]); point=sys.argv[2]
backend=ApplicationWriter(RawRegistry(root/'registry'),root/'projection',stream='projection',writer_epoch='test')
def fault(stage):
    if stage==point:os._exit(73)
store=HycubeProjectionStore(root/'projection',append_backend=backend,inventory_path=root/'inventory.json',flush_records=1,publication_fault=fault)
if point != 'recover':
    row={'record_type':'hycube_system_observation','received_at':'2026-09-02T10:00:00+00:00','BatteryCapacity':42}
    writer=HycubeEvidenceWriter(root/'raw')
    store.append_live(row,writer.append_with_receipt(row))
else:
    import json
    print(json.dumps(store.status()))
'''
    env=dict(os.environ)
    crashed=subprocess.run([sys.executable,'-c',script,str(tmp_path),point],env=env,capture_output=True,text=True)
    assert crashed.returncode==73,crashed.stderr
    restored=subprocess.run([sys.executable,'-c',script,str(tmp_path),'recover'],env=env,capture_output=True,text=True)
    assert restored.returncode==0,restored.stderr
    state=json.loads(restored.stdout)
    assert state['state']=='recovered' and state['projection_days']==1
    path=store.projection_path('2026-09-02')
    assert state['projection_bytes']==path.stat().st_size
    assert len(path.read_bytes().splitlines())==1
