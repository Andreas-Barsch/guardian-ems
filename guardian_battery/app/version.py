"""Guardian versions and build provenance sealed into the image."""

import json
import re
from pathlib import Path

GUARDIAN_VERSION = "0.9.11"
DIAGNOSTIC_ENGINE_VERSION = "0.4.12"
BUILD_INFO_PATH = Path("/app/build-info.json")


def require_source_commit(path: Path = BUILD_INFO_PATH) -> str:
    """Fail before acquisition when the image has no valid matching provenance."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("guardian_version") != GUARDIAN_VERSION:
            raise ValueError("build version mismatch")
        revision = payload.get("source_commit")
        if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("invalid source commit")
        return revision
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("Guardian Battery runtime source provenance unavailable or invalid") from exc
