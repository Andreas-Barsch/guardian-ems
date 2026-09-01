"""Phase-A tests for passive Pylontech RS485 protocol evidence.

All byte sequences in this file are synthetic protocol fixtures. They are not
labelled or represented as captures from the production installation.
"""

from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from rs485_sniffer import (  # noqa: E402
    FrameValidationError,
    PassiveRs485Reader,
    ResponseCorrelator,
    StreamFramer,
    calculate_checksum,
    calculate_lchksum,
    decode_0x92,
    decode_0x93,
    decode_0x93_info,
    decode_0x93_request,
    open_passive_serial,
    parse_frame,
)


def synthetic_frame(
    *, adr: int = 2, cid2_or_rtn: int = 0x92, info: bytes = b"", ver: int = 0x20,
    cid1: int = 0x46,
) -> bytes:
    info_hex = info.hex().upper().encode("ascii")
    lenid = len(info_hex)
    length = (calculate_lchksum(lenid) << 12) | lenid
    payload = (
        f"{ver:02X}{adr:02X}{cid1:02X}{cid2_or_rtn:02X}{length:04X}".encode("ascii")
        + info_hex
    )
    return b"~" + payload + f"{calculate_checksum(payload):04X}".encode("ascii") + b"\r"


def test_checksum_algorithms_match_independent_known_vectors():
    assert calculate_lchksum(0x000) == 0x0
    assert calculate_lchksum(0x002) == 0xE
    assert calculate_lchksum(0x014) == 0xB
    assert calculate_lchksum(0xABC) == 0xF
    assert calculate_checksum(b"200246920000") == 0xFDA7


def management_info(
    *, command: int = 0, cvl: int = 53250, dvl: int = 45000,
    ccl: int = 0, dcl: int = -250, status: int = 0x40, trailing: bytes = b"",
) -> bytes:
    return b"".join((
        bytes([command]),
        cvl.to_bytes(2, "big", signed=False),
        dvl.to_bytes(2, "big", signed=False),
        ccl.to_bytes(2, "big", signed=True),
        dcl.to_bytes(2, "big", signed=True),
        bytes([status]),
        trailing,
    ))


def correlated_management(
    *, adr: int = 2, info: bytes | None = None, rtn: int = 0, delay: float = 0.1,
):
    correlator = ResponseCorrelator(timeout_seconds=1)
    correlator.observe(parse_frame(synthetic_frame(adr=adr)), 10.0)
    response = parse_frame(synthetic_frame(
        adr=adr, cid2_or_rtn=rtn,
        info=management_info() if info is None else info,
    ))
    return correlator.observe(response, 10.0 + delay)


def correlated_serial(
    serial: bytes = b"Y225004C32250226", *, adr: int = 2, command: int | None = None,
    rtn: int = 0,
):
    command = adr if command is None else command
    correlator = ResponseCorrelator(timeout_seconds=1)
    request = parse_frame(synthetic_frame(adr=adr, cid2_or_rtn=0x93,
                                          info=bytes([command])))
    correlator.observe(request, 10.0)
    response = parse_frame(synthetic_frame(
        adr=adr, cid2_or_rtn=rtn, info=bytes([command]) + serial,
    ))
    return request, correlator.observe(response, 10.1)


def test_stream_framer_accepts_complete_and_fragmented_frames():
    raw = synthetic_frame()
    framer = StreamFramer()
    assert framer.feed(raw[:7]) == []
    assert framer.feed(raw[7:]) == [raw]
    assert framer.buffered_bytes == 0


def test_stream_framer_emits_two_frames_from_one_chunk():
    first, second = synthetic_frame(adr=2), synthetic_frame(adr=3)
    assert StreamFramer().feed(first + second) == [first, second]


def test_stream_framer_discards_garbage_and_resynchronizes():
    raw = synthetic_frame()
    assert StreamFramer().feed(b"noise\x00\xff" + raw) == [raw]


def test_stream_framer_waits_for_eoi_then_recovers_at_next_soi():
    raw = synthetic_frame(adr=3)
    framer = StreamFramer()
    assert framer.feed(b"~204246920000") == []
    assert framer.feed(raw) == [raw]


def test_parse_valid_frame_exposes_raw_and_protocol_fields():
    raw = synthetic_frame(info=b"\x01\x02")
    frame = parse_frame(raw)
    assert (frame.ver, frame.adr, frame.cid1, frame.command) == (0x20, 2, 0x46, 0x92)
    assert frame.info == b"\x01\x02"
    assert frame.info_hex == "0102"
    assert frame.raw_frame == raw
    assert frame.checksum_valid and frame.frame_complete
    assert frame.source == "rs485_passive"


@pytest.mark.parametrize("raw", [b"", b"~2046\r", b"20460246920000FFFF\r", b"~20460246920000FFFF"])
def test_parse_rejects_incomplete_or_undelimited_frames(raw):
    with pytest.raises(FrameValidationError, match="incomplete_frame"):
        parse_frame(raw)


def test_parse_rejects_invalid_hex_ascii():
    raw = bytearray(synthetic_frame(info=b"\x01"))
    raw[13] = ord("Z")
    with pytest.raises(FrameValidationError, match="invalid_hex_ascii"):
        parse_frame(bytes(raw))


def test_parse_rejects_wrong_length():
    raw = synthetic_frame(info=b"\x01")
    body = bytearray(raw[1:-1])
    declared = 4
    body[8:12] = f"{(calculate_lchksum(declared) << 12) | declared:04X}".encode()
    body[-4:] = f"{calculate_checksum(body[:-4]):04X}".encode()
    with pytest.raises(FrameValidationError, match="invalid_length"):
        parse_frame(b"~" + bytes(body) + b"\r")


def test_parse_rejects_wrong_lchksum():
    raw = bytearray(synthetic_frame(info=b"\x01"))
    raw[9] = ord("F") if raw[9] != ord("F") else ord("E")
    with pytest.raises(FrameValidationError, match="invalid_lchksum"):
        parse_frame(bytes(raw))


def test_parse_rejects_wrong_checksum():
    raw = bytearray(synthetic_frame(info=b"\x01"))
    raw[-2] = ord("0") if raw[-2] != ord("0") else ord("1")
    with pytest.raises(FrameValidationError, match="invalid_checksum"):
        parse_frame(bytes(raw))


def test_correlation_pairs_0x92_by_adr_and_order():
    correlator = ResponseCorrelator(timeout_seconds=1)
    request_two = correlator.observe(parse_frame(synthetic_frame(adr=2)), 10)
    request_three = correlator.observe(parse_frame(synthetic_frame(adr=3)), 10.1)
    response_three = correlator.observe(parse_frame(synthetic_frame(
        adr=3, cid2_or_rtn=0, info=management_info(),
    )), 10.2)
    assert request_two.paired_command == request_three.paired_command == 0x92
    assert not request_two.request_matched
    assert response_three.request_matched and response_three.paired_command == 0x92
    assert response_three.request_timestamp == 10.1


def test_correlation_marks_unmatched_and_expired_responses():
    response = parse_frame(synthetic_frame(cid2_or_rtn=0, info=management_info()))
    correlator = ResponseCorrelator(timeout_seconds=0.5)
    assert not correlator.observe(response, 1).request_matched
    correlator.observe(parse_frame(synthetic_frame()), 2)
    expired = correlator.observe(response, 3)
    assert not expired.request_matched and expired.paired_command is None


def test_unknown_cid2_is_preserved_and_not_treated_as_known_request():
    frame = parse_frame(synthetic_frame(cid2_or_rtn=0xA1, info=b"\xDE\xAD"))
    result = ResponseCorrelator().observe(frame, 1)
    assert frame.cid2_or_rtn == 0xA1
    assert frame.info_hex == "DEAD"
    assert not frame.is_request
    assert not result.request_matched


def test_nonzero_rtn_is_correlated_but_not_decoded_as_values():
    result = decode_0x92(correlated_management(rtn=1, info=b""))
    assert result["request_matched"] is True
    assert result["decoder_supported"] is False
    assert result["decode_error"] == "nonzero_rtn"


def test_decode_0x92_preserves_negative_dcl_raw_signed_and_magnitude():
    result = decode_0x92(correlated_management())
    assert result["charge_current_limit_raw"] == 0
    assert result["charge_current_limit_a"] == 0.0
    assert result["discharge_current_limit_raw"] == 0xFF06
    assert result["discharge_current_limit_a"] == -25.0
    assert result["discharge_current_limit_magnitude_a"] == 25.0
    assert result["charge_voltage_limit_v"] == 53.25
    assert result["discharge_voltage_limit_v"] == 45.0


@pytest.mark.parametrize(
    ("status", "charge", "discharge", "immediate_one", "immediate_two", "full"),
    [
        (0x00, False, False, False, False, False),
        (0xF8, True, True, True, True, True),
        (0x80, True, False, False, False, False),
        (0x40, False, True, False, False, False),
    ],
)
def test_decode_0x92_status_bits(status, charge, discharge, immediate_one, immediate_two, full):
    result = decode_0x92(correlated_management(info=management_info(status=status)))
    assert result["charge_enable"] is charge
    assert result["discharge_enable"] is discharge
    assert result["charge_immediately_1"] is immediate_one
    assert result["charge_immediately_2"] is immediate_two
    assert result["full_charge_request"] is full


def test_decode_0x92_retains_unknown_bits_and_trailing_bytes():
    result = decode_0x92(correlated_management(
        info=management_info(status=0x07, trailing=b"\xAA\x55"),
    ))
    assert result["unknown_status_bits_raw"] == 7
    assert result["unknown_trailing_info"] == "AA55"
    assert result["decoder_supported"] is True


def test_decode_0x92_marks_short_info_unsupported():
    result = decode_0x92(correlated_management(info=b"\x00" * 9))
    assert result["decode_error"] == "info_too_short"
    assert result["decoder_supported"] is False


def test_decode_rejects_unmatched_response():
    frame = parse_frame(synthetic_frame(cid2_or_rtn=0, info=management_info()))
    correlation = ResponseCorrelator().observe(frame, 1)
    with pytest.raises(ValueError, match="correlated 0x92"):
        decode_0x92(correlation)


def test_decode_0x93_validates_documented_request():
    request, _ = correlated_serial()
    result = decode_0x93_request(request)
    assert result["adr"] == 2
    assert result["command"] == 2
    assert result["decoder_supported"] is True
    assert result["protocol_reference"] == (
        "Pylontech Low Voltage RS485 Protocol V3.3, 2018-08-21")


@pytest.mark.parametrize(("adr", "info", "error"), [
    (2, b"", "invalid_info_length"),
    (2, b"\x01\x02", "invalid_info_length"),
    (2, b"\x00", "command_out_of_range"),
    (2, b"\x03", "command_adr_mismatch"),
])
def test_decode_0x93_rejects_invalid_request_structure(adr, info, error):
    request = parse_frame(synthetic_frame(adr=adr, cid2_or_rtn=0x93, info=info))
    result = decode_0x93_request(request)
    assert result["decoder_supported"] is False
    assert result["decode_error"] == error


@pytest.mark.parametrize("adr", [2, 3, 8])
def test_decode_0x93_response_preserves_exact_ascii_and_raw_bytes(adr):
    serial = b"Y225004C32250226"
    _, correlation = correlated_serial(serial, adr=adr)
    result = decode_0x93(correlation)
    assert result["adr"] == adr
    assert result["command"] == adr
    assert result["serial_string"] == "Y225004C32250226"
    assert bytes.fromhex(result["serial_raw"]) == serial
    assert result["checksum_valid"] is True
    assert result["request_matched"] is True
    assert result["decoder_supported"] is True


def test_decode_0x93_info_is_the_central_raw_decoder():
    result = decode_0x93_info(2, bytes.fromhex(
        "0248323231303035453232323132353831"))
    assert result == {
        "command": 2,
        "serial_raw": "48323231303035453232323132353831",
        "serial_string": "H221005E22212581",
        "decoder_supported": True,
    }


def test_decode_0x93_does_not_trim_or_normalize_ascii_bytes():
    serial = b" A234567890123\x00 "
    assert len(serial) == 16
    result = decode_0x93(correlated_serial(serial)[1])
    assert result["serial_string"].encode("ascii") == serial
    assert bytes.fromhex(result["serial_raw"]) == serial


@pytest.mark.parametrize("serial", [b"A" * 15, b"A" * 17])
def test_decode_0x93_rejects_non_exact_serial_length(serial):
    result = decode_0x93(correlated_serial(serial)[1])
    assert result["decoder_supported"] is False
    assert result["decode_error"] == "invalid_info_length"


def test_decode_0x93_rejects_response_command_adr_mismatch():
    result = decode_0x93(correlated_serial(command=3)[1])
    assert result["decoder_supported"] is False
    assert result["decode_error"] == "command_adr_mismatch"


def test_decode_0x93_marks_unmatched_response_unsupported():
    response = parse_frame(synthetic_frame(
        adr=2, cid2_or_rtn=0, info=b"\x02Y225004C32250226"))
    result = decode_0x93(ResponseCorrelator().observe(response, 1.0))
    assert result["request_matched"] is False
    assert result["decoder_supported"] is False
    assert result["decode_error"] == "unmatched_response"


def test_0x93_invalid_checksum_is_rejected_before_decode():
    raw = bytearray(synthetic_frame(
        adr=2, cid2_or_rtn=0, info=b"\x02Y225004C32250226"))
    raw[-2] = ord("0") if raw[-2] != ord("0") else ord("1")
    with pytest.raises(FrameValidationError, match="invalid_checksum"):
        parse_frame(bytes(raw))


def test_runtime_module_has_no_transmission_path():
    source_path = Path(__file__).resolve().parents[1] / "app" / "rs485_sniffer.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {"write", "writelines", "send", "request", "poll", "transmit"}
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (calls & forbidden_calls)
    assert "0x94 Builder" not in source and "0x95 Builder" not in source


def test_runtime_integration_is_feature_flagged_and_not_diagnostic():
    app = Path(__file__).resolve().parents[1] / "app"
    main_source = (app / "main.py").read_text(encoding="utf-8")
    assert 'options.get("rs485_sniffer_enabled", False)' in main_source
    assert "rs485_mqtt.publish" in main_source
    assert "cell_store.add(rs485" not in main_source
    assert "analyse(rs485" not in main_source
    assert "publisher.publish" in main_source


class FakeSerial:
    def __init__(self, chunks=(), failure=None):
        self.chunks = list(chunks)
        self.failure = failure
        self.read_calls = 0
        self.write_calls = 0
        self.closed = False

    def read(self, _size):
        self.read_calls += 1
        if self.chunks:
            return self.chunks.pop(0)
        if self.failure:
            raise self.failure
        time.sleep(0.002)
        return b""

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_passive_open_sets_control_lines_before_open(monkeypatch):
    events = []

    class Connection:
        def __init__(self, **settings):
            events.append(("init", settings["port"]))
            self.rs485_mode = "unset"
        def __setattr__(self, name, value):
            if name in {"rts", "dtr", "port", "rs485_mode"}:
                events.append((name, value))
            object.__setattr__(self, name, value)
        def open(self):
            events.append(("open", (self.rts, self.dtr, self.rs485_mode, self.port)))

    monkeypatch.setattr("rs485_sniffer.serial.Serial", Connection)
    connection = open_passive_serial("/dev/ttyACM0", 115200, 0.25)
    assert connection.port == "/dev/ttyACM0"
    assert events[-1] == ("open", (False, False, None, "/dev/ttyACM0"))


def test_reader_reads_without_writing_and_start_stop_are_idempotent():
    raw = synthetic_frame()
    fake = FakeSerial([raw])
    reader = PassiveRs485Reader(lambda: "/dev/ttyACM0", serial_opener=lambda *_: fake,
                                reconnect_delays=(0.01,))
    assert reader.start() is True
    assert reader.start() is False
    assert wait_until(lambda: reader.status()["valid_frames"] == 1)
    reader.stop()
    reader.stop()
    assert fake.read_calls > 0
    assert fake.write_calls == 0
    assert fake.closed
    assert reader.status()["state"] == "disabled"


def test_reader_reconnects_after_serial_exception_and_stop_interrupts_backoff():
    first = FakeSerial(failure=RuntimeError("disconnected"))
    second = FakeSerial()
    opened = []
    def opener(*_):
        value = first if not opened else second
        opened.append(value)
        return value
    reader = PassiveRs485Reader(lambda: "/dev/ttyACM0", serial_opener=opener,
                                reconnect_delays=(0.01, 0.02))
    reader.start()
    assert wait_until(lambda: len(opened) >= 2)
    reader.stop()
    assert first.closed and second.closed
    assert len(opened) == 2


def test_reader_counts_invalid_checksum_unmatched_response_and_timestamps():
    clock = iter([100.0, 101.0, 102.0, 103.0])
    reader = PassiveRs485Reader(lambda: "unused", wall_clock=lambda: next(clock))
    bad = bytearray(synthetic_frame())
    bad[-2] = ord("0") if bad[-2] != ord("0") else ord("1")
    reader._process(bytes(bad))
    reader._process(synthetic_frame(cid2_or_rtn=0, info=management_info()))
    status = reader.status()
    assert status["frames_total"] == 2
    assert status["checksum_errors"] == 1
    assert status["valid_frames"] == 1
    assert status["unmatched_responses"] == 1
    assert status["last_byte_at"] == 101.0
    assert status["last_valid_frame_at"] == 101.0


def test_runtime_0x92_latest_state_is_per_adr_and_bounded():
    reader = PassiveRs485Reader(lambda: "unused", max_adr_states=2)
    for adr in (2, 3, 4):
        reader._process(synthetic_frame(adr=adr))
        reader._process(synthetic_frame(adr=adr, cid2_or_rtn=0, info=management_info(dcl=-250-adr)))
    assert set(reader.latest_management_by_adr) == {3, 4}
    assert reader.latest_management_by_adr[4]["discharge_current_limit_a"] == -25.4
    assert reader.status()["requests_0x92"] == 3
    assert reader.status()["responses_0x92"] == 3
    assert reader.status()["last_0x92_at"] is not None


def test_runtime_0x93_identity_state_is_direct_and_bounded():
    reader = PassiveRs485Reader(lambda: "unused", max_adr_states=2)
    for adr, serial in ((2, b"H221005E22212581"), (3, b"H221005E22212536"),
                        (6, b"Y225004C32250226")):
        reader._process(synthetic_frame(adr=adr, cid2_or_rtn=0x93, info=bytes([adr])))
        reader._process(synthetic_frame(adr=adr, cid2_or_rtn=0,
                                        info=bytes([adr]) + serial))
    identities = reader.identities()
    assert set(identities) == {3, 6}
    assert identities[6]["serial_string"] == "Y225004C32250226"
    assert identities[6]["adr"] == 6
    assert "position" not in identities[6]
    assert identities[6]["identity_currently_confirmed"] is True
    status = reader.status()
    assert status["requests_0x93"] == 3
    assert status["responses_0x93"] == status["matched_0x93"] == 3
    assert status["decoded_valid_0x93"] == 3
    assert status["decode_errors_0x93"] == 0
    assert status["identity_state_entries"] == 2


def test_restored_identity_becomes_current_and_live_change_wins():
    reader = PassiveRs485Reader(lambda: "unused")
    reader.restore_identities({2: {"serial_string": "H221005E22212581",
                                   "serial_raw": b"H221005E22212581".hex(),
                                   "timestamp": 1, "decode_source": "historical_raw_redecode"}})
    assert reader.identities()[2]["identity_currently_confirmed"] is False
    reader._process(synthetic_frame(adr=2, cid2_or_rtn=0x93, info=b"\x02"))
    reader._process(synthetic_frame(adr=2, cid2_or_rtn=0,
                                    info=b"\x02H221005E22212536"))
    identity = reader.identities()[2]
    assert identity["serial_string"] == "H221005E22212536"
    assert identity["decode_source"] == "live_0x93"
    assert identity["identity_currently_confirmed"] is True


def test_reader_handles_fragmented_and_multiple_frames():
    reader = PassiveRs485Reader(lambda: "unused")
    first, second = synthetic_frame(adr=2), synthetic_frame(adr=3)
    reader._process(first[:8])
    reader._process(first[8:] + second)
    assert reader.status()["valid_frames"] == 2


def test_reader_recognizes_0x44_without_decoding_variable_info():
    reader = PassiveRs485Reader(lambda: "unused")
    reader._process(synthetic_frame(cid2_or_rtn=0x44))
    reader._process(synthetic_frame(cid2_or_rtn=0, info=b"\x01\x02\x03"))
    assert reader.status()["requests_0x44"] == 1
    assert reader.status()["responses_0x44"] == 1
    assert reader.latest_management_by_adr == {}


def test_stop_interrupts_long_reconnect_wait():
    class Unavailable(RuntimeError):
        code = "rs485_port_unavailable"
    reader = PassiveRs485Reader(lambda: (_ for _ in ()).throw(Unavailable("missing")),
                                reconnect_delays=(10.0,))
    reader.start()
    assert wait_until(lambda: reader.status()["state"] == "unavailable")
    started = time.monotonic()
    reader.stop()
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(("code", "state"), [
    ("ambiguous_rs485_port", "ambiguous_port"),
    ("serial_role_conflict", "serial_role_conflict"),
    ("unexpected", "error"),
])
def test_reader_exposes_resolution_error_state(code, state):
    class ResolutionFailure(RuntimeError):
        pass
    error = ResolutionFailure(code)
    error.code = code
    reader = PassiveRs485Reader(lambda: (_ for _ in ()).throw(error), reconnect_delays=(10.0,))
    reader.start()
    assert wait_until(lambda: reader.status()["state"] == state)
    assert code in reader.status()["last_error"]
    reader.stop()


def test_acceptance_logs_are_one_time_compact_and_contain_no_raw_frame(caplog):
    caplog.set_level("INFO", logger="guardian_battery.rs485")
    reader = PassiveRs485Reader(lambda: "unused")
    for _ in range(2):
        reader._process(synthetic_frame(adr=2))
        reader._process(synthetic_frame(adr=2, cid2_or_rtn=0, info=management_info()))
    messages = [record.getMessage() for record in caplog.records]
    assert sum("first valid frame" in message for message in messages) == 1
    assert sum("first valid 0x92" in message for message in messages) == 1
    management = next(message for message in messages if "first valid 0x92" in message)
    assert "adr=02" in management and "ccl=0.0A" in management and "dcl=-25.0A" in management
    assert "charge_enable=False" in management and "discharge_enable=True" in management
    assert synthetic_frame().decode("ascii") not in "\n".join(messages)


def test_callback_receives_evidence_only_and_cannot_access_reader_serial_object():
    received = []
    reader = PassiveRs485Reader(lambda: "unused", frame_callback=lambda *values: received.append(values))
    reader._process(synthetic_frame())
    assert len(received) == 1
    assert len(received[0]) == 2
    assert all(not isinstance(value, FakeSerial) for value in received[0])
