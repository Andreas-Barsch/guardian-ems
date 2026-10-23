"""Stable transport error codes without backend or filesystem leakage."""
from __future__ import annotations

import json
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

KNOWN_CODES = frozenset({
    "invalid_argument", "identity_unresolved", "coverage_absent",
    "range_too_large", "response_too_large", "timeout", "busy",
    "cursor_invalid", "source_unavailable", "not_found",
    'live_acquisition_priority',
    'history_busy',
    'history_index_required',
    'history_context_index_required',
    'history_preparation_required',
    'history_record_too_large',
    'cancelled',
    'history_window_budget',
    'history_product_invalid',
})
PUBLIC_MESSAGES = {
    'live_acquisition_priority': 'Live acquisition has priority; submit this historical request again manually',
    'history_busy': 'Another historical job is running; submit again manually',
    'history_index_required': 'The requested history needs a current index; no complete coverage is available',
    'history_context_index_required': 'Historical context needs an index; no complete coverage is available',
    'history_preparation_required': 'The requested historical source is not prepared',
    'history_record_too_large': 'A history record exceeds the bounded parser limit',
    'cancelled': 'Research request was cancelled',
    'history_window_budget': 'Selected historical products exceed the bounded read budget',
    'history_product_invalid': 'Requested persisted historical product is inconsistent',
    "not_found": "requested resource was not found",
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
    def __init__(self, code: str, message: str, *, origin: str = "mcp"):
        self.origin = origin
        self.code = code if code in KNOWN_CODES else "source_unavailable"
        super().__init__(PUBLIC_MESSAGES[self.code])

    def as_tool_error(self) -> ToolError:
        return ToolError(json.dumps(
            {"error": {"code": self.code, "message": str(self)}},
            separators=(",", ":"),
        ))

    def as_tool_result(self) -> CallToolResult:
        """Preserve the domain error in both MCP representations, without secrets."""
        payload = {"error": {"code": self.code, "message": str(self), "origin": self.origin}}
        return CallToolResult(is_error=True, structured_content=payload,
            content=[TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))])
