import asyncio
import json
import logging
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import uvicorn
import yaml
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

from errors import GatewayError
from gateway import MAX_FULL_RESOLUTION, MAX_PARALLEL, MAX_QUEUE, QueryGate
from guardian_client import GuardianResearchClient, MAX_RESPONSE_BYTES
from server import build_app
from settings import Settings, load_settings


TOKEN = "mcp-test-secret"
GUARDIAN_TOKEN = "guardian-test-secret"
TOOL_NAMES = {
    "guardian_status", "get_stack_topology_at", "get_identity_epochs",
    "get_maintenance_events", "get_data_coverage", "query_module_history",
    "query_cell_history", "query_phase_history", "query_daily_diagnostics",
    "query_diagnostic_evidence", "find_soc_crashes", "find_low_voltage_events",
    "query_alarm_history", "query_timeseries", "build_evidence_package",
}


def settings(**changes):
    values = dict(
        guardian_base_url="http://guardian.test:8099/api/research",
        guardian_api_token=GUARDIAN_TOKEN,
        mcp_auth_token=TOKEN,
        development_auth_mode=False,
        allowed_hosts=("127.0.0.1", "localhost"),
        allowed_origins=("https://trusted.example",),
        guardian_timeout_seconds=2,
    )
    values.update(changes)
    return Settings(**values).validate()


def envelope(data=None):
    return {
        "research_schema_version": 1,
        "data_source": "guardian.test",
        "evidence_class": "OBSERVED",
        "authoritative": True,
        "timestamp_range": {"from": None, "to": None},
        "resolution": "test",
        "quality": {"status": "complete", "confidence": None},
        "truncated": False,
        "next_cursor": None,
        "data": data or {},
    }


class GuardianStub:
    def __init__(self):
        self.available = True
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if not self.available:
            raise httpx2.ConnectError("offline", request=request)
        assert request.headers["authorization"] == "Bearer " + GUARDIAN_TOKEN
        path = request.url.path.removeprefix("/api/research/")
        query = parse_qs(request.url.query.decode())
        if path == "status":
            return httpx2.Response(200, json={"research_schema_version": 1,
                "read_only": True, "enabled": True})
        if path == "topology":
            return httpx2.Response(200, json=envelope({"positions": [{
                "position": 4, "physical_serial": "SERIAL-M4"}]}))
        if path == "identity-epochs":
            serial = query.get("physical_serial", ["SERIAL-M4"])[0]
            return httpx2.Response(200, json=envelope({"epochs": [{
                "physical_serial": serial, "position": int(serial[-1]),
                "identity_epoch_id": "EPOCH-" + serial}]}))
        if path == "events/soc-crashes":
            return httpx2.Response(200, json=envelope({"events": [{
                "event_id": "SCE-M4", "physical_serial": "SERIAL-M4",
                "detector_version": "guardian_soc_crash_v1"}]}))
        if path == "evidence-package":
            return httpx2.Response(200, json=envelope({
                "event": {"event_id": query["event_id"][0]}, "inferred": False,
                "input_fingerprint": "sha256-test"}))
        if path == "cell-history":
            cell = int(query.get("cell_numbers", ["15"])[0].split(",")[0])
            return httpx2.Response(200, json=envelope({"points": [{
                "cell_number": cell, "physical_serial": query["physical_serial"][0],
                "value": 3271}], "point_count": 1}))
        if path == "coverage":
            return httpx2.Response(200, json=envelope({"datasets": [{
                "dataset": name, "quality": "absent" if name == "soh" else "complete"
            } for name in query.get("datasets", [""])[0].split(",")]}))
        return httpx2.Response(200, json=envelope({"events": [], "points": []}))


def client_for(stub, **changes):
    cfg = settings(**changes)
    return GuardianResearchClient(cfg, transport=httpx2.MockTransport(stub))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_app(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


async def protocol_client(base_url, callback):
    async with httpx2.AsyncClient(headers={
        "Authorization": "Bearer " + TOKEN,
        "Host": urlsplit(base_url).netloc,
    }) as http_client:
        transport = streamable_http_client(base_url + "/mcp", http_client=http_client)
        async with Client(transport, mode="legacy") as client:
            return await callback(client)


def test_settings_default_closed_and_no_wildcards():
    with pytest.raises(ValueError, match="mcp_auth_token"):
        settings(mcp_auth_token="")
    with pytest.raises(ValueError, match="non-wildcard"):
        settings(allowed_hosts=("*",))
    with pytest.raises(ValueError, match="guardian_api_token"):
        settings(guardian_api_token="")
    assert settings(development_auth_mode=True, mcp_auth_token="").development_auth_mode


def test_manifest_and_runtime_defaults_allow_exact_ha_app_dns_without_wildcard(
    tmp_path, monkeypatch,
):
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
    )
    expected = {
        "guardian_research_mcp",
        "3195b09a-guardian-research-mcp",
        "localhost",
        "127.0.0.1",
    }
    assert manifest["version"] == "0.8.1"
    assert set(manifest["options"]["allowed_hosts"].split(",")) == expected
    assert "*" not in manifest["options"]["allowed_hosts"]
    options = tmp_path / "options.json"
    options.write_text(json.dumps({
        "guardian_base_url": "http://guardian.test:8099/api/research",
        "guardian_api_token": GUARDIAN_TOKEN,
        "mcp_auth_token": TOKEN,
    }), encoding="utf-8")
    monkeypatch.delenv("GUARDIAN_MCP_ALLOWED_HOSTS", raising=False)
    assert set(load_settings(options).allowed_hosts) == expected


def test_guardian_client_get_only_preserves_envelope_and_cursor():
    stub = GuardianStub()
    async def run():
        payload, size = await client_for(stub).get("module-history", {
            "physical_serial": "SERIAL-M4", "metric": "soc",
            "from": "2026-09-11T00:00:00Z", "to": "2026-09-12T00:00:00Z",
            "cursor": "opaque", "cell_numbers": [15]})
        assert payload["evidence_class"] == "OBSERVED" and size > 0
    asyncio.run(run())
    request = stub.requests[0]
    assert request.method == "GET"
    assert parse_qs(request.url.query.decode())["cursor"] == ["opaque"]
    assert parse_qs(request.url.query.decode())["cell_numbers"] == ["15"]


def test_error_mapping_payload_limit_and_no_backend_path():
    async def handler(request):
        if request.url.path.endswith("status"):
            return httpx2.Response(400, json={"error": {
                "code": "cursor_invalid",
                "message": GUARDIAN_TOKEN + " /share/private/evidence.jsonl"}})
        return httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    client = GuardianResearchClient(settings(), transport=httpx2.MockTransport(handler))
    async def run():
        with pytest.raises(GatewayError) as known:
            await client.get("status", {})
        assert known.value.code == "cursor_invalid"
        assert GUARDIAN_TOKEN not in str(known.value) and "/share" not in str(known.value)
        with pytest.raises(GatewayError) as large:
            await client.get("large", {})
        assert large.value.code == "response_too_large"
        assert "/share" not in str(known.value) + str(large.value)
    asyncio.run(run())


def test_client_cancellation_closes_inflight_guardian_request():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def handler(request):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    client = GuardianResearchClient(settings(), transport=httpx2.MockTransport(handler))
    async def run():
        task = asyncio.create_task(client.get("status", {}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), 1)
    asyncio.run(run())


def test_query_gate_limits_queue_and_cancellation():
    gate = QueryGate()
    assert (MAX_PARALLEL, MAX_FULL_RESOLUTION, MAX_QUEUE) == (2, 1, 8)
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def holder():
            async with gate.slot(full_resolution=True, timeout=1):
                entered.set()
                await release.wait()
        task = asyncio.create_task(holder())
        await entered.wait()
        waiter = asyncio.create_task(gate.slot(full_resolution=True, timeout=1).__aenter__())
        await asyncio.sleep(0)
        assert gate.queued == 1
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gate.queued == 0
        release.set()
        await task
        assert gate.snapshot() == {"active_queries": 0, "queued_queries": 0}
    asyncio.run(run())


def test_query_gate_rejects_ninth_waiter():
    gate = QueryGate()
    async def run():
        release = asyncio.Event()
        entered = 0
        async def holder():
            nonlocal entered
            async with gate.slot(full_resolution=False, timeout=2):
                entered += 1
                await release.wait()
        holders = [asyncio.create_task(holder()) for _ in range(2)]
        while entered != 2:
            await asyncio.sleep(0)
        waiters = [asyncio.create_task(holder()) for _ in range(MAX_QUEUE)]
        while gate.queued != MAX_QUEUE:
            await asyncio.sleep(0)
        with pytest.raises(GatewayError) as full:
            async with gate.slot(full_resolution=False, timeout=1):
                pass
        assert full.value.code == "busy"
        release.set()
        await asyncio.gather(*holders, *waiters)
    asyncio.run(run())


def test_protocol_discovery_security_health_and_read_only_catalog():
    stub = GuardianStub()
    app = build_app(settings(), client=client_for(stub))
    with running_app(app) as base:
        with httpx2.Client() as http:
            assert http.get(base + "/health").status_code == 401
            assert http.get(base + "/health", headers={
                "Authorization": "Bearer wrong"}).status_code == 401
            assert http.get(base + "/health", headers={
                "Authorization": "Bearer " + TOKEN,
                "Host": "evil.example"}).status_code == 421
            assert http.get(base + "/health", headers={
                "Authorization": "Bearer " + TOKEN,
                "Origin": "https://evil.example"}).status_code == 403
            health = http.get(base + "/health", headers={
                "Authorization": "Bearer " + TOKEN})
            assert health.status_code == 200
            assert health.json()["version"] == "0.8.1"
            assert set(health.json()) == {"service", "version", "transport",
                "guardian_reachable", "active_queries", "queued_queries", "last_error"}

        async def check(client):
            assert client.server_info.name == "guardian-research-mcp"
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == TOOL_NAMES
            assert all(tool.annotations.read_only_hint is True for tool in tools.tools)
            assert all(tool.annotations.destructive_hint is False for tool in tools.tools)
            status = await client.call_tool("guardian_status", {})
            assert status.is_error is False
            assert status.structured_content["read_only"] is True
            unknown = await client.call_tool("write_guardian_config", {})
            assert unknown.is_error is True
        asyncio.run(protocol_client(base, check))


def test_guardian_down_recovery_without_mcp_restart():
    stub = GuardianStub()
    app = build_app(settings(), client=client_for(stub))
    with running_app(app) as base:
        async def check(client):
            stub.available = False
            failed = await client.call_tool("guardian_status", {})
            assert failed.is_error is True
            assert "source_unavailable" in failed.content[0].text
            stub.available = True
            recovered = await client.call_tool("guardian_status", {})
            assert recovered.is_error is False
        asyncio.run(protocol_client(base, check))
        health = httpx2.get(base + "/health", headers={"Authorization": "Bearer " + TOKEN})
        assert health.json()["guardian_reachable"] is True
        assert health.json()["last_error"] is None


def test_repository_qualified_ha_dns_host_is_allowed_without_wildcard():
    host = "3195b09a-guardian-research-mcp"
    stub = GuardianStub()
    cfg = settings(allowed_hosts=(
        "guardian_research_mcp", host, "localhost", "127.0.0.1",
    ))
    app = build_app(cfg, client=GuardianResearchClient(
        cfg, transport=httpx2.MockTransport(stub),
    ))
    with running_app(app) as base:
        response = httpx2.get(base + "/health", headers={
            "Authorization": "Bearer " + TOKEN,
            "Host": host,
        })
        assert response.status_code == 200
        assert httpx2.get(base + "/health", headers={
            "Authorization": "Bearer " + TOKEN,
            "Host": "unlisted.internal",
        }).status_code == 421


def test_every_tool_maps_to_exact_get_only_research_endpoint():
    stub = GuardianStub()
    app = build_app(settings(), client=client_for(stub))
    with running_app(app) as base:
        async def check(client):
            common = {"timestamp_from": "2026-09-11T00:00:00Z",
                      "timestamp_to": "2026-09-12T00:00:00Z"}
            calls = [
                ("guardian_status", {}),
                ("get_stack_topology_at", {"timestamp": common["timestamp_from"]}),
                ("get_identity_epochs", {"physical_serial": "SERIAL-M4", **common}),
                ("get_maintenance_events", {"physical_serial": "SERIAL-M4", **common}),
                ("get_data_coverage", {"physical_serial": "SERIAL-M4",
                                       "datasets": ["soc"], **common}),
                ("query_module_history", {"physical_serial": "SERIAL-M4",
                                          "metric": "soc", **common}),
                ("query_cell_history", {"physical_serial": "SERIAL-M4",
                                        "metric": "cell_voltage", "cell_numbers": [15], **common}),
                ("query_phase_history", {"physical_serial": "SERIAL-M4", **common}),
                ("query_daily_diagnostics", {"date": "2026-09-11"}),
                ("query_diagnostic_evidence", {"date": "2026-09-11"}),
                ("find_soc_crashes", {"physical_serial": "SERIAL-M4", **common}),
                ("find_low_voltage_events", {"physical_serial": "SERIAL-M4", **common}),
                ("query_alarm_history", {"physical_serial": "SERIAL-M4", **common}),
                ("query_timeseries", {"source": "guardian.cell_history", "metric": "soc",
                                      "physical_serial": "SERIAL-M4", **common}),
                ("build_evidence_package", {"event_id": "SCE-M4"}),
            ]
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                assert result.is_error is False, (name, result.content)
        asyncio.run(protocol_client(base, check))
    expected = {
        "status", "topology", "identity-epochs", "maintenance", "coverage",
        "module-history", "cell-history", "phases", "daily-diagnostics",
        "diagnostic-evidence", "events/soc-crashes", "events/low-voltage",
        "alarms", "timeseries", "evidence-package",
    }
    assert {request.url.path.removeprefix("/api/research/")
            for request in stub.requests} == expected
    assert {request.method for request in stub.requests} == {"GET"}
    package_request = next(request for request in stub.requests
                           if request.url.path.endswith("/evidence-package"))
    package_query = parse_qs(package_request.url.query.decode())
    assert package_query["before"] == ["P1D"]
    assert package_query["after"] == ["PT30M"]


def test_m4_m5_m6_evidence_chain_and_absent_coverage_use_only_mcp():
    stub = GuardianStub()
    app = build_app(settings(), client=client_for(stub))
    with running_app(app) as base:
        async def check(client):
            topology = await client.call_tool("get_stack_topology_at", {
                "timestamp": "2026-09-11T10:00:00Z"})
            assert topology.structured_content["data"]["positions"][0]["physical_serial"] == "SERIAL-M4"
            for serial, cell in (("SERIAL-M4", 15), ("SERIAL-M5", 8), ("SERIAL-M6", 5)):
                epochs = await client.call_tool("get_identity_epochs", {
                    "physical_serial": serial,
                    "timestamp_from": "2026-09-10T00:00:00Z",
                    "timestamp_to": "2026-09-12T00:00:00Z"})
                assert epochs.structured_content["data"]["epochs"][0]["physical_serial"] == serial
                cells = await client.call_tool("query_cell_history", {
                    "physical_serial": serial, "metric": "cell_voltage",
                    "cell_numbers": [cell], "timestamp_from": "2026-09-11T00:00:00Z",
                    "timestamp_to": "2026-09-12T00:00:00Z"})
                assert cells.structured_content["data"]["points"][0]["cell_number"] == cell
            crashes = await client.call_tool("find_soc_crashes", {
                "physical_serial": "SERIAL-M4",
                "timestamp_from": "2026-09-11T00:00:00Z",
                "timestamp_to": "2026-09-12T00:00:00Z"})
            event_id = crashes.structured_content["data"]["events"][0]["event_id"]
            package = await client.call_tool("build_evidence_package", {"event_id": event_id})
            assert package.structured_content["data"]["inferred"] is False
            coverage = await client.call_tool("get_data_coverage", {
                "physical_serial": "SERIAL-M4", "datasets": ["soc", "soh"],
                "timestamp_from": "2026-09-11T00:00:00Z",
                "timestamp_to": "2026-09-12T00:00:00Z"})
            values = {item["dataset"]: item["quality"]
                      for item in coverage.structured_content["data"]["datasets"]}
            assert values == {"soc": "complete", "soh": "absent"}
        asyncio.run(protocol_client(base, check))


def test_audit_excludes_tokens_and_payload(caplog):
    stub = GuardianStub()
    app = build_app(settings(), client=client_for(stub))
    with caplog.at_level(logging.INFO, logger="guardian_research_mcp.audit"):
        with running_app(app) as base:
            async def check(client):
                await client.call_tool("query_module_history", {
                    "physical_serial": "SERIAL-M4", "metric": "soc",
                    "timestamp_from": "2026-09-11T00:00:00Z",
                    "timestamp_to": "2026-09-12T00:00:00Z"})
            asyncio.run(protocol_client(base, check))
    text = "\n".join(item.message for item in caplog.records)
    assert TOKEN not in text and GUARDIAN_TOKEN not in text
    assert '"tool":"query_module_history"' in text
    assert '"points"' not in text
    entry = json.loads(caplog.records[-1].message)
    assert entry["timestamp_from"] == "2026-09-11T00:00:00Z"
    assert entry["timestamp_to"] == "2026-09-12T00:00:00Z"


def test_addon_manifest_has_no_evidence_mount_or_default_port_and_client_is_get_only():
    root = Path(__file__).resolve().parents[1]
    manifest = (root / "config.yaml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    client_source = (root / "app" / "guardian_client.py").read_text(encoding="utf-8")
    assert "8098/tcp: null" in manifest
    assert "map:" not in manifest
    assert "/share" not in manifest and "/config" not in manifest
    assert "mcp==2.2.0" in (root / "requirements.lock").read_text(encoding="utf-8")
    assert "--only-binary=:all:" in dockerfile
    assert "apk add" not in dockerfile
    assert 'client.stream("GET"' in client_source
    assert all(token not in client_source for token in (
        'client.post(', 'client.put(', 'client.patch(', 'client.delete('))


def test_local_mcp_transport_overhead_is_below_250_ms():
    stub = GuardianStub()
    guardian = client_for(stub)
    app = build_app(settings(), client=guardian)
    samples = 12
    async def direct():
        started = time.perf_counter()
        for _ in range(samples):
            await guardian.get("status", {})
        return (time.perf_counter() - started) / samples
    direct_seconds = asyncio.run(direct())
    with running_app(app) as base:
        async def measured(client):
            started = time.perf_counter()
            for _ in range(samples):
                result = await client.call_tool("guardian_status", {})
                assert result.is_error is False
            return (time.perf_counter() - started) / samples
        mcp_seconds = asyncio.run(protocol_client(base, measured))
    overhead = mcp_seconds - direct_seconds
    print(f"MCP_OVERHEAD_SECONDS={overhead:.6f}")
    assert overhead < 0.250
