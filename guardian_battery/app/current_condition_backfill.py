"""Bounded, idempotent raw-history rebuild for classic Current Condition."""

from __future__ import annotations

import bisect
import json
import logging
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from cell_diagnostics import CellSample
from position_history import PositionHistoryLog


LOG = logging.getLogger("guardian_battery")
SOURCE_SCHEMA_VERSION = 2


class CurrentConditionBackfill:
    """Merge trustworthy raw samples into the existing bounded working cache."""

    def __init__(self, history_directory: Path, position_history_path: Path):
        self.history_directory = Path(history_directory)
        self.position_history_path = Path(position_history_path)

    @staticmethod
    def _signature(path: Path) -> dict:
        stat = path.stat()
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }

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
        serial = snapshot.positions.get(str(int(record["module"])))
        return serial, snapshot.position_history_id if serial else None

    @classmethod
    def _sample(cls, record, identity_index):
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
        balancing = record.get("balancing", [False] * 15)
        if not isinstance(balancing, list) or len(balancing) != 15:
            balancing = [False] * 15
        serial, snapshot_id = cls._resolve_identity(record, timestamp, identity_index)
        if not serial:
            return None
        return CellSample(
            timestamp, module, voltages, float(record["current_a"]),
            float(record["soc_percent"]), temperatures,
            [bool(value) for value in balancing], serial, snapshot_id,
        )

    @staticmethod
    def _sample_key(value):
        return (
            str(value.get("module_serial") or ""),
            int(value["module"]),
            float(value["timestamp"]),
        )

    def run(self, store):
        report = {
            "files_discovered": 0, "files_scanned": 0, "files_skipped": 0,
            "incremental_files": 0, "lines_seen": 0, "valid_samples": 0,
            "samples_merged": 0, "duplicates": 0, "identity_unknown": 0,
            "invalid_lines": 0, "file_errors": 0,
        }
        if not self.history_directory.exists():
            return report
        try:
            identity_index = self._identity_index()
            identity_signature = self._signature(self.position_history_path)
        except Exception:
            identity_index = ([], [])
            try:
                identity_signature = self._signature(self.position_history_path)
            except OSError:
                identity_signature = {"missing": True}
            report["position_history_unavailable"] = True

        merged = {}
        slot_keys = {}
        for value in store.iter_all_samples():
            try:
                sample_key = self._sample_key(value)
                merged[sample_key] = dict(value)
                slot_keys.setdefault(sample_key[1:], set()).add(sample_key)
            except (KeyError, TypeError, ValueError):
                continue

        sources = store.rebuild_sources if isinstance(store.rebuild_sources, dict) else {}
        if not store.materialized_coverage_satisfied():
            sources = {}
            report["materialized_cache_incomplete"] = True
        next_sources = dict(sources)
        paths = sorted(self.history_directory.glob("*.jsonl"))
        report["files_discovered"] = len(paths)
        for path in paths:
            key = path.name
            try:
                signature = self._signature(path)
            except OSError:
                report["file_errors"] += 1
                continue
            previous = sources.get(key, {})
            same_identity = previous.get("position_history") == identity_signature
            previous_file = previous.get("file", {})
            if (same_identity and previous_file == signature
                    and previous.get("schema_version") == SOURCE_SCHEMA_VERSION):
                report["files_skipped"] += 1
                continue
            offset = 0
            previous_offset = previous.get("offset", -1)
            previous_size = previous_file.get("size", -1)
            if (same_identity and previous.get("schema_version") == SOURCE_SCHEMA_VERSION
                    and previous_file.get("device") == signature["device"]
                    and previous_file.get("inode") == signature["inode"]
                    and isinstance(previous_offset, int) and isinstance(previous_size, int)
                    and 0 <= previous_offset <= previous_size
                    and signature["size"] > previous_size):
                offset = previous_offset
                report["incremental_files"] += 1
            try:
                with path.open(encoding="utf-8") as handle:
                    handle.seek(offset)
                    report["files_scanned"] += 1
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        report["lines_seen"] += 1
                        try:
                            record = json.loads(line)
                            sample = self._sample(record, identity_index)
                        except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
                            report["invalid_lines"] += 1
                            LOG.warning("Ungültige Cell-History-Zeile %s ab Offset %s: %s",
                                        path, offset, exc)
                            continue
                        if sample is None:
                            report["identity_unknown"] += 1
                            continue
                        report["valid_samples"] += 1
                        value = asdict(sample)
                        sample_key = self._sample_key(value)
                        existed = sample_key in merged
                        slot = sample_key[1:]
                        # A valid raw record is authoritative for this module/time
                        # slot. This also removes a formerly unresolved cache copy
                        # after position history later establishes its identity.
                        for old_key in slot_keys.get(slot, set()):
                            merged.pop(old_key, None)
                        slot_keys[slot] = {sample_key}
                        if existed:
                            report["duplicates"] += 1
                        else:
                            report["samples_merged"] += 1
                        merged[sample_key] = value
                    final_offset = handle.tell()
            except OSError:
                report["file_errors"] += 1
                continue
            next_sources[key] = {
                "schema_version": SOURCE_SCHEMA_VERSION,
                "file": signature,
                "offset": final_offset,
                "position_history": identity_signature,
            }

        store.replace_samples(merged.values())
        if identity_index[1]:
            store.set_current_identities(identity_index[1][-1].positions)
        store.rebuild_sources = next_sources
        if report["files_scanned"] or store.load_error:
            store.expected_materialized_coverage = store.coverage_snapshot()
            store.save()
            store.load_error = False
        return report
