"""Synthetic wire only; every filesystem destination is pytest tmp_path."""
import json
from pathlib import Path
import pytest
from rs485_identity_store import IdentityStore, observed_identity, validate, apply_observation
from rs485_identity_seed import seed_from_file
from rs485_sniffer import PassiveRs485Reader, parse_frame, decode_0x93_info
from test_rs485_sniffer import synthetic_frame, management_info
from rs485_identity import project_current_management

SERIAL = 'Y250805C30410483'
OTHER = 'Y250825C31050702'


def identity(adr=2, serial=SERIAL, at=100):
    frame = parse_frame(synthetic_frame(adr=adr, cid2_or_rtn=0, info=bytes([adr]) + serial.encode()))
    return observed_identity(frame, decode_0x93_info(adr, frame.info), at)


def feed(reader, adr=2, serial=SERIAL, rtn=0):
    reader._process(synthetic_frame(adr=adr, cid2_or_rtn=0x93, info=bytes([adr])))
    reader._process(synthetic_frame(adr=adr, cid2_or_rtn=rtn, info=bytes([adr]) + serial.encode()))


def test_restart_reads_one_file_preserves_original_time_not_live(tmp_path):
    store = IdentityStore(tmp_path/'identity.json')
    assert store.load() == {} and not store.path.exists()
    assert store.save({2: identity(at=100)})
    restored = IdentityStore(store.path).load()
    assert restored[2]['timestamp'] == 100
    assert restored[2]['identity_currently_confirmed'] is False
    assert restored[2]['source_frame_reference'] == identity()['source_frame_reference']
    assert sorted(p.name for p in tmp_path.iterdir()) == ['identity.json']


def test_live_changes_persist_and_repeated_frames_are_coalesced(tmp_path):
    now = [100]
    store = IdentityStore(tmp_path/'identity.json', refresh_seconds=300)
    reader = PassiveRs485Reader(lambda:pytest.fail('no hardware'), identity_store=store, wall_clock=lambda:now[0])
    feed(reader)
    first = store.path.read_bytes()
    assert reader.identities()[2]['identity_currently_confirmed']
    now[0] = 200
    feed(reader)
    assert store.path.read_bytes() == first and store.writes == 1
    assert reader.identities()[2]['timestamp'] == 200
    now[0] = 400
    feed(reader)
    assert store.writes == 2
    assert store.path.with_suffix('.json.previous').read_bytes() == first
    now[0] = 401
    feed(reader, serial=OTHER)
    assert store.writes == 3 and IdentityStore(store.path).load()[2]['serial_string'] == OTHER
    assert reader.identities()[2]['previous_serial'] == SERIAL


def test_address_move_invalidates_old_assignment_in_reader_and_checkpoint(tmp_path):
    now=[100];store=IdentityStore(tmp_path/'identity.json')
    reader=PassiveRs485Reader(lambda:None, identity_store=store, wall_clock=lambda:now[0])
    feed(reader);now[0]=101;feed(reader, adr=5)
    assert set(reader.identities()) == {5}
    assert reader.identities()[5]['displaced_adrs'] == [2]
    assert set(IdentityStore(store.path).load()) == {5}


def test_older_observation_cannot_undo_move_and_equal_time_conflict_is_not_assigned():
    values={2:identity(at=100)}
    assert not apply_observation(values, 3, identity(adr=3, at=99))
    assert set(values)=={2}
    apply_observation(values, 3, identity(adr=3, at=100))
    assert values == {}


@pytest.mark.parametrize('damage', ['truncated','duplicate','bad_frame','bad_timestamp','schema','too_large','bad_evidence'])
def test_corrupt_checkpoint_never_partially_restores_or_overwrites(tmp_path, damage):
    path=tmp_path/'identity.json'
    payload={'schema_version':1,'identities':[identity()]}
    if damage=='duplicate':payload['identities'].append(identity(adr=3))
    if damage=='bad_frame':payload['identities'][0]['raw_frame']='00'
    if damage=='bad_timestamp':payload['identities'][0]['timestamp']='bad'
    if damage=='bad_evidence':payload['identities'][0]['evidence']=[]
    if damage=='schema':payload['schema_version']=2
    raw=(b'{' if damage=='truncated' else b' '*(1024*1024+1) if damage=='too_large' else json.dumps(payload).encode())
    path.write_bytes(raw)
    store=IdentityStore(path)
    assert store.load()=={} and store.error
    assert not store.save({2:identity(at=200)})
    assert path.read_bytes()==raw


def test_failed_atomic_replace_keeps_last_checkpoint(tmp_path, monkeypatch):
    import rs485_identity_store as module
    store=IdentityStore(tmp_path/'identity.json');assert store.save({2:identity()})
    before=store.path.read_bytes();original=module.os.replace
    def fail(source,target):
        if Path(target)==store.path:raise OSError('synthetic disk failure')
        return original(source,target)
    monkeypatch.setattr(module.os,'replace',fail)
    assert not store.save({2:identity(serial=OTHER,at=200)})
    assert store.error and store.path.read_bytes()==before
    assert store.path.with_suffix('.json.previous').read_bytes()==before


def test_management_and_invalid_identity_do_not_refresh_or_write(tmp_path):
    now=[100];store=IdentityStore(tmp_path/'identity.json')
    reader=PassiveRs485Reader(lambda:None, identity_store=store, wall_clock=lambda:now[0])
    feed(reader);now[0]=900
    feed(reader,serial=OTHER,rtn=6)
    reader._process(synthetic_frame(cid2_or_rtn=0x92))
    reader._process(synthetic_frame(cid2_or_rtn=0,info=management_info()))
    assert reader.identities()[2]['timestamp']==100 and store.writes==1
    output=project_current_management(reader.management(),reader.identities(),position_history_path=tmp_path/'positions')
    assert output[2]['timestamp']==900
    assert output[2]['identity_observed_at']==100
    assert output[2]['identity_evidence']['source_frame_reference']==identity()['source_frame_reference']


def test_explicit_seed_validates_raw_and_preserves_timestamp_source(tmp_path):
    value=identity()
    record={'record_type':'frame','direction':'response','paired_command':147,
            'request_matched':True,'checksum_valid':True,'frame_complete':True,
            'adr':2,'raw_frame':value['raw_frame'],'timestamp':'2026-10-22T12:59:01.820771+00:00'}
    source=tmp_path/'2026-10-22.jsonl';source.write_text(json.dumps(record)+'\n')
    payload=seed_from_file(source,expected_adrs={2})
    result=validate(payload)[2]
    assert result['timestamp']==record['timestamp'] and not result['identity_currently_confirmed']
    assert result['evidence']['source']==str(source) and result['evidence']['line']==1
    assert len(result['evidence']['source_sha256'])==64
    with pytest.raises(ValueError,match='budget'):seed_from_file(source,max_bytes=1)
    with pytest.raises(ValueError,match='reviewed'):seed_from_file(source,expected_adrs={2,3})
    record['raw_frame']='00';source.write_text(json.dumps(record)+'\n')
    with pytest.raises(ValueError):seed_from_file(source)


def test_normal_start_no_raw_restore_call():
    import ast
    path=Path(__file__).parents[1]/'app/main.py'
    calls=[node.func.id for node in ast.walk(ast.parse(path.read_text()))
           if isinstance(node,ast.Call) and isinstance(node.func,ast.Name)]
    assert 'restore_latest_identities' not in calls
    assert 'IdentityStore' in calls


def test_actual_startup_checkpoint_and_shared_pipeline_identity(tmp_path):
    from main import create_rs485_reader, restore_rs485_checkpoint
    from rs485_evidence import Rs485EvidencePipeline, Rs485EvidenceWriter
    store=IdentityStore(tmp_path/'identities.json')
    assert store.save({2:identity()})
    writer=Rs485EvidenceWriter(tmp_path/'raw')
    pipeline=Rs485EvidencePipeline(writer,position_history_path=tmp_path/'positions')
    reader=create_rs485_reader({'rs485_sniffer_enabled':True},'unused',pipeline,
        port_resolver=lambda:pytest.fail('no hardware'),wall_clock=lambda:200)
    restore_rs485_checkpoint(reader,IdentityStore(store.path))
    assert pipeline.identity_provider()[2]['timestamp']==100
    assert not pipeline.identity_provider()[2]['identity_currently_confirmed']
    feed(reader,adr=5)
    assert set(pipeline.identity_provider())=={5}
    assert pipeline._latest_identity=={}  # no competing runtime map
    assert set(IdentityStore(store.path).load())=={5}
