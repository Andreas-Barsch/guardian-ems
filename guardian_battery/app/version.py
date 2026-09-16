"""Central Guardian Battery software and immutable build provenance."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

GUARDIAN_VERSION = "0.8.1"
DIAGNOSTIC_ENGINE_VERSION = "0.4.12"
RESEARCH_SEMANTICS_VERSION = "research_soc_crash_evidence_v2"

BUILD_INFO_PATH: Final = Path("/app/build-info.json")
UNAVAILABLE_SOURCE_COMMIT: Final = "unavailable"
_COMMIT_PATTERN: Final = re.compile(r"[0-9a-f]{40}")


def load_source_commit(path: Path = BUILD_INFO_PATH) -> str:
    """Load the immutable revision sealed into the image at build time."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return UNAVAILABLE_SOURCE_COMMIT
    if payload.get("guardian_version") != GUARDIAN_VERSION:
        return UNAVAILABLE_SOURCE_COMMIT
    value = payload.get("source_commit")
    if not isinstance(value, str) or _COMMIT_PATTERN.fullmatch(value) is None:
        return UNAVAILABLE_SOURCE_COMMIT
    return value


SOURCE_COMMIT = load_source_commit()


def require_source_commit(value: str = SOURCE_COMMIT) -> str:
    """Reject startup rather than report absent or malformed provenance."""
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise RuntimeError("Guardian Battery runtime source provenance unavailable")
    return value
