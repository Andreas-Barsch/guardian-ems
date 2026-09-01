import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from position_history import PositionHistoryLog, PositionSnapshot
from rs485_evidence import Rs485HistorySeries
from rs485_identity import project_current_management


SERIALS = ["H221005E22212581", "H221005E22212536", "H221005E22212571",
           "H221005E22212538", "Y225004C32250226"]


def snapshot(path, effective_at, positions):
    item = PositionSnapshot(
        schema_version=1, position_history_id="PHS-" + str(uuid.uuid4()),
        effective_at=effective_at, created_at=effective_at,
        maintenance_event_id="MEV-" + str(uuid.uuid4()),
        positions={str(position): positions.get(position) for position in range(1, 7)},
    )
    PositionHistoryLog(path).append(item)
    return item


def test_real_serials_resolve_through_position_history_not_adr_arithmetic(tmp_path):
    positions = tmp_path / "positions.jsonl"
    snapshot(positions, "2026-08-31T00:00:00+00:00",
             {index: serial for index, serial in enumerate(SERIALS, 1)})
    management = {adr: {"timestamp": 100, "discharge_current_limit_a": -25.0}
                  for adr in (6, 2, 5, 3, 4)}
    identities = {adr: {"serial_string": serial, "serial_raw": serial.encode().hex().upper(),
                        "decode_source": "stored_decoded"}
                  for adr, serial in zip((2, 3, 4, 5, 6), SERIALS)}
    result = project_current_management(
        management, identities, position_history_path=positions,
        at="2026-08-31T12:00:00+00:00")
    assert [(adr, result[adr]["position"], result[adr]["physical_serial"])
            for adr in (2, 3, 4, 5, 6)] == [
        (2, 1, SERIALS[0]), (3, 2, SERIALS[1]), (4, 3, SERIALS[2]),
        (5, 4, SERIALS[3]), (6, 5, SERIALS[4])]


def test_unknown_serial_and_missing_position_remain_unresolved(tmp_path):
    positions = tmp_path / "positions.jsonl"
    snapshot(positions, "2026-08-31T00:00:00+00:00", {1: "KNOWN"})
    result = project_current_management(
        {6: {"timestamp": 1}, 7: {"timestamp": 1}},
        {6: {"serial_string": "UNKNOWN", "serial_raw": "00",
             "decode_source": "stored_decoded"}},
        position_history_path=positions, at="2026-08-31T12:00:00+00:00")
    assert result[6]["serial_string"] == "UNKNOWN" and not result[6]["identity_resolved"]
    assert result[7]["serial_string"] is None and not result[7]["identity_resolved"]
    assert result[6]["identity_known"] is True
    assert result[6]["identity_currently_confirmed"] is False
    assert result[7]["identity_known"] is False


def write_record(directory, timestamp, adr, command, decoded, info_raw=""):
    directory.mkdir(exist_ok=True)
    day = timestamp[:10]
    record = {"record_type": "frame", "timestamp": timestamp, "adr": adr,
              "direction": "response", "paired_command": command,
              "checksum_valid": True, "frame_complete": True, "request_matched": True,
              "info_raw": info_raw, "decoded": decoded}
    with (directory / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def test_history_uses_position_effective_at_each_management_sample(tmp_path):
    positions = tmp_path / "positions.jsonl"
    snapshot(positions, "2026-08-31T00:00:00+00:00", {1: "SERIAL-123456789"})
    snapshot(positions, "2026-08-31T12:00:00+00:00", {3: "SERIAL-123456789"})
    history = tmp_path / "rs485"
    serial = "SERIAL-123456789"
    write_record(history, "2026-08-31T01:00:00+00:00", 2, 0x93, None,
                 "02" + serial.encode().hex().upper())
    write_record(history, "2026-08-31T02:00:00+00:00", 2, 0x92,
                 {"discharge_current_limit_a": -25.0})
    write_record(history, "2026-08-31T13:00:00+00:00", 2, 0x92,
                 {"discharge_current_limit_a": 0.0})
    series = Rs485HistorySeries(history, position_history_path=positions)
    args = dict(requests=[{"metric": "rs485_dcl"}],
                timestamp_from="2026-08-31T00:00:00+00:00",
                timestamp_to="2026-09-01T00:00:00+00:00")
    first = series.query_bundles(**args, module_number=1)[0]
    third = series.query_bundles(**args, module_number=3)[0]
    assert [point["value"] for point in first["points"]] == [-25.0]
    assert [point["value"] for point in third["points"]] == [0.0]
    assert all(point["physical_serial"] == serial for point in first["points"] + third["points"])
    assert all(point["identity_decode_source"] == "historical_raw_redecode"
               for point in first["points"] + third["points"])
