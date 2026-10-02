import asyncio
import json
from dataclasses import replace

import httpx2
import pytest
from settings import load_settings
from gateway import QueryGate, QUEUE_TIMEOUT_SECONDS
from guardian_client import GuardianResearchClient
from errors import GatewayError
from test_mcp_addon import settings


def test_default_http_budget_and_independent_queue(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDIAN_MCP_GUARDIAN_TIMEOUT_SECONDS", raising=False)
    p = tmp_path / "options.json"
    p.write_text(json.dumps({"guardian_base_url":"http://guardian.test/api/research", "guardian_api_token":"test", "mcp_auth_token":"test"}))
    cfg = load_settings(p)
    assert cfg.guardian_timeout_seconds == 75
    assert QUEUE_TIMEOUT_SECONDS == 15
    for value in [0, -1, 75.01, float("inf"), float("nan")]:
        with pytest.raises(ValueError):
            replace(cfg, guardian_timeout_seconds=value).validate()
    assert replace(cfg, guardian_timeout_seconds=75).validate()
    monkeypatch.setenv("GUARDIAN_MCP_GUARDIAN_TIMEOUT_SECONDS", "12")
    assert load_settings(p).guardian_timeout_seconds == 12


@pytest.mark.parametrize("cancel", [False, True])
def test_transport_expiry_or_cancellation_closes_stream_releases_gate_and_never_retries(cancel):
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()
        calls = []
        class SlowStream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"unused"
            async def aclose(self):
                closed.set()
        async def backend(request):
            calls.append(request)
            return httpx2.Response(200, stream=SlowStream())
        client = GuardianResearchClient(settings(guardian_timeout_seconds=0.03), transport=httpx2.MockTransport(backend))
        gate = QueryGate()
        async def invoke():
            async with gate.slot(full_resolution=True, timeout=15):
                return await client.get("alarms", {})
        task = asyncio.create_task(invoke())
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            with pytest.raises(GatewayError) as exc: await task
            assert exc.value.code == "timeout" and exc.value.origin == "guardian_transport"
        assert task.done() and closed.is_set() and len(calls) == 1
        assert gate.snapshot() == {"active_queries": 0, "queued_queries": 0}
        assert gate.active_full == 0
    asyncio.run(scenario())


def test_queue_timeout_never_calls_backend_or_leaks_capacity():
    async def scenario():
        gate = QueryGate()
        calls = []
        async with gate.slot(full_resolution=True, timeout=15):
            with pytest.raises(GatewayError) as exc:
                async with gate.slot(full_resolution=True, timeout=0.01): calls.append("backend")
            assert exc.value.origin == "mcp_queue"
            assert gate.active == 1 and gate.queued == 0 and not calls
        assert gate.active == gate.active_full == gate.queued == 0
    asyncio.run(scenario())


def test_invocation_uses_separate_queue_and_full_http_budget(monkeypatch):
    from tools import register_tools
    from gateway import ServiceState
    import guardian_client
    from contextlib import asynccontextmanager
    cfg = settings(guardian_timeout_seconds=75)
    waits, calls = [], []
    original_timeout = asyncio.timeout
    def capture_timeout(value):
        waits.append(value)
        return original_timeout(value)
    monkeypatch.setattr(guardian_client.asyncio, "timeout", capture_timeout)
    class Server:
        def __init__(self): self.tools = {}
        def tool(self, **kwargs):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register
    class Gate:
        @asynccontextmanager
        async def slot(self, **kwargs):
            calls.append(kwargs)
            yield
    expected = {"data": {"alarms": [{"id": i} for i in range(174)]}, "truncated": False}
    async def backend(request):
        assert request.extensions["timeout"]["read"] == 75
        return httpx2.Response(200, json=expected)
    server = Server()
    register_tools(server, GuardianResearchClient(cfg, transport=httpx2.MockTransport(backend)), Gate(), ServiceState())
    result = asyncio.run(server.tools["query_alarm_history"](
        timestamp_from="2026-09-11T00:00:00Z", timestamp_to="2026-09-12T00:00:00Z"))
    assert result == expected
    assert calls == [{"full_resolution": False, "timeout": 15}]
    assert waits == [75]
