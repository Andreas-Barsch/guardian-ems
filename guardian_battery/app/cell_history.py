from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def cell_history_timing(cell_sample_at: float, pwr_sample_at: float | None) -> dict:
    """Describe the non-simultaneous PWR context without negative ages."""
    if pwr_sample_at is None:
        return {"cell_sample_at": float(cell_sample_at), "pwr_sample_at": None,
                "pwr_age_seconds": None, "pwr_age_quality": "unavailable"}
    age = float(cell_sample_at) - float(pwr_sample_at)
    return {"cell_sample_at": float(cell_sample_at),
            "pwr_sample_at": float(pwr_sample_at),
            "pwr_age_seconds": age if age >= 0 else None,
            "pwr_age_quality": "observed" if age >= 0 else "invalid_future"}


class CellHistoryWriter:
    """Append-only daily JSONL history for raw cell diagnostic samples."""

    SCHEMA_VERSION = 1

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def append(self, sample: Any) -> None:
        data = asdict(sample) if is_dataclass(sample) else dict(sample)
        timestamp = float(data["timestamp"])
        day = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        path = self.directory / f"{day}.jsonl"
        module = int(data["module"])
        record = {
            "schema_version": self.SCHEMA_VERSION,
            "physical_groups": {"1": [1, 2, 3, 4, 5], "2": [6, 7, 8, 9, 10], "3": [11, 12, 13, 14, 15]},
            **data,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")
