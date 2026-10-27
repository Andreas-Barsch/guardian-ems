"""Explicit synthetic replacement, never a productive migration."""
import subprocess,sys
import pytest
from pathlib import Path
from raw_registry import RawRegistry,encoded,hashed,RegistryConflict
from test_raw_registry import setup,append,token


@pytest.mark.parametrize('stage',['after_intent','before_replacement_rename','after_replacement_rename',
    'after_replacement_dir_fsync','after_receipt','after_head','after_checkpoint','after_pending_clear'])
def test_replacement_process_exit_recovery_and_exact_new_prefix(tmp_path,stage):
    registry,path,first=setup(tmp_path);old=append(registry,first,b'keep\ndelete\n')
    replacement=tmp_path/'prepared';replacement.write_bytes(b'keep\n')
    code='''import os,sys
from raw_registry import RawRegistry,encoded,hashed
r=RawRegistry(sys.argv[1],fault=lambda value:os._exit(77) if value==sys.argv[3] else None)
r.replace_file('s','deleted',sys.argv[2],expected_receipt=hashed(encoded(r.state('s'))),operation='delete')
'''
    result=subprocess.run([sys.executable,'-c',code,str(registry.root),str(replacement),stage],capture_output=True,text=True)
    assert result.returncode==77,result.stderr
    fresh=RawRegistry(registry.root);fresh.recover()
    registered=fresh.replace_file('s','deleted',replacement,expected_receipt=token(old),operation='delete')
    assert registered['seq']==old['seq']+1 and registered['mode']=='replace'
    assert registered['end']==0 and registered['previous_revision']==old['revision']
    assert path.read_bytes()==b'keep\n' and not replacement.exists()
    confirmed=fresh.adopt_tail('s',expected_receipt=token(registered),operation='adopt-new')
    assert confirmed['end']==5
    assert fresh.append('s',b'new\n',expected_receipt=token(confirmed),operation='new')['end']==9
    assert path.read_bytes()==b'keep\nnew\n'


def test_foreign_replacement_change_is_never_overwritten(tmp_path):
    registry,path,first=setup(tmp_path);old=append(registry,first,b'original\n')
    replacement=tmp_path/'prepared';replacement.write_bytes(b'replacement\n')
    class Interrupted(Exception):pass
    registry.fault=lambda value:(_ for _ in ()).throw(Interrupted()) if value=='after_intent' else None
    with pytest.raises(Interrupted):registry.replace_file('s','new',replacement,expected_receipt=token(old),operation='replace')
    replacement.write_bytes(b'foreign\n')
    with pytest.raises(RegistryConflict,match='replacement changed'):RawRegistry(registry.root).recover()
    assert path.read_bytes()==b'original\n' and replacement.read_bytes()==b'foreign\n'


def test_normal_register_still_rejects_unannounced_rotation(tmp_path):
    registry,path,first=setup(tmp_path);old=append(registry,first,b'original\n')
    other=tmp_path/'foreign';other.write_bytes(b'other\n');other.replace(path)
    with pytest.raises(RegistryConflict,match='rotation'):
        registry.register('s','new',path,writer_epoch='w1',namespace='legacy-local',operation='register-other',expected_receipt=token(old),allow_existing=True)


def test_append_cannot_hide_same_stat_foreign_rewrite(tmp_path):
    import os
    registry,path,first=setup(tmp_path);old=append(registry,first,b'original\n')
    before=path.stat();path.write_bytes(b'modified\n');os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(RegistryConflict,match='source changed'):
        registry.append('s',b'new\n',expected_receipt=token(old),operation='new')


@pytest.mark.parametrize('stage',['after_intent','after_replacement_rename','after_head'])
def test_recovery_rejects_same_size_restored_mtime_change(tmp_path,stage):
    import os
    registry,path,first=setup(tmp_path);old=append(registry,first,b'original\n')
    replacement=tmp_path/'prepared';replacement.write_bytes(b'keep\n')
    class Interrupted(Exception):pass
    registry.fault=lambda value:(_ for _ in ()).throw(Interrupted()) if value==stage else None
    with pytest.raises(Interrupted):registry.replace_file('s','new',replacement,expected_receipt=token(old),operation='replace')
    target=replacement if replacement.exists() else path
    stat=target.stat();target.write_bytes(b'fake\n');os.utime(target,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert target.stat().st_size==stat.st_size and target.stat().st_mtime_ns==stat.st_mtime_ns
    with pytest.raises(RegistryConflict,match='replacement.*changed'):RawRegistry(registry.root).recover()
    assert target.read_bytes()==b'fake\n'
    assert (registry.root/'intent.json').exists()


def test_recovery_hash_is_outside_shared_writer_lock_and_not_live_writer_work(tmp_path,monkeypatch):
    import fcntl
    from application_writer import ApplicationWriter
    registry,path,first=setup(tmp_path);old=append(registry,first,b'original\n')
    replacement=tmp_path/'prepared';replacement.write_bytes(b'keep\n'*20000)
    class Interrupted(Exception):pass
    registry.fault=lambda value:(_ for _ in ()).throw(Interrupted()) if value=='after_replacement_rename' else None
    with pytest.raises(Interrupted):registry.replace_file('s','new',replacement,expected_receipt=token(old),operation='replace')
    fresh=RawRegistry(registry.root);original=fresh._replacement_hash;calls=[]
    def checked(path):
        with (fresh.root/'writer.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            calls.append(path);return original(path)
    monkeypatch.setattr(fresh,'_replacement_hash',checked)
    writer=ApplicationWriter(fresh,tmp_path,stream='live',writer_epoch='w1')
    with pytest.raises(RegistryConflict,match='explicit maintenance replacement recovery'):
        writer.append(tmp_path/'live.jsonl',b'{}\n')
    assert not calls and not (tmp_path/'live.jsonl').exists()
    fresh.recover();assert len(calls)==1
    assert path.read_bytes()==b'keep\n'*20000
