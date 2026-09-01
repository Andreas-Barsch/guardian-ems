"""Time-aware projection from passive RS485 serial evidence to stack identity."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from position_history import (DEFAULT_POSITION_HISTORY_FILE, PositionHistoryError,
                              documented_position_at)


def _iso(timestamp: float | str | datetime) -> str:
    if isinstance(timestamp, datetime):
        return timestamp.astimezone(timezone.utc).isoformat()
    if isinstance(timestamp, str):
        return timestamp
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def resolve_rs485_identity(adr: int, serial_string: str, serial_raw: str,
                           timestamp: float | str | datetime, *,
                           decode_source: str,
                           position_history_path: Path | str = DEFAULT_POSITION_HISTORY_FILE) -> dict:
    """Resolve one direct ADR/serial observation without deriving a position."""
    try:
        position, snapshot_id = documented_position_at(
            position_history_path, serial_string, _iso(timestamp))
    except (PositionHistoryError, ValueError, OSError):
        position = snapshot_id = None
    return {
        "adr": int(adr), "serial_string": serial_string, "serial_raw": serial_raw,
        "timestamp": timestamp, "decode_source": decode_source,
        "identity_source": decode_source,
        "identity_resolved": position is not None,
        "physical_serial": serial_string,
        "position": position,
        "position_history_id": snapshot_id,
        "quality": {
            "evidence_level": "observation",
            "identity_source": "position_history" if position is not None else "unresolved",
            "causality": "not_determined",
        },
    }


def project_current_management(management: dict[int, dict], identities: dict[int, dict], *,
                               position_history_path: Path | str = DEFAULT_POSITION_HISTORY_FILE,
                               at: float | str | datetime | None = None) -> dict[int, dict]:
    """Attach current physical identity while preserving every management value."""
    target = at if at is not None else datetime.now(timezone.utc)
    result = {}
    for adr, values in management.items():
        identity = identities.get(int(adr))
        if identity and identity.get("serial_string"):
            resolved = resolve_rs485_identity(
                int(adr), identity["serial_string"], identity["serial_raw"], target,
                decode_source=identity.get("decode_source", "stored_decoded"),
                position_history_path=position_history_path)
            resolved["identity_known"] = bool(identity.get("identity_known", True))
            resolved["identity_currently_confirmed"] = bool(
                identity.get("identity_currently_confirmed", False))
        else:
            resolved = {
                "adr": int(adr), "serial_string": None, "serial_raw": None,
                "decode_source": None, "identity_resolved": False,
                "identity_source": None, "identity_known": False,
                "identity_currently_confirmed": False,
                "physical_serial": None, "position": None,
                "position_history_id": None,
                "quality": {"evidence_level": "observation",
                            "identity_source": "unresolved",
                            "causality": "not_determined"},
            }
        result[int(adr)] = {**values, **resolved}
    return result
