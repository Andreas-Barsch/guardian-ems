"""ASGI security boundary for MCP and health endpoints."""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from settings import Settings


class SecurityMiddleware:
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", ())}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        host = headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in {item.lower() for item in self.settings.allowed_hosts}:
            await JSONResponse({"error": "host_not_allowed"}, status_code=421)(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            normalized = f"{parsed.scheme}://{parsed.netloc}"
            if normalized not in self.settings.allowed_origins:
                await JSONResponse({"error": "origin_not_allowed"}, status_code=403)(scope, receive, send)
                return
        if not self.settings.development_auth_mode:
            scheme, _, supplied = headers.get("authorization", "").partition(" ")
            if (scheme.lower() != "bearer" or not supplied or
                    not secrets.compare_digest(supplied, self.settings.mcp_auth_token)):
                await JSONResponse({"error": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)
