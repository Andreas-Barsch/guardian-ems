"""HTTP-only adapter for the existing Guardian Research API."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import httpx2

from errors import GatewayError
from settings import Settings

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class GuardianResearchClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    @staticmethod
    def _query(params: dict[str, object]) -> str:
        values: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                encoded = ",".join(str(item) for item in value)
            elif isinstance(value, bool):
                encoded = "true" if value else "false"
            else:
                encoded = str(value)
            values.append((key, encoded))
        return urlencode(values)

    async def get(self, endpoint: str, params: dict[str, object]) -> tuple[dict, int]:
        url = self.settings.guardian_base_url + "/" + endpoint.strip("/")
        query = self._query(params)
        if query:
            url += "?" + query
        headers = {
            "Authorization": "Bearer " + self.settings.guardian_api_token,
            "Accept": "application/json",
            "User-Agent": "guardian-research-mcp/0.8.0",
        }
        try:
            async with httpx2.AsyncClient(
                transport=self.transport,
                timeout=self.settings.guardian_timeout_seconds,
                follow_redirects=False,
            ) as client:
                async with asyncio.timeout(self.settings.guardian_timeout_seconds):
                    async with client.stream("GET", url, headers=headers) as response:
                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > MAX_RESPONSE_BYTES:
                                    raise GatewayError("response_too_large", "")
                            except ValueError as exc:
                                raise GatewayError("source_unavailable", "") from exc
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise GatewayError("response_too_large", "")
                            chunks.extend(chunk)
                        body = bytes(chunks)
                        status_code = response.status_code
        except TimeoutError as exc:
            raise GatewayError("timeout", "Guardian Research API timed out") from exc
        except httpx2.TimeoutException as exc:
            raise GatewayError("timeout", "Guardian Research API timed out") from exc
        except httpx2.RequestError as exc:
            raise GatewayError("source_unavailable", "Guardian Research API unavailable") from exc
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("source_unavailable", "Guardian returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GatewayError("source_unavailable", "Guardian returned an invalid envelope")
        if status_code >= 400:
            detail = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise GatewayError(
                str(detail.get("code", "source_unavailable")),
                "",
            )
        return payload, len(body)
