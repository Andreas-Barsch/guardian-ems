import ast
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from rs485_evidence import (Rs485EvidencePipeline, Rs485EvidenceWriter,
                            Rs485HistorySeries, restore_latest_identities)
from rs485_sniffer import (Correlation, ResponseCorrelator, calculate_checksum,
                           calculate_lchksum, parse_frame)
from rs485_history_index import load_valid
import rs485_evidence


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


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return False


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


def test_pipeline_persists_and_decodes_every_valid_0x93_request_response(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, batch_size=1, flush_interval_seconds=.01)
    writer.start()
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: 1_786_147_200.0,
                                     position_history_path=tmp_path / "positions.jsonl")
    correlator = ResponseCorrelator()
    request = frame(adr=6, code=0x93, info=b"\x06")
    request_correlation = correlator.observe(request, 1.0)
    response = frame(adr=6, code=0, info=b"\x06Y225004C32250226")
    response_correlation = correlator.observe(response, 1.1)
    pipeline(request, request_correlation)
    pipeline(response, response_correlation)
    writer.stop()
    records = read_records(next(tmp_path.glob("*.jsonl")))
    assert len(records) == 2
    assert records[0]["paired_command"] == records[1]["paired_command"] == 0x93
    assert records[1]["decoded"]["serial_string"] == "Y225004C32250226"
    assert records[1]["physical_serial"] == "Y225004C32250226"
    assert records[1]["identity_resolved"] is False


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


def test_every_passive_0x47_request_and_response_is_persisted_and_decoded(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, batch_size=1, flush_interval_seconds=.01)
    writer.start()
    clock = iter([1_786_147_200.0 + index for index in range(4)])
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: next(clock),
                                     position_history_path=tmp_path / "positions.jsonl")
    correlator = ResponseCorrelator()
    values = (3600, 3000, 2800, 3182, 2732, 250, 54000, 45000, 42000, 3232, 2632, -250)
    info = bytes([0]) + b"".join(value.to_bytes(2, "big", signed=value < 0)
                                  for value in values)
    for index, offset in enumerate((0, 100)):
        request = frame(adr=6, code=0x47)
        pipeline(request, correlator.observe(request, 10 + offset))
        current_info = info if index == 0 else info[:3] + (2990).to_bytes(2, "big") + info[5:]
        response = frame(adr=6, code=0, info=current_info)
        pipeline(response, correlator.observe(response, 10.1 + offset))
    writer.stop()
    records = read_records(next(tmp_path.glob("*.jsonl")))
    assert len(records) == 4
    assert all(record["paired_command"] == 0x47 for record in records)
    assert [record["decoder_supported"] for record in records] == [False, True, False, True]
    assert [record["decoded"]["cell_low_voltage_alarm_limit_v"]
            for record in records if record["decoder_supported"]] == [3.0, 2.99]
    assert records[-1]["physical_serial"] is None


def test_0x47_inherits_only_observed_0x93_identity_without_position_formula(tmp_path):
    writer = Rs485EvidenceWriter(tmp_path, batch_size=1, flush_interval_seconds=.01)
    writer.start()
    clock = iter([1_786_147_200.0 + index for index in range(4)])
    pipeline = Rs485EvidencePipeline(writer, wall_clock=lambda: next(clock),
                                     position_history_path=tmp_path / "positions.jsonl")
    correlator = ResponseCorrelator()
    serial_request = frame(adr=6, code=0x93, info=b"\x06")
    pipeline(serial_request, correlator.observe(serial_request, 1))
    serial_response = frame(adr=6, code=0, info=b"\x06Y225004C32250226")
    pipeline(serial_response, correlator.observe(serial_response, 1.1))
    request = frame(adr=6, code=0x47)
    pipeline(request, correlator.observe(request, 2))
    values = (3600, 3000, 2800, 3182, 2732, 250, 54000, 45000, 42000, 3232, 2632, -250)
    info = bytes([0]) + b"".join(value.to_bytes(2, "big", signed=value < 0)
                                  for value in values)
    response = frame(adr=6, code=0, info=info)
    pipeline(response, correlator.observe(response, 2.1))
    writer.stop()
    threshold = read_records(next(tmp_path.glob("*.jsonl")))[-1]
    assert threshold["physical_serial"] == "Y225004C32250226"
    assert threshold["identity_decode_source"] == "stored_decoded"
    assert threshold["position"] is None
    assert threshold["identity_resolved"] is False


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


def test_writer_maintains_rebuildable_rs485_index_outside_research(tmp_path):
    record = {"timestamp": "2026-08-31T00:00:00+00:00", "record_type": "test"}
    writer = Rs485EvidenceWriter(
        tmp_path, batch_size=1, flush_interval_seconds=.01,
        index_interval_seconds=.01)
    writer.start(); writer.append(record); writer.stop()
    source = tmp_path / "2026-08-31.jsonl"
    index = load_valid(source)
    assert index["complete"] is True
    assert index["blocks"][0]["record_count"] == 1


def test_existing_large_history_backfills_multiple_blocks_without_new_write(
        tmp_path, caplog):
    source = tmp_path / "2026-08-31.jsonl"
    with source.open("w", encoding="utf-8") as handle:
        for index in range(1_600):
            handle.write(json.dumps({
                "timestamp": f"2026-08-31T00:{index // 60:02d}:{index % 60:02d}+00:00",
                "record_type": "test", "sequence": index,
            }, separators=(",", ":")) + "\n")
    raw = source.read_bytes()
    writer = Rs485EvidenceWriter(
        tmp_path, index_interval_seconds=.01, flush_interval_seconds=.02)
    caplog.set_level(logging.INFO, logger="guardian_battery.rs485_evidence")
    writer.start()
    observed_sizes = set()

    def three_cycles():
        try:
            index = load_valid(source)
        except Exception:
            return False
        observed_sizes.add(index["indexed_size"])
        return len(index["blocks"]) >= 3 and len(observed_sizes) >= 3

    assert wait_until(three_cycles)
    running = writer.status()
    assert running["writer_thread_alive"] is True
    assert running["index_thread_alive"] is True
    assert running["index_files_seen"] == 1
    assert running["index_last_run_at"] is not None
    assert running["index_last_error"] is None
    writer.stop()
    stopped = writer.status()
    assert stopped["writer_thread_alive"] is False
    assert stopped["index_thread_alive"] is False
    assert source.read_bytes() == raw
    messages = [record.getMessage() for record in caplog.records]
    assert sum("index first candidate" in message for message in messages) == 1
    assert any("day_file=2026-08-31.jsonl sidecar_present=False" in message
               for message in messages)
    assert any("index first extend result=success indexed_size=" in message
               and "block_count=1" in message for message in messages)


def test_current_day_short_history_creates_valid_empty_sidecar_without_write(tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    source = tmp_path / f"{today}.jsonl"
    source.write_text(json.dumps({"timestamp": f"{today}T00:00:00+00:00",
                                  "record_type": "test"}) + "\n")
    writer = Rs485EvidenceWriter(
        tmp_path, index_interval_seconds=.01, flush_interval_seconds=.02)
    writer.start()
    assert wait_until(lambda: Path(str(source) + ".idx").exists())
    index = load_valid(source)
    writer.stop()
    assert index["indexed_size"] == 0
    assert index["blocks"] == []
    assert index["complete"] is False


def test_indexer_visits_newest_day_first_without_new_write(tmp_path, monkeypatch):
    old = tmp_path / "2026-08-30.jsonl"
    new = tmp_path / "2026-08-31.jsonl"
    for path in (old, new):
        path.write_text(json.dumps({"timestamp": path.stem + "T00:00:00+00:00"}) + "\n")
    calls = []

    def observe(path, **_kwargs):
        calls.append(path.name)
        return {"indexed_size": 0, "blocks": []}

    monkeypatch.setattr(rs485_evidence, "extend_rs485_history_index", observe)
    writer = Rs485EvidenceWriter(
        tmp_path, index_interval_seconds=.01, flush_interval_seconds=.02)
    writer.start()
    assert wait_until(lambda: len(calls) >= 2)
    writer.stop()
    assert calls[:2] == [new.name, old.name]


def test_index_error_is_visible_and_does_not_stop_raw_writer(tmp_path, monkeypatch):
    source = tmp_path / "2026-08-31.jsonl"
    source.write_text(json.dumps({"timestamp": "2026-08-31T00:00:00+00:00"}) + "\n")

    def fail(*_args, **_kwargs):
        raise RuntimeError("test index failure")

    monkeypatch.setattr(rs485_evidence, "extend_rs485_history_index", fail)
    writer = Rs485EvidenceWriter(
        tmp_path, batch_size=1, index_interval_seconds=.01,
        flush_interval_seconds=.01)
    writer.start()
    assert wait_until(lambda: writer.status()["index_last_error"] == "RuntimeError")
    assert writer.status()["writer_thread_alive"] is True
    writer.append({"timestamp": "2026-08-31T00:00:01+00:00",
                   "record_type": "test"})
    assert wait_until(lambda: writer.status()["records_written"] == 1)
    writer.stop()
    assert len(read_records(source)) == 2


def test_startup_restore_redecodes_latest_valid_identity_read_only(tmp_path):
    path = tmp_path / "2026-08-31.jsonl"
    record = {"record_type": "frame", "timestamp": "2026-08-31T20:24:42+00:00",
              "adr": 2, "direction": "response", "paired_command": 0x93,
              "checksum_valid": True, "frame_complete": True, "request_matched": True,
              "info_raw": "0248323231303035453232323132353831",
              "decoder_supported": False, "decoded": None}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    before = path.read_bytes()
    restored = restore_latest_identities(tmp_path)
    assert restored[2]["serial_string"] == "H221005E22212581"
    assert restored[2]["decode_source"] == "historical_raw_redecode"
    assert restored[2]["identity_known"] is True
    assert restored[2]["identity_currently_confirmed"] is False
    assert path.read_bytes() == before


def test_startup_restore_rejects_invalid_records_and_uses_newest_day(tmp_path):
    base = {"record_type": "frame", "direction": "response", "paired_command": 0x93,
            "checksum_valid": True, "frame_complete": True, "request_matched": True,
            "decoder_supported": True, "decoded": {"serial_string": "stored"}}
    old = {**base, "timestamp": "2026-08-30T10:00:00+00:00", "adr": 2,
           "info_raw": "02" + b"H221005E22212581".hex()}
    new = {**base, "timestamp": "2026-08-31T10:00:00+00:00", "adr": 2,
           "info_raw": "02" + b"H221005E22212536".hex()}
    invalid = {**base, "timestamp": "2026-08-31T11:00:00+00:00", "adr": 3,
               "checksum_valid": False, "info_raw": "03" + b"H221005E22212571".hex()}
    (tmp_path / "2026-08-30.jsonl").write_text(json.dumps(old) + "\n")
    (tmp_path / "2026-08-31.jsonl").write_text(
        json.dumps(new) + "\n" + json.dumps(invalid) + "\n")
    restored = restore_latest_identities(tmp_path)
    assert restored[2]["serial_string"] == "H221005E22212536"
    assert restored[2]["decode_source"] == "last_confirmed_0x93"
    assert 3 not in restored


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
    assert source.index("CurrentConditionBackfill(") < source.index(
        'LOG.info("RS485 evidence lifecycle: writer start requested")')
    assert source.index(
        'LOG.info("RS485 evidence lifecycle: writer start requested")') < source.index(
        "rs485_writer.start()")
    assert source.index("rs485_writer.start()") < source.index("rs485_reader.start()")
    assert source.index("rs485_reader.start()") < source.index(
        "aggregate_backfill_worker.start()")
    assert source.index("aggregate_backfill_worker.start()") < source.index(
        "persistence_worker.start()")
    messages = [record.getMessage() for record in caplog.records]
    assert any("RS485 evidence writer started" in message for message in messages)
    assert any("RS485 evidence first record persisted" in message for message in messages)


def test_lifecycle_logs_and_status_are_non_sensitive(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="guardian_battery.rs485_evidence")
    writer = Rs485EvidenceWriter(
        tmp_path, index_interval_seconds=.01, flush_interval_seconds=.02)
    writer.start()
    assert wait_until(lambda: writer.status()["index_last_run_at"] is not None)
    writer.stop()
    messages = [record.getMessage() for record in caplog.records]
    encoded = "\n".join(messages)
    assert "writer start entered" in encoded
    assert "evidence_thread_alive=True index_thread_alive=True" in encoded
    assert "index thread entered" in encoded
    assert "index first run candidate_files=0" in encoded
    assert "index thread exited reason=stop" in encoded
    assert str(tmp_path) not in encoded
    assert set(writer.status()) >= {
        "writer_thread_alive", "index_thread_alive", "index_last_run_at",
        "index_files_seen", "index_last_error"}


def test_runtime_image_uses_embedded_version_module_identity():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "guardian_battery/Dockerfile").read_text()
    run_script = (root / "guardian_battery/run.sh").read_text()
    main_source = (root / "guardian_battery/app/main.py").read_text()
    version_source = (root / "guardian_battery/app/version.py").read_text()
    assert "COPY app /app" in dockerfile
    assert "exec python3 /app/main.py" in run_script
    assert "RESEARCH_SEMANTICS_VERSION, SOURCE_COMMIT" in main_source
    assert 'source_commit=%s; research_semantics=%s' in main_source
    assert 'SOURCE_COMMIT = "43c04ab0b67fec4bcf2e4bcdb34b31767a90b620"' in version_source
