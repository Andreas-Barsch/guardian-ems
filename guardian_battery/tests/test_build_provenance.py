import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import version

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40


def tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PACK = tool("prepare_build_context")
SEAL = tool("seal_build_info")


def test_build_round_trip_and_manifest_version(tmp_path):
    manifest = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert manifest["version"] == version.GUARDIAN_VERSION == "0.9.11"
    marker = tmp_path / "marker"
    marker.write_text(REVISION + "\n")
    destination = tmp_path / "build-info.json"
    SEAL.seal_build_info(manifest["version"], marker, destination)
    assert version.require_source_commit(destination) == REVISION
    assert json.loads(destination.read_text()) == {
        "guardian_version": "0.9.11", "source_commit": REVISION,
    }
    with pytest.raises(FileExistsError):
        SEAL.seal_build_info(manifest["version"], marker, destination)


@pytest.mark.parametrize("build_version", ["", "0.8.1", "0.9.0\n"])
def test_build_rejects_mismatching_version(tmp_path, build_version):
    marker = tmp_path / "marker"
    marker.write_text(REVISION)
    with pytest.raises(ValueError, match="BUILD_VERSION"):
        SEAL.seal_build_info(build_version, marker, tmp_path / "result")
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("revision", ["", "3d2e1c2", "A" * 40, "g" * 40, "a" * 39, "a" * 41])
def test_build_rejects_invalid_marker(tmp_path, revision):
    marker = tmp_path / "marker"
    marker.write_text(revision)
    with pytest.raises(ValueError, match="source commit"):
        SEAL.seal_build_info(version.GUARDIAN_VERSION, marker, tmp_path / "result")
    assert not (tmp_path / "result").exists()


def test_build_requires_marker(tmp_path):
    with pytest.raises(FileNotFoundError):
        SEAL.seal_build_info(version.GUARDIAN_VERSION, tmp_path / "missing", tmp_path / "result")


@pytest.mark.parametrize("content", [
    b"{", b"\xff", b"null", b"[]", b"{}",
    json.dumps({"guardian_version": "0.8.1", "source_commit": REVISION}).encode(),
    *[json.dumps({"guardian_version": version.GUARDIAN_VERSION, "source_commit": value}).encode()
      for value in [None, 12, "3d2e1c2", "A" * 40]],
])
def test_runtime_rejects_invalid_or_mismatching_metadata(tmp_path, content):
    path = tmp_path / "build-info.json"
    path.write_bytes(content)
    with pytest.raises(RuntimeError, match="provenance"):
        version.require_source_commit(path)


def test_runtime_requires_build_info(tmp_path):
    with pytest.raises(RuntimeError, match="provenance"):
        version.require_source_commit(tmp_path / "missing")


def fake_repository(tmp_path, monkeypatch, *, dirty=""):
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(root, *args):
        assert root == repository
        if args[0] == "status":
            return dirty
        assert args == ("rev-parse", "HEAD")
        return REVISION
    monkeypatch.setattr(PACK, "_git", git)
    return repository


def test_package_pins_resolved_commit_and_replaces_stale_marker(tmp_path, monkeypatch):
    repository = fake_repository(tmp_path, monkeypatch)
    def archive(command, *, check, stdout):
        # Must archive the resolved SHA, never a HEAD that could move meanwhile.
        assert command == ["git", "-C", str(repository), "archive", REVISION, "guardian_battery"]
        with tarfile.open(fileobj=stdout, mode="w") as output:
            for name, value in {"app/version.py": b"fixture", ".guardian-source-commit": b"old"}.items():
                member = tarfile.TarInfo("guardian_battery/" + name)
                member.size = len(value)
                output.addfile(member, io.BytesIO(value))
    monkeypatch.setattr(PACK.subprocess, "run", archive)
    destination = tmp_path / "context"
    assert PACK.prepare_build_context(repository, destination) == REVISION
    assert (destination / ".guardian-source-commit").read_text() == REVISION + "\n"
    assert (destination / "app/version.py").read_bytes() == b"fixture"
    assert not (destination / ".git").exists()


@pytest.mark.parametrize("dirty", [" M guardian_battery/app/main.py", "M  guardian_battery/config.yaml", "?? new-file"])
def test_package_rejects_dirty_worktree(tmp_path, monkeypatch, dirty):
    repository = fake_repository(tmp_path, monkeypatch, dirty=dirty)
    with pytest.raises(RuntimeError, match="dirty"):
        PACK.prepare_build_context(repository, tmp_path / "context")
    assert not (tmp_path / "context").exists()


def test_package_preserves_existing_destination_and_rejects_repo_destination(tmp_path, monkeypatch):
    repository = fake_repository(tmp_path, monkeypatch)
    destination = tmp_path / "context"
    destination.mkdir()
    (destination / "keep").write_text("untouched")
    with pytest.raises(RuntimeError, match="already exists"):
        PACK.prepare_build_context(repository, destination)
    assert (destination / "keep").read_text() == "untouched"
    with pytest.raises(RuntimeError, match="outside"):
        PACK.prepare_build_context(repository, repository / "context")


def test_startup_rejects_missing_provenance_before_service_or_acquisition(tmp_path, monkeypatch):
    with patch.object(Path, "mkdir", return_value=None):
        import main
    def reject():
        raise RuntimeError("missing provenance")
    monkeypatch.setattr(main, "SHARE_DIR", tmp_path / "not-created")
    monkeypatch.setattr(main, "require_source_commit", reject)
    monkeypatch.setattr(main, "load_options", lambda: pytest.fail("options accessed"))
    monkeypatch.setattr(main, "start_config_server", lambda: pytest.fail("service started"))
    monkeypatch.setattr(main.signal, "signal", lambda *_: pytest.fail("signal handler installed"))
    with pytest.raises(RuntimeError, match="provenance"):
        main.main()
    assert not main.SHARE_DIR.exists()


def test_startup_logs_validated_provenance_before_existing_lifecycle(tmp_path, monkeypatch, caplog):
    with patch.object(Path, "mkdir", return_value=None):
        import main
    class StopBeforeIO(Exception):
        pass
    def stop(*_):
        raise StopBeforeIO()
    monkeypatch.setattr(main, "SHARE_DIR", tmp_path / "synthetic-share")
    monkeypatch.setattr(main, "require_source_commit", lambda: REVISION)
    monkeypatch.setattr(main, "update_display_projection_startup", stop)
    with caplog.at_level("INFO"), pytest.raises(StopBeforeIO):
        main.main()
    assert "Guardian Battery 0.9.11; source_commit=" + REVISION in caplog.text
    assert "diagnostic_engine=0.4.12" in caplog.text


def test_docker_invokes_validated_writer():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'ARG BUILD_VERSION' in dockerfile
    assert 'LABEL org.opencontainers.image.version="${BUILD_VERSION}"' in dockerfile
    assert 'COPY .guardian-source-commit /tmp/guardian-source-commit' in dockerfile
    assert 'PYTHONPATH=/app python3 /tmp/seal_build_info.py "${BUILD_VERSION}" /tmp/guardian-source-commit /app/build-info.json' in dockerfile
    assert 'chmod 0444 /app/build-info.json' in dockerfile


def test_package_real_git_archive_read_only(tmp_path, monkeypatch):
    repository = ROOT.parent
    real_git = PACK._git
    revision = real_git(repository, "rev-parse", "HEAD")
    # Simulate clean status solely to exercise the archive of the committed tree;
    # no working-tree content or Git mutation is used by this integration test.
    monkeypatch.setattr(PACK, "_git", lambda root, *args:
                        "" if args[0] == "status" else real_git(root, *args))
    destination = tmp_path / "context"
    assert PACK.prepare_build_context(repository, destination) == revision
    expected = subprocess.check_output([
        "git", "-C", str(repository), "show", revision + ":guardian_battery/app/version.py",
    ])
    assert (destination / "app/version.py").read_bytes() == expected
    assert (destination / ".guardian-source-commit").read_text() == revision + "\n"


def test_package_failure_leaves_no_partial_destination(tmp_path, monkeypatch):
    repository = fake_repository(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git archive")
    monkeypatch.setattr(PACK.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        PACK.prepare_build_context(repository, tmp_path / "context")
    assert not (tmp_path / "context").exists()
    assert not list(tmp_path.glob("guardian-battery-context-*"))
