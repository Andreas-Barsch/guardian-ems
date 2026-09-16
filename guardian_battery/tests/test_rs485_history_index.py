import json
import time
from datetime import datetime

import pytest
from types import SimpleNamespace

from history_block_index import BlockIndexError, index_path
# Keep imports explicit so this test also verifies the public module contract.
from rs485_history_index import (extend, load_valid, rebuild, select_ranges)
from research_api import GuardianResearchApi


SERIAL_A = "SERIAL-A        "
SERIAL_B = "SERIAL-B        "


def identity(timestamp, serial=SERIAL_A, adr=2):
    return {"record_type": "frame", "timestamp": timestamp, "adr": adr,
        "direction": "response", "paired_command": 0x93, "checksum_valid": True,
        "frame_complete": True, "request_matched": True,
        "info_raw": f"{adr:02x}" + serial.encode().hex(),
        "decoder_supported": False, "decoded": None}


def management(timestamp, *, adr=2, physical_serial=None, dcl=-25.0):
    return {"record_type": "frame", "timestamp": timestamp, "adr": adr,
        "direction": "response", "paired_command": 0x92, "checksum_valid": True,
        "frame_complete": True, "request_matched": True,
        "physical_serial": physical_serial,
        "decoded": {"discharge_current_limit_a": dcl,
                    "charge_current_limit_a": 25.0,
                    "charge_enable": True, "discharge_enable": True}}


def command(timestamp, paired_command, decoded, *, adr=2):
    return {"record_type": "frame", "timestamp": timestamp, "adr": adr,
        "direction": "response", "paired_command": paired_command,
        "checksum_valid": True, "frame_complete": True, "request_matched": True,
        "decoded": decoded}


def write(path, rows, suffix=b""):
    path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows) + suffix)


def test_index_contract_checkpoint_and_selected_ranges(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    rows = [identity("2026-09-11T00:00:00+00:00"),
            management("2026-09-11T00:01:00+00:00"),
            management("2026-09-11T12:00:00+00:00"),
            management("2026-09-11T12:01:00+00:00")]
    write(source, rows)
    raw = source.read_bytes()
    value = rebuild(source, block_records=2)

    assert value["complete"] is True and value["indexed_size"] == len(raw)
    assert value["blocks"][0]["identity_checkpoint"] == {}
    assert value["blocks"][1]["identity_checkpoint"]["2"][
        "physical_serial"] == SERIAL_A
    noon = datetime.fromisoformat("2026-09-11T12:00:00+00:00").timestamp()
    ranges, profile = select_ranges(source, noon, noon + 120)
    assert len(ranges) == 1 and ranges[0]["identity_checkpoint"]["2"][
        "physical_serial"] == SERIAL_A
    assert profile["selected_blocks"] == 1
    assert source.read_bytes() == raw


def test_out_of_order_blocks_are_selected_by_min_max_not_source_order(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    rows = [identity("2026-09-11T00:00:00+00:00"),
            management("2026-09-11T12:00:00+00:00"),
            management("2026-09-11T01:00:00+00:00"),
            management("2026-09-11T13:00:00+00:00")]
    write(source, rows)
    rebuild(source, block_records=2)
    start = datetime.fromisoformat("2026-09-11T01:00:00+00:00").timestamp()
    ranges, _ = select_ranges(source, start, start + 60)
    # The first block conservatively overlaps because its min/max spans noon;
    # critically, the later out-of-order block is selected as well.
    assert len(ranges) == 2
    assert ranges[1]["identity_checkpoint"]["2"]["physical_serial"] == SERIAL_A


def test_open_day_indexes_only_complete_blocks_and_reads_bounded_suffix(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    rows = [identity("2026-09-11T00:00:00+00:00"),
            management("2026-09-11T00:01:00+00:00"),
            management("2026-09-11T00:02:00+00:00")]
    write(source, rows, b'{"partial"')
    raw = source.read_bytes()
    value = extend(source, block_records=2, max_blocks=10, close_source=False)
    assert len(value["blocks"]) == 1 and value["complete"] is False
    ranges, profile = select_ranges(source, 0, 4_000_000_000)
    assert len(ranges) == 2
    assert profile["open_suffix_bytes"] > 0
    assert source.read_bytes() == raw


def test_append_extends_without_changing_confirmed_block(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    write(source, [identity("2026-09-11T00:00:00+00:00"),
                   management("2026-09-11T00:01:00+00:00")])
    first = extend(source, block_records=2)
    confirmed = dict(first["blocks"][0])
    with source.open("a") as handle:
        handle.write(json.dumps(management("2026-09-11T00:02:00+00:00")) + "\n")
        handle.write(json.dumps(management("2026-09-11T00:03:00+00:00")) + "\n")
    second = extend(source, block_records=2)
    assert second["blocks"][0] == confirmed
    assert len(second["blocks"]) == 2


def test_serial_change_and_multiple_adrs_are_checkpointed(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    write(source, [identity("2026-09-11T00:00:00+00:00", SERIAL_A, 2),
                   identity("2026-09-11T00:00:01+00:00", SERIAL_A, 3),
                   identity("2026-09-11T00:00:02+00:00", SERIAL_B, 2),
                   management("2026-09-11T00:00:03+00:00", adr=2)])
    value = rebuild(source, block_records=2)
    checkpoint = value["blocks"][1]["identity_checkpoint"]
    assert checkpoint["2"]["physical_serial"] == SERIAL_A
    assert checkpoint["3"]["physical_serial"] == SERIAL_A
    # The following block advances ADR 2 to SERIAL_B for later blocks.
    with source.open("a") as handle:
        handle.write(json.dumps(management("2026-09-11T00:00:04+00:00")) + "\n")
        handle.write(json.dumps(management("2026-09-11T00:00:05+00:00")) + "\n")
    index_path(source).unlink()
    value = rebuild(source, block_records=2)
    assert value["blocks"][2]["identity_checkpoint"]["2"][
        "physical_serial"] == SERIAL_B


@pytest.mark.parametrize("mutation", ["replace", "truncate", "corrupt_index"])
def test_replaced_truncated_or_corrupt_source_invalidates_index(tmp_path, mutation):
    source = tmp_path / "2026-09-11.jsonl"
    write(source, [identity("2026-09-11T00:00:00+00:00"),
                   management("2026-09-11T00:01:00+00:00")])
    rebuild(source, block_records=1)
    if mutation == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(source.read_bytes())
        source.unlink(); replacement.rename(source)
    elif mutation == "truncate":
        source.write_bytes(source.read_bytes()[:10])
    else:
        index_path(source).write_text("{broken")
    with pytest.raises(BlockIndexError):
        load_valid(source)


def test_rebuild_is_deterministic_and_explicit_serial_does_not_override_0x93(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    write(source, [identity("2026-09-11T00:00:00+00:00"),
                   management("2026-09-11T00:01:00+00:00",
                              physical_serial=SERIAL_B),
                   management("2026-09-11T00:02:00+00:00")])
    first = rebuild(source, block_records=1)
    first.pop("source_mtime_ns")
    second = rebuild(source, block_records=1)
    second.pop("source_mtime_ns")
    assert first == second
    assert second["blocks"][2]["identity_checkpoint"]["2"][
        "physical_serial"] == SERIAL_A


def test_indexed_core_matches_bounded_full_scan_for_management_0x44_and_0x47(
        tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    rows = [identity("2026-09-11T00:00:00+00:00"),
            management("2026-09-11T00:01:00+00:00"),
            management("2026-09-11T10:00:00+00:00", dcl=0),
            command("2026-09-11T10:01:00+00:00", 0x44, {"state": 7}),
            command("2026-09-11T10:02:00+00:00", 0x47,
                    {"cell_low_voltage_alarm_limit_v": 3.0}),
            management("2026-09-11T20:00:00+00:00")]
    write(source, rows)
    api = GuardianResearchApi.__new__(GuardianResearchApi)
    api.paths = SimpleNamespace(rs485_history=tmp_path)
    deadline = time.monotonic() + 10
    profile = {}
    rebuild(source, block_records=2)
    indexed = api._rs485_core_context(
        SERIAL_A, "2026-09-11T09:50:00+00:00",
        "2026-09-11T10:30:00+00:00", deadline, profile)
    raw = source.read_bytes()
    index_path(source).unlink()
    fallback = api._rs485_core_context(
        SERIAL_A, "2026-09-11T09:50:00+00:00",
        "2026-09-11T10:30:00+00:00", time.monotonic() + 10, {})

    assert indexed == fallback
    assert [row["paired_command"] for row in indexed["management"]["records"]
            ] == [0x92, 0x44]
    assert [row["paired_command"] for row in indexed["low_voltage"]["records"]
            ] == [0x44, 0x47]
    assert profile["read_mode"] == "rs485_block_index"
    assert profile["identity_checkpoint_used"] is True
    assert profile["selected_blocks"] == 2
    assert profile["raw_bytes_read"] < len(raw)
    assert source.read_bytes() == raw


def test_missing_large_index_is_unavailable_without_full_scan(tmp_path, monkeypatch):
    source = tmp_path / "2026-09-11.jsonl"
    write(source, [identity("2026-09-11T00:00:00+00:00"),
                   management("2026-09-11T10:00:00+00:00")])
    monkeypatch.setattr("research_api.OPEN_SUFFIX_MAX_BYTES", 1)
    api = GuardianResearchApi.__new__(GuardianResearchApi)
    api.paths = SimpleNamespace(rs485_history=tmp_path)
    profile = {}
    result = api._rs485_core_context(
        SERIAL_A, "2026-09-11T09:50:00+00:00",
        "2026-09-11T10:30:00+00:00", time.monotonic() + 10, profile)
    assert result["management"]["quality"] == "unavailable"
    assert result["low_voltage"]["quality"] == "unavailable"
    assert profile["read_mode"] == "index_unavailable"
    assert profile["records_inspected"] == 0


def test_core_profile_reports_only_non_sensitive_index_metadata(tmp_path):
    source = tmp_path / "2026-09-11.jsonl"
    write(source, [identity("2026-09-11T00:00:00+00:00"),
                   management("2026-09-11T10:00:00+00:00")])
    rebuild(source, block_records=1)
    api = GuardianResearchApi.__new__(GuardianResearchApi)
    api.paths = SimpleNamespace(rs485_history=tmp_path)
    profile, io_profile = api._core_profile(), {}
    deadline = time.monotonic() + 10
    api._run_package_stage(profile, "rs485_core_context", deadline,
        lambda: api._rs485_core_context(
            SERIAL_A, "2026-09-11T09:50:00+00:00",
            "2026-09-11T10:30:00+00:00", deadline, io_profile),
        io_profile=io_profile)
    stage = profile["stages"]["rs485_core_context"]
    assert {"index_present", "index_valid", "selected_blocks", "selected_bytes",
            "raw_bytes_read", "records_inspected", "identity_checkpoint_used",
            "open_suffix_bytes", "read_mode", "elapsed_seconds"} <= set(stage)
    encoded = json.dumps(profile)
    assert SERIAL_A not in encoded
    assert "physical_serial" not in encoded
    assert '"identity_checkpoint":' not in encoded


def test_core_crosses_utc_midnight_with_identity_from_previous_file(tmp_path):
    first = tmp_path / "2026-09-11.jsonl"
    second = tmp_path / "2026-09-12.jsonl"
    write(first, [identity("2026-09-11T23:50:00+00:00"),
                  management("2026-09-11T23:59:00+00:00")])
    write(second, [management("2026-09-12T00:01:00+00:00"),
                   management("2026-09-12T01:00:00+00:00")])
    rebuild(first, block_records=1)
    rebuild(second, block_records=1)
    api = GuardianResearchApi.__new__(GuardianResearchApi)
    api.paths = SimpleNamespace(rs485_history=tmp_path)
    result = api._rs485_core_context(
        SERIAL_A, "2026-09-11T23:55:00+00:00",
        "2026-09-12T00:05:00+00:00", time.monotonic() + 10, {})
    assert [row["timestamp"] for row in result["management"]["records"]] == [
        "2026-09-11T23:59:00+00:00", "2026-09-12T00:01:00+00:00"]
