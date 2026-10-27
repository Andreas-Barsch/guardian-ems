"""Actual container selection, synthetic marker/files only; no real activation."""
import hashlib,json
from pathlib import Path
import pytest
import startup,version


def marker(tmp_path):
    layout=tmp_path/'prepared-layout.json'
    layout.write_text(json.dumps({'contract':'guardian-prepared-candidate-1'}))
    return dict(contract='guardian-prepared-activation-1',guardian_version=version.GUARDIAN_VERSION,
        source_commit='a'*40,root=str(tmp_path),layout=layout.name,
        layout_sha256=hashlib.sha256(layout.read_bytes()).hexdigest())


def test_absent_activation_retains_one_legacy_owner(tmp_path,monkeypatch):
    import main,prepared_candidate
    calls=[]
    monkeypatch.setattr(main,'main',lambda:calls.append('legacy'))
    monkeypatch.setattr(prepared_candidate,'main',lambda *a:pytest.fail('prepared without activation'))
    startup.main(activation_path=tmp_path/'absent')
    assert calls==['legacy'] and not list(tmp_path.iterdir())


@pytest.mark.parametrize('fault',['json','extra','version','commit','hash','path','missing_layout','oversized','broken_symlink','layout_contract','missing_provenance'])
def test_present_invalid_activation_never_falls_back_or_opens_transport(tmp_path,monkeypatch,fault):
    import main,prepared_candidate
    calls=[]
    monkeypatch.setattr(main,'main',lambda:pytest.fail('silent legacy fallback'))
    monkeypatch.setattr(prepared_candidate,'main',lambda *a:pytest.fail('candidate started before validation'))
    monkeypatch.setattr(version,'require_source_commit',lambda:'a'*40)
    data=marker(tmp_path);path=tmp_path/'activation.json'
    if fault=='extra':data['extra']=True
    if fault=='version':data['guardian_version']='0.8.9'
    if fault=='commit':data['source_commit']='b'*40
    if fault=='hash':data['layout_sha256']='0'*64
    if fault=='path':data['layout']='../outside.json'
    if fault=='missing_layout':data['layout']='absent.json'
    if fault=='layout_contract':
        layout=tmp_path/data['layout'];layout.write_text('{}');data['layout_sha256']=hashlib.sha256(layout.read_bytes()).hexdigest()
    if fault=='missing_provenance':
        def fail():raise RuntimeError('sealed provenance missing')
        monkeypatch.setattr(version,'require_source_commit',fail)
    if fault=='broken_symlink':path.symlink_to(tmp_path/'missing-target')
    else:path.write_text('{' if fault=='json' else ' '*65537 if fault=='oversized' else json.dumps(data))
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises((ValueError,OSError,RuntimeError)):startup.main(activation_path=path)
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}==before


def test_valid_marker_selects_prepared_once_and_removal_selects_legacy(tmp_path,monkeypatch):
    import main,prepared_candidate
    calls=[];data=marker(tmp_path);path=tmp_path/'activation.json';path.write_text(json.dumps(data))
    monkeypatch.setattr(version,'require_source_commit',lambda:'a'*40)
    monkeypatch.setattr(main,'main',lambda:calls.append('legacy'))
    monkeypatch.setattr(prepared_candidate,'main',lambda args:calls.append(args))
    startup.main(activation_path=path)
    assert calls==[['--root',str(tmp_path),'--layout',str(tmp_path/data['layout']),'--run']]
    path.unlink()  # Explicit synthetic operator action, never done by startup.
    startup.main(activation_path=path)
    assert calls[-1]=='legacy' and len(calls)==2


def test_actual_shell_entrypoint_selects_startup_module():
    root=Path(__file__).resolve().parents[1]
    assert (root/'run.sh').read_text().splitlines()[-1]=='exec python3 /app/startup.py'
    assert 'COPY app /app' in (root/'Dockerfile').read_text()
