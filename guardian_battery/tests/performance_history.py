"""Reproducible 1/3/7/30-day Guardian history benchmark (not a unit test)."""
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from config_history import ConfigHistory
from history_series import CellHistorySeries
from phase_engine import PhaseEngine


def main():
    root = Path(tempfile.mkdtemp(prefix="guardian-history-performance-"))
    history = root / "cell_history"; history.mkdir()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = {"schema_version": 1, "module": 1,
              "voltages_mv": [3300 + index for index in range(15)],
              "current_a": -2.0, "soc_percent": 55.0,
              "temperatures_c": [24.0] * 15, "balancing": [False] * 15,
              "physical_groups": {}}
    handles = {}
    for minute in range(30 * 24 * 60):
        now = start + timedelta(minutes=minute)
        day = now.date().isoformat()
        handle = handles.get(day)
        if handle is None:
            handle = (history / f"{day}.jsonl").open("w")
            handles[day] = handle
        record["timestamp"] = now.timestamp()
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    for handle in handles.values(): handle.close()
    parameters = {"cell_diag_low_soc_percent": 30, "cell_diag_high_soc_percent": 80,
                  "cell_diag_charge_current_a": .8, "cell_diag_discharge_current_a": .8}
    config = root / "config.jsonl"
    config.write_text(json.dumps({"schema_version": 1, "timestamp": start.isoformat(),
                                  "config_id": "benchmark", "parameters": parameters}) + "\n")
    series = CellHistorySeries(history); engine = PhaseEngine(ConfigHistory(config), lambda: parameters)
    for days in (1, 3, 7, 30):
        end = start + timedelta(days=days) - timedelta(seconds=1)
        began = time.perf_counter()
        bundle = series.query_bundle(metric="soc", timestamp_from=start.isoformat(),
                                     timestamp_to=end.isoformat(), module_number=1)
        analysis = engine.analyse(bundle["samples"], mode="historical", window_to=end.isoformat())
        elapsed = time.perf_counter() - began
        payload = json.dumps({"points": bundle["points"],
                              "phases": analysis["visual_intervals"]}, separators=(",", ":"))
        print(json.dumps({"days": days, "raw_records": bundle["raw_records"],
                          "raw_points": bundle["raw_points"], "display_points": len(bundle["points"]),
                          "history_seconds": round(bundle["read_seconds"], 4),
                          "downsample_seconds": round(bundle["downsample_seconds"], 4),
                          "total_seconds": round(elapsed, 4), "payload_bytes": len(payload)}))


if __name__ == "__main__": main()
