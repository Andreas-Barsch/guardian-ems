import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "guardian_battery/tools/prepare_build_context.py"
SPEC = importlib.util.spec_from_file_location("prepare_build_context", TOOL_PATH)
TOOL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TOOL)


def git(repository, *args):
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def repository(tmp_path):
    root = tmp_path / "repository"
    (root / "guardian_battery/app").mkdir(parents=True)
    (root / "guardian_battery/app/main.py").write_text("print('guardian')\n")
    (root / "guardian_battery/Dockerfile").write_text("FROM scratch\n")
    git(root, "init")
    git(root, "config", "user.email", "guardian@example.invalid")
    git(root, "config", "user.name", "Guardian Test")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return root


def test_packaging_uses_exact_clean_head_and_does_not_copy_git(tmp_path):
    root = repository(tmp_path)
    expected = git(root, "rev-parse", "HEAD")
    destination = tmp_path / "build-context"
    assert TOOL.prepare_build_context(root, destination) == expected
    assert (destination / ".guardian-source-commit").read_text() == expected + "\n"
    assert (destination / "app/main.py").is_file()
    assert not (destination / ".git").exists()


def test_packaging_rejects_dirty_tree_and_existing_destination(tmp_path):
    root = repository(tmp_path)
    (root / "guardian_battery/app/main.py").write_text("changed\n")
    with pytest.raises(RuntimeError, match="dirty working tree"):
        TOOL.prepare_build_context(root, tmp_path / "dirty")
    git(root, "restore", ".")
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(RuntimeError, match="destination already exists"):
        TOOL.prepare_build_context(root, destination)


def test_docker_contract_seals_packaged_revision_and_version():
    dockerfile = (ROOT / "guardian_battery/Dockerfile").read_text()
    version = (ROOT / "guardian_battery/app/version.py").read_text()
    assert "COPY .guardian-source-commit" in dockerfile
    assert 'ARG BUILD_VERSION' in dockerfile
    assert 'build-info.json' in dockerfile
    assert 'payload.get("guardian_version") != GUARDIAN_VERSION' in version
    assert "43c04ab0b67fec4bcf2e4bcdb34b31767a90b620" not in version
