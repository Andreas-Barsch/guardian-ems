"""Synthetic temp files only. Process exits test recovery, not device power loss."""
from pathlib import Path
import os
import subprocess
import sys
import sqlite3
import pytest
from raw_registry import RawRegistry, RegistryConflict, RegistryCorrupt, UnconfirmedTail, MAX_BLOCK, ZERO, encoded, hashed
from raw_registry_catalog import RegistryCatalogConsumer
from source_catalog import SourceCatalog


def token(r):return hashed(encoded(r))


def setup(tmp_path):
    reg=RawRegistry.create(tmp_path/'registry',segment_records=2)
    raw=tmp_path/'raw.jsonl'
    r=reg.register('s','r1',raw,writer_epoch='w1',namespace='legacy-local',operation='register')
    return reg,raw,r


def append(reg,r,data=b'{"timestamp":1,"module":1}\n',operation='append'):
    return reg.append('s',data,expected_receipt=token(r),operation=operation)


def consumer(tmp_path,reg):
    cat=SourceCatalog(tmp_path/'catalog.db');c=RegistryCatalogConsumer(reg,cat);c.initialize()
    return cat,c


def child(code,*args):
    result=subprocess.run([sys.executable,'-c',code,*map(str,args)],capture_output=True,text=True)
    assert result.returncode==77,(result.stdout,result.stderr,result.returncode)


def test_registration_precedes_first_raw_line_and_exact_receipts(tmp_path):
    reg,raw,r=setup(tmp_path)
    assert raw.read_bytes()==b'' and r['seq']==1 and r['end']==0
    a=append(reg,r,b'a\nb\n')
    assert a['start']==0 and a['end']==4 and a['lines']==2
    assert a['file_id']==r['file_id'] and a['writer_epoch']=='w1'
    assert a['source_previous']==token(r) and a['block_previous']==r['block_chain']
    page=RawRegistry(reg.root).read_page()
    assert page['records']==[r,a] and page['caught_up']
    assert append(reg,r,b'a\nb\n')==a and raw.read_bytes()==b'a\nb\n'
    with pytest.raises(RegistryConflict):append(reg,r,b'x\n')
    with pytest.raises(RegistryConflict):reg.register('s','other',raw,writer_epoch='w1',namespace='legacy-local',operation='append')


@pytest.mark.parametrize('payload',[b'',b'partial',b'x'*(MAX_BLOCK+1)+b'\n'])
def test_reject_incomplete_or_unbounded_batches(tmp_path,payload):
    reg,raw,r=setup(tmp_path)
    with pytest.raises(ValueError):append(reg,r,payload)
    assert raw.read_bytes()==b'' and reg.state('s')==r


def test_flush_order_and_failed_fsync_never_acknowledged(tmp_path,monkeypatch):
    reg,raw,r=setup(tmp_path);trace=[];reg.fault=trace.append
    append(reg,r)
    ordered=['intent:file_flush','intent:file_fsync','intent:dir_fsync','after_intent',
             'after_raw_write','after_raw_flush','after_raw_fsync','after_raw_dir_fsync',
             'receipt:file_fsync','receipt:dir_fsync','after_receipt','head:file_fsync',
             'head:dir_fsync','after_head','after_checkpoint','after_pending_clear']
    assert [trace.index(x) for x in ordered]==sorted(trace.index(x) for x in ordered)
    old=reg.state('s')
    def fail(_):raise OSError('synthetic fsync failure')
    monkeypatch.setattr(os,'fsync',fail)
    with pytest.raises(OSError):append(reg,old,operation='next')
    assert reg.state('s')==old


@pytest.mark.parametrize('stage',['intent:file_flush','intent:file_fsync','intent:dir_fsync','after_intent',
 'after_raw_write','after_raw_flush','after_raw_fsync','after_raw_dir_fsync','receipt:file_flush',
 'receipt:file_fsync','receipt:dir_fsync','after_receipt','head:file_flush','head:file_fsync',
 'head:dir_fsync','after_head','checkpoint:file_flush','checkpoint:file_fsync','after_checkpoint','after_pending_clear'])
def test_process_exit_at_writer_boundaries(tmp_path,stage):
    reg,raw,r=setup(tmp_path)
    child('''import os,sys
from raw_registry import RawRegistry,encoded,hashed
r=RawRegistry(sys.argv[1],fault=lambda s:os._exit(77) if s==sys.argv[2] else None)
r.append('s',b'first\\n',expected_receipt=hashed(encoded(r.state('s'))),operation='first')
''',reg.root,stage)
    recovered=RawRegistry(reg.root);recovered.recover()
    a=recovered.append('s',b'first\n',expected_receipt=token(r),operation='first')
    assert raw.read_bytes()==b'first\n' and a['seq']==2 and a['end']==6
    assert len(recovered.read_page()['records'])==2


@pytest.mark.parametrize('matching',[True,False])
def test_pending_partial_write_never_truncates(tmp_path,matching):
    reg,raw,r=setup(tmp_path)
    class Stop(Exception):pass
    reg.fault=lambda stage:(_ for _ in ()).throw(Stop()) if stage=='after_intent' else None
    with pytest.raises(Stop):append(reg,r,b'abcdef\n')
    prefix=b'abc' if matching else b'XXX';raw.write_bytes(prefix)
    fresh=RawRegistry(reg.root)
    if matching:
        fresh.recover();assert raw.read_bytes()==b'abcdef\n'
        assert fresh.state('s')['end']==7
    else:
        with pytest.raises(UnconfirmedTail):fresh.recover()
        assert raw.read_bytes()==prefix and fresh.state('s')==r


def test_explicit_tail_only_complete_bounded_prefix(tmp_path):
    reg,raw,r=setup(tmp_path);raw.write_bytes(b'a\nb\npartial')
    assert reg.inspect_tail('s',max_bytes=3)['payload']==b'a\n'
    with pytest.raises(RegistryConflict):append(reg,r)
    a=reg.adopt_tail('s',expected_receipt=token(r),operation='adopt1',max_bytes=3)
    assert a['end']==2
    b=reg.adopt_tail('s',expected_receipt=token(a),operation='adopt2')
    assert b['end']==4 and raw.read_bytes()==b'a\nb\npartial'
    with pytest.raises(UnconfirmedTail):reg.adopt_tail('s',expected_receipt=token(b),operation='adopt3')
    assert reg.state('s')==b


@pytest.mark.parametrize('target',['head.json','format.json','receipt'])
@pytest.mark.parametrize('damage',['missing','corrupt'])
def test_missing_or_corrupt_metadata_fails_without_reset(tmp_path,target,damage):
    reg,raw,r=setup(tmp_path);a=append(reg,r)
    p=reg._receipt_path(a['seq']) if target=='receipt' else reg.root/target
    if damage=='missing':p.unlink()
    else:p.write_bytes(b'broken')
    before=raw.read_bytes()
    with pytest.raises(RegistryCorrupt):RawRegistry(reg.root).read_page()
    assert raw.read_bytes()==before


def test_rewrite_truncation_and_rotation_require_explicit_identity(tmp_path):
    reg,raw,r=setup(tmp_path);a=append(reg,r,b'old\n')
    raw.write_bytes(b'new\n')
    with pytest.raises(RegistryConflict):append(reg,a,operation='fail')
    b=reg.register('s','r2',raw,writer_epoch='w2',namespace='legacy-local',operation='rewrite',expected_receipt=token(a),allow_existing=True)
    assert b['end']==0 and b['previous_revision']=='r1'
    c=reg.adopt_tail('s',expected_receipt=token(b),operation='newdata')
    raw.write_bytes(b'')
    with pytest.raises(RegistryConflict):append(reg,c,operation='fail2')
    d=reg.register('s','r3',raw,writer_epoch='w3',namespace='legacy-local',operation='truncate',expected_receipt=token(c))
    raw.rename(tmp_path/'rotated.jsonl');raw.write_bytes(b'')
    with pytest.raises(RegistryConflict):reg.register('s','r4',raw,writer_epoch='w4',namespace='legacy-local',operation='rotate',expected_receipt=token(d))
    e=reg.register('newsource','r1',raw,writer_epoch='w4',namespace='legacy-local',operation='rotation')
    assert e['file_id']!=d['file_id'] and len(reg.read_page()['records'])==6


def test_catalog_lock_does_not_block_raw_registration_or_append(tmp_path):
    reg,raw,r=setup(tmp_path);cat,c=consumer(tmp_path,reg)
    with cat.write():
        a=append(reg,r)
        other=reg.register('other','r1',tmp_path/'other.jsonl',writer_epoch='w1',namespace='legacy-local',operation='other')
        assert a['end']>0 and other['end']==0
    assert c.cursor()==(1,ZERO)
    result=c.step(expected_cursor=c.cursor())
    assert result['records']==3 and result['caught_up']


@pytest.mark.parametrize('stage',['after_catalog_records','after_catalog_cursor','after_catalog_commit'])
def test_consumer_source_receipt_cursor_atomic_on_process_exit(tmp_path,stage):
    reg,raw,r=setup(tmp_path);a=append(reg,r,b'1\n');cat,c=consumer(tmp_path,reg)
    child('''import os,sys
from raw_registry import RawRegistry
from source_catalog import SourceCatalog
from raw_registry_catalog import RegistryCatalogConsumer
c=RegistryCatalogConsumer(RawRegistry(sys.argv[1]),SourceCatalog(sys.argv[2]))
c.step(expected_cursor=c.cursor(),fault=lambda s:os._exit(77) if s==sys.argv[3] else None)
''',reg.root,cat.path,stage)
    with cat.connection() as db:
        count=db.execute('SELECT count(*) FROM receipt').fetchone()[0]
        proofs=db.execute('SELECT count(*) FROM registry_proof').fetchone()[0]
    assert count==proofs==(2 if stage=='after_catalog_commit' else 0)
    assert c.cursor()[0]==(3 if stage=='after_catalog_commit' else 1)
    c.step(expected_cursor=c.cursor())
    assert c.cursor()==(3,token(a))
    b=append(reg,a,b'2\n',operation='second');result=c.step(expected_cursor=c.cursor())
    assert result['records']==1 and result['raw_bytes']==2
    with cat.connection() as db:
        assert db.execute('SELECT frontier FROM source').fetchone()[0]==4
        assert db.execute('SELECT count(*) FROM receipt').fetchone()[0]==3


def test_consumer_cas_budget_corruption_and_no_partial_transaction(tmp_path):
    reg,raw,r=setup(tmp_path);a=append(reg,r,b'abc\n');b=append(reg,a,b'def\n',operation='b');cat,c=consumer(tmp_path,reg)
    initial=c.cursor();first=c.step(expected_cursor=initial,max_raw_bytes=4)
    assert first['records']==2 and not first['caught_up']
    with pytest.raises(RegistryConflict):c.step(expected_cursor=initial)
    blocked=c.step(expected_cursor=c.cursor(),max_raw_bytes=3)
    assert blocked['budget_blocked'] and blocked['records']==0
    raw.write_bytes(b'abc\nBAD\n');before=c.cursor()
    with pytest.raises(RegistryConflict):c.step(expected_cursor=before)
    assert c.cursor()==before
    with cat.connection() as db:assert db.execute('SELECT count(*) FROM receipt').fetchone()[0]==2


def test_consumer_caught_up_can_register_rewrite_but_cold_replay_cannot_invent_old_bytes(tmp_path):
    reg,raw,r=setup(tmp_path);a=append(reg,r,b'old\n');cat,c=consumer(tmp_path,reg);c.step(expected_cursor=c.cursor())
    raw.write_bytes(b'new\n')
    b=reg.register('s','r2',raw,writer_epoch='w2',namespace='legacy-local',operation='rewrite',expected_receipt=token(a),allow_existing=True)
    reg.adopt_tail('s',expected_receipt=token(b),operation='new')
    c.step(expected_cursor=c.cursor())
    with cat.connection() as db:assert db.execute('SELECT count(*) FROM source').fetchone()[0]==2
    cold=RegistryCatalogConsumer(reg,SourceCatalog(tmp_path/'cold.db'));cold.initialize()
    with pytest.raises(RegistryConflict):cold.step(expected_cursor=cold.cursor())
    assert cold.cursor()==(1,ZERO)


@pytest.mark.parametrize('history',[1,30,365,730])
def test_bounded_restart_and_tail_consume_independent_of_old_receipts(tmp_path,history,monkeypatch):
    reg,raw,r=setup(tmp_path)
    for i in range(history):r=append(reg,r,b'x\n',operation=str(i))
    cat,c=consumer(tmp_path,reg)
    while not c.step(expected_cursor=c.cursor())['caught_up']:pass
    r=append(reg,r,b'last\n',operation='last')
    def forbidden(*args,**kwargs):raise AssertionError('no discovery scan permitted')
    monkeypatch.setattr(os,'scandir',forbidden)
    fresh=RawRegistry(reg.root);fresh.recover();fresh.reset_metrics()
    resumed=RegistryCatalogConsumer(fresh,cat)
    result=resumed.step(expected_cursor=resumed.cursor())
    assert result['records']==1 and result['raw_bytes']==5
    assert fresh.metrics['metadata_reads']<=3 and fresh.metrics['raw_bytes_read']==5
    assert result['caught_up'] and result['metadata_bytes']<3000
    print({'history_receipts':history,'metrics':fresh.metrics})


def test_journal_cursor_and_chain_reject_invalid_claims(tmp_path):
    reg,raw,r=setup(tmp_path)
    with pytest.raises(RegistryConflict):reg.read_page(next_seq=3)
    with pytest.raises(RegistryConflict):reg.read_page(next_seq=2,previous_hash=ZERO)
    a=append(reg,r)
    with pytest.raises(RegistryCorrupt):reg.read_page(next_seq=2,previous_hash=ZERO)


def test_missing_non_tip_checkpoint_is_not_a_new_source(tmp_path):
    reg,raw,r=setup(tmp_path)
    reg.register('other','r1',tmp_path/'other.jsonl',writer_epoch='w1',namespace='legacy-local',operation='other')
    reg._state_path('s').unlink()
    with pytest.raises(RegistryCorrupt,match='checkpoint missing'):reg.state('s')
    with pytest.raises(RegistryCorrupt):reg.register('s','r1',raw,writer_epoch='w1',namespace='legacy-local',operation='again')
    assert raw.read_bytes()==b''


def test_missing_latest_checkpoint_repaired_from_head_only(tmp_path):
    reg,raw,r=setup(tmp_path);a=append(reg,r)
    reg._state_path('s').unlink()
    fresh=RawRegistry(reg.root);fresh.recover()
    assert fresh.state('s')==a
    assert fresh.metrics['raw_bytes_read']==0 and fresh.metrics['metadata_reads']<10


def test_corrupt_pending_intent_preserves_confirmed_state_and_bytes(tmp_path):
    reg,raw,r=setup(tmp_path)
    (reg.root/'intent.json').write_bytes(b'bad intent')
    with pytest.raises(RegistryCorrupt):reg.recover()
    assert raw.read_bytes()==b'' and reg.state('s')==r


def test_hash_chain_detects_checksumming_a_changed_receipt(tmp_path):
    reg,raw,r=setup(tmp_path);a=append(reg,r)
    changed=dict(r,writer_epoch='tampered')
    reg._atomic(reg._receipt_path(1),changed,'synthetic')
    with pytest.raises(RegistryCorrupt,match='chain mismatch'):reg.read_page()


def test_two_writers_fail_fast_on_registry_lock(tmp_path):
    reg,raw,r=setup(tmp_path)
    with reg._writer():
        with pytest.raises(RegistryConflict,match='busy'):append(RawRegistry(reg.root),r)
    assert raw.read_bytes()==b''


def test_consumer_rolls_back_prior_records_if_second_source_conflicts(tmp_path):
    reg,raw,r=setup(tmp_path);cat,c=consumer(tmp_path,reg)
    reg.register('other','r1',tmp_path/'other.jsonl',writer_epoch='w1',namespace='legacy-local',operation='other')
    cat.register(source='other',revision='foreign',path=str(tmp_path/'other.jsonl'),namespace='legacy-local',writer_epoch='foreign',receipt='foreign',frontier=0)
    from projection_repository import Conflict
    with pytest.raises(Conflict):c.step(expected_cursor=c.cursor())
    assert c.cursor()==(1,ZERO)
    with cat.connection() as db:
        assert db.execute("SELECT count(*) FROM source WHERE source='s'").fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM registry_proof').fetchone()[0]==0


@pytest.mark.parametrize('stage',['after_intent','after_raw_fsync','after_receipt','after_head','source_marker:file_fsync','after_checkpoint'])
def test_registration_process_crash_recovers_before_append(tmp_path,stage):
    reg=RawRegistry.create(tmp_path/'registry');raw=tmp_path/'raw.jsonl'
    child('''import os,sys
from raw_registry import RawRegistry
r=RawRegistry(sys.argv[1],fault=lambda s:os._exit(77) if s==sys.argv[3] else None)
r.register('s','r1',sys.argv[2],writer_epoch='w1',namespace='legacy-local',operation='register')
''',reg.root,raw,stage)
    fresh=RawRegistry(reg.root);fresh.recover()
    r=fresh.register('s','r1',raw,writer_epoch='w1',namespace='legacy-local',operation='register')
    assert r['seq']==1 and r['end']==0 and raw.read_bytes()==b''
    a=append(fresh,r,b'line\n')
    assert a['seq']==2 and raw.read_bytes()==b'line\n'


def test_raw_fsync_failure_keeps_unacknowledged_intent_for_recovery(tmp_path,monkeypatch):
    reg,raw,r=setup(tmp_path);original=os.fsync;ino=raw.stat().st_ino
    def fail_raw(fd):
        if os.fstat(fd).st_ino==ino:raise OSError('synthetic Raw fsync error')
        original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os,'fsync',fail_raw)
        with pytest.raises(OSError,match='Raw fsync'):append(reg,r,b'line\n')
    assert reg.state('s')==r and (reg.root/'intent.json').exists()
    reg.recover()
    assert reg.state('s')['end']==5 and raw.read_bytes()==b'line\n'


def test_adopt_operation_cannot_be_reused_as_append(tmp_path):
    reg,raw,r=setup(tmp_path);raw.write_bytes(b'line\n')
    a=reg.adopt_tail('s',expected_receipt=token(r),operation='adopt')
    with pytest.raises(RegistryConflict):append(reg,a,b'line\n',operation='adopt')
    assert raw.read_bytes()==b'line\n'
