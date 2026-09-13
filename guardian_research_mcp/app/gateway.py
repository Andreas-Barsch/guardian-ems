"""Bounded query execution, operational state, and metadata-only audit."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from errors import GatewayError

LOG = logging.getLogger("guardian_research_mcp.audit")
MAX_PARALLEL = 2
MAX_FULL_RESOLUTION = 1
MAX_QUEUE = 8


class QueryGate:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self.active = 0
        self.active_full = 0
        self.queued = 0

    @property
    def active_full(self) -> int:
        return self._active_full

    @active_full.setter
    def active_full(self, value: int) -> None:
        self._active_full = value

    def snapshot(self) -> dict[str, int]:
        return {"active_queries": self.active, "queued_queries": self.queued}

    @asynccontextmanager
    async def slot(self, *, full_resolution: bool, timeout: float):
        async with self._condition:
            def available() -> bool:
                return self.active < MAX_PARALLEL and (
                    not full_resolution or self.active_full < MAX_FULL_RESOLUTION)
            if not available():
                if self.queued >= MAX_QUEUE:
                    raise GatewayError("busy", "MCP query queue is full")
                self.queued += 1
                try:
                    async with asyncio.timeout(timeout):
                        await self._condition.wait_for(available)
                except TimeoutError as exc:
                    raise GatewayError("timeout", "MCP query queue timed out") from exc
                finally:
                    self.queued -= 1
            self.active += 1
            if full_resolution:
                self.active_full += 1
        try:
            yield
        finally:
            async with self._condition:
                self.active -= 1
                if full_resolution:
                    self.active_full -= 1
                self._condition.notify_all()


@dataclass
class ServiceState:
    guardian_reachable: bool = False
    last_error: str | None = None


def record_count(payload: object) -> int | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return None
    data = payload["data"]
    for key in ("point_count", "sample_count"):
        if isinstance(data.get(key), int):
            return data[key]
    for key in ("events", "alarms", "epochs", "datasets", "points"):
        if isinstance(data.get(key), list):
            return len(data[key])
    return None


def audit(*, request_id: str, tool: str, client_id: str | None,
          params: dict[str, object], duration: float, payload: object,
          byte_count: int, status: str) -> None:
    cells = params.get("cell_numbers") or []
    LOG.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "client_id": client_id,
        "tool": tool,
        "scope": "research:read",
        "timestamp_from": params.get("from"),
        "timestamp_to": params.get("to"),
        "serial_count": 1 if params.get("physical_serial") else 0,
        "cell_count": len(cells) if isinstance(cells, list) else 0,
        "duration_seconds": round(duration, 6),
        "records": record_count(payload),
        "bytes": byte_count,
        "truncated": payload.get("truncated") if isinstance(payload, dict) else None,
        "status": status,
    }, separators=(",", ":"), sort_keys=True))


def request_id() -> str:
    return str(uuid.uuid4())
