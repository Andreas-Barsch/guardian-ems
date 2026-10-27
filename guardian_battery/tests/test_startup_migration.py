"""Explicit add-on control files; isolated copies, no live transport."""
import hashlib,json
import pytest
import startup,version
from test_startup_activation import marker


def request(root,plan):
    path=root/'reviewed-plan.json';path.write_text(json.dumps(plan))
    return dict(contract='guardian-prepared-migration-request-1',
        guardian_version=version.GUARDIAN_VERSION,source_commit='a'*40,
        root=str(root),plan=path.name,plan_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),steps=64)


def invoke(root):
    return startup.main(activation_path=root/'private-marker',
        shared_activation_path=root/'shared-marker',migration_request_path=root/'migration-request')


@pytest.fixture
def sealed(monkeypatch):
    import main,prepared_candidate
    monkeypatch.setattr(version,'require_source_commit',lambda:'a'*40)
    monkeypatch.setattr(main,'main',lambda:pytest.fail('legacy acquisition during offline request'))
    monkeypatch.setattr(prepared_candidate,'main',lambda *a:pytest.fail('prepared acquisition during offline request'))


@pytest.mark.parametrize('fault',['json','extra','version','source','hash','path','root','steps_zero','steps_bool','steps_large','broken_link','oversized','missing_provenance'])
def test_bad_request_fails_before_migration_or_transport(tmp_path,monkeypatch,sealed,fault):
    import prepared_migration
    monkeypatch.setattr(prepared_migration,'PreparedMigration',lambda *a:pytest.fail('migration opened before validation'))
    data=request(tmp_path,{})
    if fault=='extra':data['unexpected']=True
    if fault=='version':data['guardian_version']='0.8.9'
    if fault=='source':data['source_commit']='b'*40
    if fault=='hash':data['plan_sha256']='0'*64
    if fault=='path':data['plan']='../outside.json'
    if fault=='root':data['root']='relative'
    if fault=='steps_zero':data['steps']=0
    if fault=='steps_bool':data['steps']=True
    if fault=='steps_large':data['steps']=startup.MAX_MIGRATION_STEPS+1
    if fault=='missing_provenance':
        def fail():raise RuntimeError('source unavailable')
        monkeypatch.setattr(version,'require_source_commit',fail)
    target=tmp_path/'migration-request'
    if fault=='broken_link':target.symlink_to(tmp_path/'missing')
    else:target.write_text('{' if fault=='json' else ' '*65537 if fault=='oversized' else json.dumps(data))
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises((ValueError,OSError,RuntimeError)):invoke(tmp_path)
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


@pytest.mark.parametrize('names',[['private-marker','shared-marker'],['private-marker','migration-request'],['shared-marker','migration-request']])
def test_ambiguous_controls_preserved_and_never_started(tmp_path,sealed,names):
    for name in names:(tmp_path/name).write_text('{}')
    with pytest.raises(ValueError,match='ambiguous startup controls'):invoke(tmp_path)
    assert all((tmp_path/name).read_text()=='{}' for name in names)


def test_shared_activation_uses_existing_validation_and_dispatch(tmp_path,monkeypatch):
    import prepared_candidate
    data=marker(tmp_path);(tmp_path/'shared-marker').write_text(json.dumps(data));calls=[]
    monkeypatch.setattr(version,'require_source_commit',lambda:'a'*40)
    monkeypatch.setattr(prepared_candidate,'main',lambda args:calls.append(args))
    invoke(tmp_path)
    assert calls==[['--root',str(tmp_path),'--layout',str(tmp_path/data['layout']),'--run']]
    data['layout_sha256']='0'*64;(tmp_path/'shared-marker').write_text(json.dumps(data))
    with pytest.raises(ValueError,match='layout hash mismatch'):invoke(tmp_path)
    assert len(calls)==1


def test_actual_offline_migration_resumes_through_addon_start_and_preserves_originals(tmp_path,sealed):
    from test_prepared_migration import legacy_plan
    root,plan=legacy_plan(tmp_path)
    plan['state_directory']='persistent-control'
    originals={item['path']:(root/item['path']).read_bytes() for item in plan['files']}
    data=request(root,plan);data['steps']=1
    control=root/'migration-request';control.write_text(json.dumps(data))
    assert invoke(root)==0
    assert json.loads((root/'persistent-control/prepared-migration.json').read_text())['phase']=='freeze'
    data['steps']=64;control.write_text(json.dumps(data))
    for _ in range(24):
        assert invoke(root)==0
        if json.loads((root/'persistent-control/prepared-migration.json').read_text())['phase']=='complete':break
    else:pytest.fail('bounded migration did not complete')
    assert (root/'persistent-control/prepared-layout.json').is_file()
    assert not (root/'private-marker').exists() and not (root/'shared-marker').exists()
    assert control.is_file()  # Completed request never silently changes next start mode.
    assert all((root/name).read_bytes()==raw for name,raw in originals.items())
    assert invoke(root)==0  # Completed request remains offline on restart.


def test_container_entrypoint_passes_both_explicit_shared_control_paths():
    from pathlib import Path
    source=(Path(startup.__file__)).read_text()
    assert "main(shared_activation_path=SHARED_ACTIVATION_PATH,migration_request_path=MIGRATION_REQUEST_PATH)" in source


def test_migration_control_directory_cannot_escape_offline_root(tmp_path):
    from test_prepared_migration import legacy_plan
    from prepared_migration import PreparedMigration
    root,plan=legacy_plan(tmp_path);plan['state_directory']='../outside'
    with pytest.raises(ValueError,match='outside explicit root'):PreparedMigration(root,plan)
    assert not (tmp_path/'outside').exists()


def test_explicit_offline_budget_can_cover_multiple_cli_batches(tmp_path,monkeypatch,sealed):
    import prepared_migration
    data=request(tmp_path,{});data['steps']=65
    (tmp_path/'migration-request').write_text(json.dumps(data));calls=[]
    class Migration:
        digest='test-plan'
        def __init__(self,*args):pass
        def begin(self):calls.append('begin')
        def step(self):calls.append('step');return {'phase':'adopt','ordinal':1}
    monkeypatch.setattr(prepared_migration,'PreparedMigration',Migration)
    assert invoke(tmp_path)==0
    assert calls==['begin']+['step']*65
