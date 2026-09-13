"""Guardian Research MCP process entrypoint."""
from __future__ import annotations

import json
import logging

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from gateway import QueryGate, ServiceState
from guardian_client import GuardianResearchClient
from security import SecurityMiddleware
from settings import Settings, load_settings
from tools import register_tools

SERVICE = "guardian-research-mcp"
VERSION = "0.8.0"
MAX_MCP_REQUEST_BYTES = 1024 * 1024


def build_app(settings: Settings, *, client: GuardianResearchClient | None = None):
    gate = QueryGate()
    state = ServiceState()
    client = client or GuardianResearchClient(settings)
    server = MCPServer(
        SERVICE,
        version=VERSION,
        instructions=(
            "Read-only Guardian evidence transport. physical_serial is the primary identity; "
            "position is time-dependent; OBSERVED and DERIVED are distinct; missing coverage "
            "is not zero; Guardian and this MCP server make no causal or INFERRED claims."
        ),
    )
    register_tools(server, client, gate, state)
    allowed_hosts = []
    for host in settings.allowed_hosts:
        allowed_hosts.extend((host, host + ":*"))
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=list(settings.allowed_origins),
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_MCP_REQUEST_BYTES,
        session_idle_timeout=None,
        max_sessions=None,
        transport_security=transport_security,
        host=settings.bind_host,
    )

    async def health(_request: Request):
        return JSONResponse({
            "service": SERVICE,
            "version": VERSION,
            "transport": "streamable-http-stateless",
            "guardian_reachable": state.guardian_reachable,
            **gate.snapshot(),
            "last_error": state.last_error,
        })

    app.add_route("/health", health, methods=["GET"])
    app.add_middleware(SecurityMiddleware, settings=settings)
    app.state.mcp_server = server
    app.state.query_gate = gate
    app.state.service_state = state
    app.state.guardian_client = client
    return app


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("guardian_research_mcp").info(json.dumps({
        "event": "service_start", "service": SERVICE, "version": VERSION,
        "transport": "streamable-http-stateless",
    }, separators=(",", ":")))
    uvicorn.run(build_app(settings), host=settings.bind_host, port=settings.port,
                log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
