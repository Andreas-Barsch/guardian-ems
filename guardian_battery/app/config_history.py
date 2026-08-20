"""Append-only configuration provenance for Guardian diagnostics."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from version import DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION


CONFIG_HISTORY_SCHEMA_VERSION = 1

# Only settings that can affect acquisition, availability assessment, trend/
# incident behaviour or cell diagnostics belong in diagnostic provenance.
DIAGNOSTIC_OPTION_KEYS = (
    "poll_interval_seconds",
    "module_count",
    "warning_cell_delta_mv",
    "critical_cell_delta_mv",
    "warning_soc_deviation_pct",
    "critical_soc_deviation_pct",
    "missing_module_is_critical",
    "trend_window_minutes",
    "trend_min_change_mv",
    "incident_hold_minutes",
    "cell_diagnostics_enabled",
    "cell_diagnostics_interval_seconds",
    "cell_diag_low_soc_percent",
    "cell_diag_high_soc_percent",
    "cell_diag_charge_current_a",
    "cell_diag_discharge_current_a",
    "cell_diag_min_phase_samples",
    "cell_diag_confidence_medium_samples",
    "cell_diag_confidence_high_samples",
    "cell_diag_observe_deviation_mv",
    "cell_diag_warning_deviation_mv",
    "cell_diag_critical_deviation_mv",
    "cell_diag_history_max_samples",
    "bms_stat_interval_seconds",
    "cell_diag_discharge_observe_deviation_mv",
    "cell_diag_discharge_warning_deviation_mv",
    "cell_diag_discharge_critical_deviation_mv",
    "cell_diag_low_observe_deviation_mv",
    "cell_diag_low_warning_deviation_mv",
    "cell_diag_low_critical_deviation_mv",
    "cell_diag_charge_observe_deviation_mv",
    "cell_diag_charge_warning_deviation_mv",
    "cell_diag_charge_critical_deviation_mv",
    "cell_diag_high_observe_deviation_mv",
    "cell_diag_high_warning_deviation_mv",
    "cell_diag_high_critical_deviation_mv",
)


def diagnostic_parameters(options: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, diagnostically relevant subset of app options."""
    return {key: options.get(key) for key in DIAGNOSTIC_OPTION_KEYS if key in options}


def config_id(parameters: dict[str, Any]) -> str:
    """Create a deterministic ID for an effective diagnostic parameter set."""
    canonical = json.dumps(
        parameters,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


class ConfigHistory:
    """Append a record only when the effective diagnostic configuration changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_record(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            last = None
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        last = json.loads(line)
            return last
        except (OSError, json.JSONDecodeError):
            return None

    def records(self) -> list[dict[str, Any]]:
        """Read all immutable provenance records, failing closed on corruption."""
        if not self.path.exists():
            return []
        result = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if (not isinstance(value, dict) or value.get("schema_version") != CONFIG_HISTORY_SCHEMA_VERSION
                            or not isinstance(value.get("parameters"), dict)):
                        raise ValueError(f"invalid config history line {line_number}")
                    timestamp = datetime.fromisoformat(str(value["timestamp"]).replace("Z", "+00:00"))
                    if timestamp.tzinfo is None:
                        raise ValueError(f"invalid config history timestamp at line {line_number}")
                    value = dict(value)
                    value["timestamp"] = timestamp.astimezone(timezone.utc).isoformat()
                    result.append(value)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("configuration history is unavailable") from exc
        return sorted(result, key=lambda item: (item["timestamp"], item["config_id"]))

    def record_if_changed(self, options: dict[str, Any]) -> dict[str, Any] | None:
        parameters = diagnostic_parameters(options)
        current_id = config_id(parameters)
        last = self._last_record()
        if last and last.get("config_id") == current_id:
            return None

        record = {
            "schema_version": CONFIG_HISTORY_SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config_id": current_id,
            "guardian_version": GUARDIAN_VERSION,
            "diagnostic_engine_version": DIAGNOSTIC_ENGINE_VERSION,
            "parameters": parameters,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        return record
