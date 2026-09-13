"""Time-valid identity projection for the Guardian Research API."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from maintenance import normalize_utc_timestamp
from position_history import PositionHistoryLog


class ResearchIdentityResolver:
    """Resolve identities solely from authoritative position snapshots."""

    def __init__(self, snapshots):
        self.snapshots = sorted(snapshots, key=lambda item: (
            item.effective_at, item.created_at, item.position_history_id))

    @classmethod
    def from_path(cls, path: Path | str):
        return cls(PositionHistoryLog(path).read_all())

    @staticmethod
    def _epoch_id(serial, valid_from, position):
        value = f"{serial}|{valid_from}|{position}"
        return "IEP-" + hashlib.sha256(value.encode()).hexdigest()[:24]

    def topology_at(self, timestamp):
        target = normalize_utc_timestamp(timestamp, "timestamp")
        matches = [item for item in self.snapshots if item.effective_at <= target]
        snapshot = matches[-1] if matches else None
        positions = []
        for position in range(1, 7):
            serial = snapshot.positions[str(position)] if snapshot else None
            epoch = self.epoch_at(serial, target) if serial else None
            positions.append({"position": position, "physical_serial": serial,
                "resolved": serial is not None,
                "position_history_id": snapshot.position_history_id if snapshot else None,
                "identity_epoch_id": epoch.get("identity_epoch_id") if epoch else None})
        return {"timestamp": target, "position_history_id":
                snapshot.position_history_id if snapshot else None, "positions": positions}

    def position_at(self, physical_serial, timestamp):
        topology = self.topology_at(timestamp)
        matches = [item for item in topology["positions"]
                   if item["physical_serial"] == physical_serial]
        if len(matches) != 1:
            return {"physical_serial": physical_serial, "position_at_time": None,
                "resolved": False, "position_history_id": topology["position_history_id"],
                "identity_epoch_id": None}
        item = matches[0]
        return {"physical_serial": physical_serial, "position_at_time": item["position"],
            "resolved": True, "position_history_id": item["position_history_id"],
            "identity_epoch_id": item["identity_epoch_id"]}

    def serial_at(self, position, timestamp):
        if not isinstance(position, int) or not 1 <= position <= 6:
            raise ValueError("position must be between 1 and 6")
        item = self.topology_at(timestamp)["positions"][position - 1]
        return {**item, "position_at_time": position}

    def epochs(self, physical_serial=None, timestamp_from=None, timestamp_to=None):
        serials = sorted({serial for snapshot in self.snapshots
                          for serial in snapshot.positions.values() if serial and
                          (physical_serial is None or serial == physical_serial)})
        result = []
        for serial in serials:
            current = None
            first_known = next(index for index, snapshot in enumerate(self.snapshots)
                               if serial in snapshot.positions.values())
            for index in range(first_known, len(self.snapshots)):
                snapshot = self.snapshots[index]
                position = next((int(key) for key, value in snapshot.positions.items()
                                 if value == serial), None)
                valid_to = (self.snapshots[index + 1].effective_at
                            if index + 1 < len(self.snapshots) else None)
                if current and current["position"] == position:
                    current["valid_to"] = valid_to
                    continue
                current = {"physical_serial": serial, "position": position,
                    "valid_from": snapshot.effective_at, "valid_to": valid_to,
                    "position_history_id": snapshot.position_history_id,
                    "identity_epoch_id": self._epoch_id(
                        serial, snapshot.effective_at, position)}
                result.append(current)
        if timestamp_from:
            start = datetime.fromisoformat(normalize_utc_timestamp(timestamp_from, "from"))
            result = [item for item in result if item["valid_to"] is None or
                      datetime.fromisoformat(item["valid_to"]) >= start]
        if timestamp_to:
            end = datetime.fromisoformat(normalize_utc_timestamp(timestamp_to, "to"))
            result = [item for item in result
                      if datetime.fromisoformat(item["valid_from"]) <= end]
        return result

    def epoch_at(self, physical_serial, timestamp):
        target = datetime.fromisoformat(normalize_utc_timestamp(timestamp, "timestamp"))
        for item in self.epochs(physical_serial):
            if datetime.fromisoformat(item["valid_from"]) <= target and (
                    item["valid_to"] is None or target < datetime.fromisoformat(item["valid_to"])):
                return item
        return None
