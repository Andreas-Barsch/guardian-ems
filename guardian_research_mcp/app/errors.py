"""Stable transport error codes without backend or filesystem leakage."""
from __future__ import annotations

import json
from mcp.server.mcpserver.exceptions import ToolError

KNOWN_CODES = frozenset({
    "invalid_argument", "identity_unresolved", "coverage_absent",
    "range_too_large", "response_too_large", "timeout", "busy",
    "cursor_invalid", "source_unavailable",
})
PUBLIC_MESSAGES = {
    "invalid_argument": "request arguments are invalid",
    "identity_unresolved": "physical identity is unresolved for the requested time",
    "coverage_absent": "requested evidence coverage is absent",
    "range_too_large": "requested range exceeds the bounded limit",
    "response_too_large": "response exceeds the bounded limit",
    "timeout": "research request timed out",
    "busy": "research service is busy",
    "cursor_invalid": "cursor is invalid or expired",
    "source_unavailable": "Guardian Research API is unavailable",
}


class GatewayError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code if code in KNOWN_CODES else "source_unavailable"
        super().__init__(PUBLIC_MESSAGES[self.code])

    def as_tool_error(self) -> ToolError:
        return ToolError(json.dumps(
            {"error": {"code": self.code, "message": str(self)}},
            separators=(",", ":"),
        ))
