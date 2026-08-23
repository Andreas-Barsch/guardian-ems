"""Single-pass, idempotent backfill from append-only cell history."""

from __future__ import annotations

import bisect
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from cell_diagnostics import CellSample
from config_history import config_id, diagnostic_parameters
from position_history import PositionHistoryLog


class DiagnosticAggregateBackfill:
    """Add only missing aggregate samples; raw JSONL files are read-only."""

    def __init__(self, history_directory: Path, position_history_path: Path):
        self.history_directory = Path(history_directory)
        self.position_history_path = Path(position_history_path)

    def _identity_index(self):
        snapshots = sorted(
            PositionHistoryLog(self.position_history_path).read_all(),
            key=lambda item: (item.effective_at, item.created_at, item.position_history_id),
        )
        return [item.effective_at for item in snapshots], snapshots

    @staticmethod
    def _resolve_identity(record, timestamp, identity_index):
        serial = record.get("module_serial")
        if isinstance(serial, str) and serial.strip():
            return serial.strip(), record.get("position_history_id")
        effective, snapshots = identity_index
        iso = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        index = bisect.bisect_right(effective, iso) - 1
        if index < 0:
            return None, None
        snapshot = snapshots[index]
        return snapshot.positions.get(str(int(record["module"]))), snapshot.position_history_id

    @staticmethod
    def _sample(record, identity_index):
        if record.get("schema_version") != 1:
            raise ValueError("unsupported cell-history schema")
        timestamp = float(record["timestamp"])
        module = int(record["module"])
        voltages = [float(value) for value in record["voltages_mv"]]
        temperatures = [float(value) for value in record["temperatures_c"]]
        if module not in range(1, 7) or len(voltages) != 15 or len(temperatures) != 15:
            raise ValueError("invalid module or cell count")
        numeric = [timestamp, float(record["current_a"]), float(record["soc_percent"]),
                   *voltages, *temperatures]
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("non-finite cell-history value")
        serial, snapshot_id = DiagnosticAggregateBackfill._resolve_identity(
            record, timestamp, identity_index
        )
        if not serial:
            return None
        balancing = record.get("balancing", [False] * 15)
        if not isinstance(balancing, list) or len(balancing) != 15:
            balancing = [False] * 15
        return CellSample(timestamp, module, voltages, float(record["current_a"]),
                          float(record["soc_percent"]), temperatures,
                          [bool(value) for value in balancing], serial, snapshot_id)

    def run(self, aggregate_store, options):
        report = {"files_discovered": 0, "files_scanned": 0, "files_skipped": 0,
                  "lines_seen": 0, "valid_samples": 0, "aggregated_samples": 0,
                  "identity_unknown": 0, "invalid_lines": 0, "file_errors": 0}
        if not self.history_directory.exists():
            return report
        try:
            identity_index = self._identity_index()
        except Exception:
            # Explicit serials in raw samples remain usable. Missing identities
            # fail closed when position history itself is unavailable.
            identity_index = ([], [])
            report["position_history_unavailable"] = True
        try:
            identity_signature = aggregate_store.source_signature(self.position_history_path)
        except OSError:
            identity_signature = {"missing": True}
        current_config_id = config_id(diagnostic_parameters(options))
        paths = sorted(self.history_directory.glob("*.jsonl"))
        report["files_discovered"] = len(paths)
        for path in paths:
            try:
                signature = {
                    "cell_history": aggregate_store.source_signature(path),
                    "position_history": identity_signature,
                }
            except OSError:
                report["file_errors"] += 1
                continue
            if aggregate_store.source_complete(path, signature, current_config_id):
                report["files_skipped"] += 1
                continue
            file_stats = {"lines_seen": 0, "valid_samples": 0,
                          "aggregated_samples": 0, "identity_unknown": 0,
                          "invalid_lines": 0}
            temporary = aggregate_store.in_memory(
                aggregate_store.phase_classifier, aggregate_store.retention_days
            )
            try:
                with path.open(encoding="utf-8") as handle:
                    report["files_scanned"] += 1
                    for line in handle:
                        if not line.strip():
                            continue
                        report["lines_seen"] += 1
                        file_stats["lines_seen"] += 1
                        try:
                            record = json.loads(line)
                            sample = self._sample(record, identity_index)
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError):
                            report["invalid_lines"] += 1
                            file_stats["invalid_lines"] += 1
                            continue
                        if sample is None:
                            report["identity_unknown"] += 1
                            file_stats["identity_unknown"] += 1
                            continue
                        report["valid_samples"] += 1
                        file_stats["valid_samples"] += 1
                        if temporary.add(sample, options):
                            report["aggregated_samples"] += 1
                            file_stats["aggregated_samples"] += 1
            except OSError:
                report["file_errors"] += 1
                continue
            file_stats["records_updated"] = aggregate_store.merge_backfill_records(
                temporary.records
            )
            aggregate_store.mark_source_complete(
                path, signature, current_config_id, set(temporary.records), file_stats
            )
            # Records and completion marker share one atomic file replacement.
            aggregate_store.save()
        valid_days = [path.stem for path in paths
                      if len(path.stem) == 10 and path.stem[4] == "-" and path.stem[7] == "-"]
        if valid_days:
            aggregate_store.prune_through(max(valid_days))
            aggregate_store.save()
        return report
