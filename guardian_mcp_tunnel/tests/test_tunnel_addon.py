import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
STARTUP = ROOT / "app" / "startup.sh"
DEFAULT_MCP_URL = "http://3195b09a-guardian-research-mcp:8098/mcp"
TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"
CONTROL_SECRET = "control-secret-must-not-leak"
MCP_SECRET = "mcp-secret-must-not-leak"


def manifest():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def fake_tunnel_client(tmp_path: Path) -> Path:
    binary = tmp_path / "tunnel-client"
    binary.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CALLS\"\n"
        "[ -z \"${GUARDIAN_CONTROL_PLANE_API_KEY+x}\" ]\n"
        "[ -z \"${GUARDIAN_MCP_AUTH_TOKEN+x}\" ]\n"
        "case \"$1\" in\n"
        "  --version) printf '%s\\n' 'tunnel-client version 0.0.14' ;;\n"
        "  doctor)\n"
        "    case \"${FAKE_DOCTOR_RESULT:-pass}\" in\n"
        "      pass) printf '%s\\n' '{\"result\":\"ok\",\"checks\":[]}' ;;\n"
        "      oauth) printf '%s\\n' '{\"result\":\"fail\",\"failed_checks\":[\"oauth_metadata\"],\"checks\":[{\"id\":\"oauth_metadata\",\"status\":\"FAIL\",\"summary\":\"oauth discovery invalid metadata: protected resource metadata missing resource\"}]}' ; exit 2 ;;\n"
        "      other) printf '%s\\n' '{\"result\":\"fail\",\"failed_checks\":[\"mcp_server_reachable\"],\"checks\":[{\"id\":\"mcp_server_reachable\",\"status\":\"FAIL\",\"summary\":\"connection refused\"}]}' ; exit 2 ;;\n"
        "      mixed) printf '%s\\n' '{\"result\":\"fail\",\"failed_checks\":[\"oauth_metadata\",\"mcp_server_reachable\"],\"checks\":[{\"id\":\"oauth_metadata\",\"status\":\"FAIL\",\"summary\":\"protected resource metadata missing resource\"},{\"id\":\"mcp_server_reachable\",\"status\":\"FAIL\",\"summary\":\"connection refused\"}]}' ; exit 2 ;;\n"
        "      oauth_other) printf '%s\\n' '{\"result\":\"fail\",\"failed_checks\":[\"oauth_metadata\"],\"checks\":[{\"id\":\"oauth_metadata\",\"status\":\"FAIL\",\"summary\":\"authorization server metadata unavailable\"}]}' ; exit 2 ;;\n"
        "      malformed) printf '%s\\n' 'not-json' ; exit 2 ;;\n"
        "      *) exit 7 ;;\n"
        "    esac ;;\n"
        "  run)\n"
        "    [ \"$CONTROL_PLANE_TUNNEL_ID\" = \"$GUARDIAN_TUNNEL_ID\" ]\n"
        "    [ \"$MCP_SERVER_URL\" = \"$GUARDIAN_MCP_SERVER_URL\" ]\n"
        "    case \"$MCP_EXTRA_HEADERS\" in 'Authorization: file:'*) ;; *) exit 8 ;; esac\n"
        "    [ \"$MCP_MAX_CONCURRENT_REQUESTS\" = 2 ]\n"
        "    [ \"$MCP_STARTUP_WAIT_TIMEOUT\" = 30s ] ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def environment(tmp_path: Path, **changes):
    values = {
        "GUARDIAN_TUNNEL_ID": TUNNEL_ID,
        "GUARDIAN_CONTROL_PLANE_API_KEY": CONTROL_SECRET,
        "GUARDIAN_MCP_AUTH_TOKEN": MCP_SECRET,
        "GUARDIAN_MCP_SERVER_URL": DEFAULT_MCP_URL,
        "GUARDIAN_RUN_DOCTOR": "true",
        "GUARDIAN_TUNNEL_LOG_LEVEL": "info",
        "TUNNEL_CLIENT_BIN": str(fake_tunnel_client(tmp_path)),
        "FAKE_CALLS": str(tmp_path / "calls.txt"),
        "GUARDIAN_SECRET_DIR": str(tmp_path / "secrets"),
    }
    values.update(changes)
    return {**os.environ, **values}


def run_startup(tmp_path: Path, **changes):
    return subprocess.run(
        [str(STARTUP)],
        env=environment(tmp_path, **changes),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_manifest_is_private_aarch64_addon_with_secret_schema():
    value = manifest()
    assert value["version"] == "0.8.0"
    assert value["slug"] == "guardian_mcp_tunnel"
    assert value["arch"] == ["aarch64"]
    assert value["ingress"] is False
    assert value["hassio_api"] is False
    assert value["boot"] == "auto"
    assert "ports" not in value and "map" not in value
    assert value["schema"]["control_plane_api_key"] == "password"
    assert value["schema"]["mcp_auth_token"] == "password"
    assert value["options"]["control_plane_api_key"] == ""
    assert value["options"]["mcp_auth_token"] == ""


def test_manifest_has_exact_private_mcp_default_and_no_default_secret():
    value = manifest()
    assert value["options"]["mcp_server_url"] == DEFAULT_MCP_URL
    assert value["options"]["tunnel_id"] == ""
    assert CONTROL_SECRET not in (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert MCP_SECRET not in (ROOT / "config.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"GUARDIAN_TUNNEL_ID": ""}, "tunnel_id"),
        ({"GUARDIAN_TUNNEL_ID": "tunnel_NOT-VALID"}, "tunnel_id"),
        ({"GUARDIAN_CONTROL_PLANE_API_KEY": ""}, "control_plane_api_key"),
        ({"GUARDIAN_MCP_AUTH_TOKEN": ""}, "mcp_auth_token"),
        ({"GUARDIAN_MCP_SERVER_URL": "ftp://example/mcp"}, "mcp_server_url"),
        ({"GUARDIAN_MCP_SERVER_URL": "http://user:pass@example/mcp"}, "mcp_server_url"),
        ({"GUARDIAN_MCP_SERVER_URL": "http://example/mcp?token=x"}, "mcp_server_url"),
        ({"GUARDIAN_RUN_DOCTOR": "sometimes"}, "run_doctor"),
        ({"GUARDIAN_TUNNEL_LOG_LEVEL": "trace"}, "log_level"),
    ],
)
def test_invalid_or_empty_configuration_fails_redacted(tmp_path, changes, field):
    result = run_startup(tmp_path, **changes)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert field in combined
    assert CONTROL_SECRET not in combined
    assert MCP_SECRET not in combined


def test_startup_uses_file_backed_local_bearer_and_secret_free_arguments(tmp_path):
    result = run_startup(tmp_path)
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    combined = result.stdout + result.stderr + calls
    assert result.returncode == 0
    lines = calls.splitlines()
    assert lines[0] == "--version"
    assert lines[1].startswith("doctor --json --control-plane.api-key=file:")
    assert lines[2].startswith("run --control-plane.api-key=file:")
    assert lines[2].endswith(" --log.level=info --log.format=json")
    assert "configuration validated; secrets redacted" in result.stdout
    assert CONTROL_SECRET not in combined
    assert MCP_SECRET not in combined
    assert DEFAULT_MCP_URL not in combined


def test_doctor_failure_for_unreachable_mcp_is_fatal_and_redacted(tmp_path):
    result = run_startup(tmp_path, FAKE_DOCTOR_RESULT="other")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    combined = result.stdout + result.stderr + calls
    assert result.returncode != 0
    lines = calls.splitlines()
    assert lines[0] == "--version"
    assert lines[1].startswith("doctor --json --control-plane.api-key=file:")
    assert "preflight failed (doctor)" in result.stderr
    assert " run " not in calls
    assert CONTROL_SECRET not in combined and MCP_SECRET not in combined


def test_known_oauth_metadata_failure_is_nonfatal_for_static_bearer(tmp_path):
    result = run_startup(tmp_path, FAKE_DOCTOR_RESULT="oauth")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "doctor OAuth metadata failure accepted" in result.stdout
    assert "protected resource metadata missing resource" in result.stdout
    assert calls.splitlines()[2].startswith("run --control-plane.api-key=file:")


@pytest.mark.parametrize("doctor_result", ["mixed", "oauth_other", "malformed"])
def test_doctor_exception_does_not_hide_other_or_unknown_failures(tmp_path, doctor_result):
    result = run_startup(tmp_path, FAKE_DOCTOR_RESULT=doctor_result)
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    assert result.returncode != 0
    assert "preflight failed (doctor)" in result.stderr
    assert not any(line.startswith("run ") for line in calls.splitlines())


def test_doctor_can_be_explicitly_skipped_for_supervisor_recovery(tmp_path):
    result = run_startup(tmp_path, GUARDIAN_RUN_DOCTOR="false")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    assert result.returncode == 0
    lines = calls.splitlines()
    assert lines[0] == "--version"
    assert lines[1].startswith("run --control-plane.api-key=file:")
    assert lines[1].endswith(" --log.level=info --log.format=json")


def test_secret_files_are_private_and_long_lived_process_env_is_redacted(tmp_path):
    result = run_startup(tmp_path)
    secret_dir = tmp_path / "secrets"
    control = secret_dir / "control-plane-api-key"
    authorization = secret_dir / "mcp-authorization"
    assert result.returncode == 0
    assert control.read_text(encoding="utf-8") == CONTROL_SECRET
    assert authorization.read_text(encoding="utf-8") == "Bearer " + MCP_SECRET
    assert control.stat().st_mode & 0o777 == 0o600
    assert authorization.stat().st_mode & 0o777 == 0o600


def test_dockerfile_pins_official_v0014_arm64_asset_and_integrity():
    source = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "github.com/openai/tunnel-client/releases/download/v0.0.14/" in source
    assert "tunnel-client-v0.0.14-linux-arm64.zip" in source
    assert "2de3fb879a18edb847e0313592c912f1983685488290a7fdba7ac403e6a4fb0a" in source
    assert "sha256sum -c -" in source
    assert "curl --fail" in source and "--proto '=https'" in source
    assert "aarch64-base-python:3.12-alpine3.20" in source
    assert "tunnel-client --version" in source


def test_runtime_has_no_build_toolchain_and_no_guardian_changes_or_mounts():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    manifest_source = (ROOT / "config.yaml").read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM ghcr.io/home-assistant/", 1)[1]
    assert "apk add" not in runtime
    assert all(tool not in runtime for tool in ("gcc", "cargo", "rust", "musl-dev"))
    assert "/share" not in manifest_source and "/config" not in manifest_source


def test_run_script_maps_options_without_logging_values():
    source = (ROOT / "run.sh").read_text(encoding="utf-8")
    for option in ("tunnel_id", "control_plane_api_key", "mcp_auth_token",
                   "mcp_server_url", "run_doctor", "log_level"):
        assert f"bashio::config '{option}'" in source
    assert "echo $" not in source and "set -x" not in source


def test_readme_distinguishes_all_secret_roles_and_forbids_public_port():
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    for term in ("guardian_research_api_token", "guardian_api_token",
                 "mcp_auth_token", "control_plane_api_key", "tunnel_id"):
        assert term in source
    assert "does not publish a host port" in source
    assert "No router port-forward is required" in source
