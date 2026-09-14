"""Strict runtime configuration for the Guardian Research MCP add-on."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

OPTIONS_FILE = Path("/data/options.json")


def _csv(value: object) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


def _bool(value: object) -> bool:
    return value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    guardian_base_url: str
    guardian_api_token: str
    mcp_auth_token: str
    development_auth_mode: bool
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    log_level: str = "INFO"
    bind_host: str = "0.0.0.0"
    port: int = 8098
    guardian_timeout_seconds: float = 15.0

    def validate(self) -> "Settings":
        split = urlsplit(self.guardian_base_url)
        if split.scheme not in {"http", "https"} or not split.hostname:
            raise ValueError("guardian_base_url must be an HTTP(S) URL")
        if split.username or split.password or split.query or split.fragment:
            raise ValueError("guardian_base_url must not contain credentials, query, or fragment")
        if not split.path.rstrip("/").endswith("/api/research"):
            raise ValueError("guardian_base_url must end with /api/research")
        if not self.guardian_api_token:
            raise ValueError("guardian_api_token is required")
        if not self.development_auth_mode and not self.mcp_auth_token:
            raise ValueError("mcp_auth_token is required outside development mode")
        if not self.allowed_hosts or "*" in self.allowed_hosts or "*" in self.allowed_origins:
            raise ValueError("explicit non-wildcard allowed_hosts/origins are required")
        if not 0 < self.guardian_timeout_seconds <= 15:
            raise ValueError("guardian timeout exceeds Guardian hard deadline")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("invalid log level")
        return self


def load_settings(path: Path = OPTIONS_FILE) -> Settings:
    values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    def option(name: str, default: object = "") -> object:
        return os.environ.get("GUARDIAN_MCP_" + name.upper(), values.get(name, default))
    return Settings(
        guardian_base_url=str(option("guardian_base_url", "")).rstrip("/"),
        guardian_api_token=str(option("guardian_api_token")),
        mcp_auth_token=str(option("mcp_auth_token")),
        development_auth_mode=_bool(option("development_auth_mode", False)),
        allowed_hosts=_csv(option(
            "allowed_hosts",
            "guardian_research_mcp,3195b09a-guardian-research-mcp,localhost,127.0.0.1",
        )),
        allowed_origins=_csv(option("allowed_origins", "")),
        log_level=str(option("log_level", "INFO")).upper(),
        bind_host=str(option("bind_host", "0.0.0.0")),
        port=int(option("port", 8098)),
        guardian_timeout_seconds=float(option("guardian_timeout_seconds", 15)),
    ).validate()
