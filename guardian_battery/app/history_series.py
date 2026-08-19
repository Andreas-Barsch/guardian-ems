"""Guardian-owned time-series reads combined with shared event overlays."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CELL_HISTORY_DIR = Path("/share/guardian_battery/cell_history")
SERIES_METRICS = frozenset({"soc", "current", "cell_voltage", "cell_temperature"})


class SeriesHistoryError(RuntimeError):
    pass


class CellHistorySeries:
    def __init__(self, directory: Path | str = DEFAULT_CELL_HISTORY_DIR):
        self.directory = Path(directory)

    def query(self, *, metric: str, timestamp_from: str, timestamp_to: str,
              module_number: int, cell_number: int | None = None) -> list[dict[str, Any]]:
        if metric not in SERIES_METRICS:
            raise ValueError("unsupported series metric")
        if metric.startswith("cell_") and cell_number is None:
            raise ValueError("cell_number is required for cell metrics")
        start = datetime.fromisoformat(timestamp_from)
        end = datetime.fromisoformat(timestamp_to)
        start_day = start.astimezone(timezone.utc).date().isoformat()
        end_day = end.astimezone(timezone.utc).date().isoformat()
        if not self.directory.exists():
            return []
        try:
            paths = sorted(path for path in self.directory.glob("*.jsonl")
                           if start_day <= path.stem <= end_day)
        except OSError as exc:
            raise SeriesHistoryError("cell history is unavailable") from exc
        points = []
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise SeriesHistoryError("cell history is unavailable") from exc
            for line_number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    point = self._point(record, metric, module_number, cell_number)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
                    raise SeriesHistoryError(
                        f"cell history {path.name} line {line_number} is invalid"
                    ) from exc
                if point and timestamp_from <= point["timestamp"] <= timestamp_to:
                    points.append(point)
        return sorted(points, key=lambda point: point["timestamp"])

    @staticmethod
    def _point(record: Any, metric: str, module_number: int,
               cell_number: int | None) -> dict[str, Any] | None:
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise ValueError("unsupported cell history schema")
        if int(record["module"]) != module_number:
            return None
        raw_timestamp = record["timestamp"]
        if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, (int, float)):
            raise ValueError("timestamp must be numeric")
        timestamp = datetime.fromtimestamp(raw_timestamp, timezone.utc).isoformat()
        if metric == "soc":
            value = float(record["soc_percent"])
        elif metric == "current":
            value = float(record["current_a"])
        elif metric == "cell_voltage":
            value = float(record["voltages_mv"][cell_number - 1])
        else:
            value = float(record["temperatures_c"][cell_number - 1])
        return {"timestamp": timestamp, "value": value}
