import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from position_history import PositionHistoryLog, PositionSnapshot
from rs485_evidence import Rs485HistorySeries
from rs485_identity import project_current_management
from rs485_mqtt import Rs485MqttProjection


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


def test_management_timestamp_and_frame_provenance_win_over_default_resolver_datetime(
        tmp_path):
    positions = tmp_path / "positions.jsonl"
    serial = SERIALS[0]
    snapshot(positions, "2026-08-31T00:00:00+00:00", {1: serial})
    management_quality = {"decoder_supported": True, "source": "direct_0x92"}
    result = project_current_management(
        {2: {"adr": 2, "timestamp": 1725210000.0,
             "discharge_current_limit_a": -25.0,
             "charge_current_limit_a": 0.0,
             "discharge_enable": True, "charge_enable": False,
             "quality": management_quality, "raw_frame": b"raw-0x92"}},
        {2: {"serial_string": serial, "serial_raw": serial.encode().hex().upper(),
             "decode_source": "live_0x93", "identity_known": True,
             "identity_currently_confirmed": True}},
        position_history_path=positions,
    )[2]

    assert result["timestamp"] == 1725210000.0
    assert isinstance(result["timestamp"], (int, float))
    assert result["adr"] == 2
    assert result["quality"] is management_quality
    assert "raw_frame" not in result
    assert result["physical_serial"] == serial
    assert result["position"] == 1
    assert result["identity_resolved"] is True
    assert result["identity_source"] == "live_0x93"
    assert not any(isinstance(value, datetime) for value in result.values())
    json.dumps(result)


def test_real_management_projection_reaches_mqtt_json_and_history_confirmation(
        tmp_path, monkeypatch):
    import position_history

    positions = tmp_path / "positions.jsonl"
    serial = SERIALS[0]
    snapshot(positions, "2026-08-31T00:00:00+00:00", {1: serial})
    projected = project_current_management(
        {2: {"timestamp": 1725210000.0, "charge_current_limit_a": 0.0,
             "discharge_current_limit_a": -25.0, "charge_enable": False,
             "discharge_enable": True}},
        {2: {"serial_string": serial, "serial_raw": serial.encode().hex().upper(),
             "decode_source": "live_0x93", "identity_known": True,
             "identity_currently_confirmed": True}},
        position_history_path=positions,
    )

    class MqttCapture:
        prefix = "guardian"
        discovery_enabled = False

        def __init__(self):
            self.messages = []

        def _publish(self, topic, payload, retain):
            self.messages.append((topic, payload, retain))

        def state(self, name, value):
            self.messages.append((name + "/state", value, True))

        def attributes(self, name, value):
            self.messages.append((name + "/attributes", value, True))

    mqtt = MqttCapture()
    Rs485MqttProjection(
        mqtt, wall_clock=lambda: 1725210010.0, stale_seconds=600,
    ).publish({"state": "listening"}, projected)
    attributes = next(value for topic, value, _ in mqtt.messages
                      if topic == "rs485_adr_02_dcl/attributes")
    assert attributes["sample_age_seconds"] == 10.0
    assert attributes["management_freshness"] == "current"
    assert attributes["module_position"] == 1
    assert ("rs485_adr_02_dcl/state", -25.0, True) in mqtt.messages
    assert ("rs485_adr_02_charge_enable/state", "STOP REQUEST", True) in mqtt.messages
    assert ("rs485_adr_02_discharge_enable/state", "ENABLED", True) in mqtt.messages
    assert ("rs485_adr_02_last_update/state",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1725210000.0)),
            True) in mqtt.messages
    json.dumps({"status": {"state": "listening"}, "management": projected,
                "identities": {}, "history": {}})

    monkeypatch.setattr(position_history, "_HISTORY_READY", False)
    monkeypatch.setattr(position_history, "_PRESENCE_SOURCES", {})
    monkeypatch.setattr(position_history, "_OBSERVATION_CANDIDATES", {})
    monkeypatch.setattr(position_history, "_MISSING_CANDIDATES", {})
    monkeypatch.setattr(position_history, "_OBSERVED_STACK", {})
    position_history.update_observed_stack(
        {1: {"barcode": serial}}, present_positions={1},
        expected_module_count=1, observed_at=1725210010.0,
        confirm_history=True,
    )
    assert position_history.history_observation_ready() is True


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
