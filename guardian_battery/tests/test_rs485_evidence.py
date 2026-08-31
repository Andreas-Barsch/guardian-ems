import ast
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from rs485_evidence import Rs485EvidencePipeline, Rs485EvidenceWriter, Rs485HistorySeries
from rs485_sniffer import (Correlation, ResponseCorrelator, calculate_checksum,
                           calculate_lchksum, parse_frame)


def frame(*, adr=2, code=0x92, info=b""):
    info_ascii = info.hex().upper().encode()
    length = len(info_ascii)
    length_field = (calculate_lchksum(length) << 12) | length
    payload = f"20{adr:02X}46{code:02X}{length_field:04X}".encode() + info_ascii
    return parse_frame(b"~" + payload + f"{calculate_checksum(payload):04X}".encode() + b"\r")


def management(status=0x40, ccl=0, dcl=-250):
    dcl_raw = dcl & 0xFFFF
    return bytes([0, 0xCF, 0x08, 0xB5, 0xB8, ccl >> 8, ccl & 255,
                  dcl_raw >> 8, dcl_raw & 255, status])


def pair(info=None, now=1.0):
    correlator = ResponseCorrelator()
    request = frame(code=0x92)
    correlator.observe(request, now)
    response = frame(code=0, info=info or management())
    return request, correlator.observe(response, now + 0.1)


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_append_only_rotation_full_0x92_and_no_initial_fake_change(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, batch_size=1, flush_interval_seconds=.01)
    writer.start()
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: 1_786_147_200.0)
    request, response = pair()
    pipeline(request, Correlation(request, 0x92, False, 1.0))
    pipeline(response.frame, response)
    writer.stop()
    records = read_records(next(tmp_path.glob("*.jsonl")))
    assert [record["record_type"] for record in records] == ["frame", "frame"]
    decoded = records[-1]["decoded"]
    assert decoded["discharge_current_limit_a"] == -25.0
    assert records[-1]["identity_resolved"] is False
    assert bytes.fromhex(records[-1]["raw_frame"]) == response.frame.raw_frame


def test_management_change_events_are_observations_with_old_new(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, batch_size=1, flush_interval_seconds=.01)
    writer.start()
    clock = iter([1_786_147_200.0, 1_786_147_201.0])
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: next(clock))
    for info in (management(0x40), management(0xC0)):
        _, response = pair(info)
        pipeline(response.frame, response)
    writer.stop()
    changes = [item for item in read_records(next(tmp_path.glob("*.jsonl")))
               if item["record_type"] == "state_change"]
    charge = next(item for item in changes if item["field"] == "charge_enable")
    assert (charge["old_value"], charge["new_value"]) == (False, True)
    assert charge["evidence_level"] == "observation"
    assert charge["causality"] == "not_determined"


def test_0x42_is_downsampled_but_0x44_is_complete(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, batch_size=20, flush_interval_seconds=.01)
    writer.start()
    times = iter([1000.0, 1001.0, 1002.0, 1003.0])
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: next(times),
                                     fast_frame_interval_seconds=300)
    for code in (0x42, 0x42, 0x44, 0x44):
        item = frame(code=code)
        pipeline(item, Correlation(item, code, False, 0))
    writer.stop()
    records = read_records(next(tmp_path.glob("*.jsonl")))
    assert [item["paired_command"] for item in records] == [0x42, 0x44, 0x44]


def test_queue_overflow_is_counted_and_nonblocking(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, queue_size=1)
    assert writer.append({"timestamp": "2026-08-31T00:00:00+00:00"})
    assert not writer.append({"timestamp": "2026-08-31T00:00:01+00:00"})
    assert writer.status()["dropped_records"] == 1


def test_history_reads_values_transitions_and_tolerates_partial_last_line(tmp_path):
    path = tmp_path / "2026-08-31.jsonl"
    records = []
    for second, enabled in ((0, False), (10, True)):
        records.append({"record_type": "frame", "timestamp": f"2026-08-31T00:00:{second:02d}+00:00",
                        "adr": 2, "identity_resolved": False,
                        "decoded": {"charge_enable": enabled,
                                    "charge_current_limit_a": second / 10}})
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n{partial")
    series = Rs485HistorySeries(tmp_path).query_bundles(
        [{"metric": "rs485_charge_enable"}, {"metric": "rs485_ccl"}],
        timestamp_from="2026-08-31T00:00:00+00:00",
        timestamp_to="2026-08-31T00:01:00+00:00", adr=2)
    assert [point["value"] for point in series[0]["points"]] == [0, 1]
    assert series[0]["state_semantics"] == {"0": "STOP REQUEST", "1": "ENABLED"}
    assert [point["value"] for point in series[1]["points"]] == [0.0, 1.0]


def test_writer_restart_appends_without_rewriting(tmp_path):
    record = {"timestamp": "2026-08-31T00:00:00+00:00", "record_type": "test"}
    for _ in range(2):
        writer = Rs485EvidenceWriter(tmp_path, batch_size=1, flush_interval_seconds=.01)
        writer.start(); writer.append(record); writer.stop()
    assert len(read_records(tmp_path / "2026-08-31.jsonl")) == 2


def test_main_lifecycle_enabled_reader_persists_0x92_and_logs(tmp_path, caplog):
    """Exercise the production main.py callback wiring, not only the writer."""
    main_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    source = main_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(main_path))
    create_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "create_rs485_reader")
    request, response = pair()

    class Device:
        @staticmethod
        def preferred_path(): return "/dev/serial/by-id/waveshare-test"

    class FakeReader:
        def __init__(self, _resolver, _baudrate, frame_callback=None):
            self.frame_callback = frame_callback
        def start(self):
            self.frame_callback(request, Correlation(request, 0x92, False, 1.0))
            self.frame_callback(response.frame, response)

    namespace = {
        "LOG": logging.getLogger("guardian-main-lifecycle-test"),
        "PassiveRs485Reader": FakeReader,
        "discover_serial_devices": lambda: (),
        "resolve_rs485_port": lambda *_: Device(),
        "ensure_distinct_roles": lambda *_: None,
    }
    exec(compile(ast.Module(body=[create_node], type_ignores=[]), str(main_path), "exec"), namespace)
    history_dir = tmp_path / "rs485_history"
    writer = Rs485EvidenceWriter(history_dir, batch_size=1, flush_interval_seconds=.01)
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: 1_786_147_200.0)
    reader = namespace["create_rs485_reader"]({
        "rs485_sniffer_enabled": True, "rs485_sniffer_port": "auto",
        "rs485_sniffer_baudrate": 115200,
    }, "/dev/ttyUSB0", pipeline)

    caplog.set_level(logging.INFO, logger="guardian_battery.rs485_evidence")
    writer.start()
    reader.start()
    writer.stop()

    path = history_dir / "2026-08-08.jsonl"
    assert path.exists()
    records = read_records(path)
    assert any(item.get("paired_command") == 0x92 for item in records)
    assert "rs485_pipeline = Rs485EvidencePipeline(rs485_writer)" in source
    assert "create_rs485_reader(options, port, rs485_pipeline)" in source
    assert source.index("rs485_writer.start()") < source.index("rs485_reader.start()")
    messages = [record.getMessage() for record in caplog.records]
    assert any("RS485 evidence writer started" in message for message in messages)
    assert any("RS485 evidence first record persisted" in message for message in messages)
