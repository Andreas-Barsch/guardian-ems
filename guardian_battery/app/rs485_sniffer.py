"""Passive Pylontech RS485 framing and evidence decoding.

This Phase-A module only consumes byte sequences supplied by its caller.  It
does not open a serial device and deliberately contains no transmission path.

Protocol reference: Pylontech Low Voltage RS485 Protocol V3.3, 2018-08-21.
The decoded values are protocol evidence, not diagnostic conclusions.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import logging
import threading
import time
from typing import Deque

import serial


SOI = 0x7E
EOI = 0x0D
PROTOCOL_REFERENCE = "Pylontech Low Voltage RS485 Protocol V3.3, 2018-08-21"
DECODER_VERSION = "guardian-rs485-phase-a-1"
KNOWN_COMMANDS = frozenset({0x42, 0x44, 0x47, 0x92, 0x93, 0x94, 0x95, 0x96})


class FrameValidationError(ValueError):
    """A frame is structurally invalid and cannot be protocol evidence."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedFrame:
    raw_frame: bytes
    raw_ascii: str
    ver: int
    adr: int
    cid1: int
    cid2_or_rtn: int
    lenid: int
    info_hex: str
    info: bytes
    checksum_received: int
    checksum_valid: bool
    frame_complete: bool = True
    source: str = "rs485_passive"
    protocol_reference: str = PROTOCOL_REFERENCE
    decoder_version: str = DECODER_VERSION

    @property
    def is_request(self) -> bool:
        return self.cid2_or_rtn in KNOWN_COMMANDS

    @property
    def command(self) -> int | None:
        return self.cid2_or_rtn if self.is_request else None

    @property
    def rtn(self) -> int | None:
        return None if self.is_request else self.cid2_or_rtn


@dataclass(frozen=True)
class Correlation:
    frame: ParsedFrame
    paired_command: int | None
    request_matched: bool
    request_timestamp: float | None


class StreamFramer:
    """Extract delimiter-complete frames from arbitrary read chunks."""

    def __init__(self, max_frame_bytes: int = 65536):
        if max_frame_bytes < 2:
            raise ValueError("max_frame_bytes must be at least 2")
        self.max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[bytes]:
        if not isinstance(chunk, bytes):
            raise TypeError("chunk must be bytes")
        self._buffer.extend(chunk)
        frames: list[bytes] = []

        while self._buffer:
            soi = self._buffer.find(SOI)
            if soi < 0:
                self._buffer.clear()
                break
            if soi:
                del self._buffer[:soi]

            next_soi = self._buffer.find(SOI, 1)
            eoi = self._buffer.find(EOI, 1)
            if next_soi >= 0 and (eoi < 0 or next_soi < eoi):
                del self._buffer[:next_soi]
                continue
            if eoi < 0:
                if len(self._buffer) > self.max_frame_bytes:
                    self._buffer.clear()
                break

            frames.append(bytes(self._buffer[: eoi + 1]))
            del self._buffer[: eoi + 1]

        return frames


def _hex_int(value: bytes, field: str) -> int:
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FrameValidationError("invalid_hex_ascii") from exc
    if not text or any(character not in "0123456789abcdefABCDEF" for character in text):
        raise FrameValidationError("invalid_hex_ascii")
    try:
        return int(text, 16)
    except ValueError as exc:
        raise FrameValidationError(f"invalid_{field}") from exc


def calculate_lchksum(lenid: int) -> int:
    """Return the V3.3 LENGTH check nibble for a 12-bit INFO length."""
    if not 0 <= lenid <= 0xFFF:
        raise ValueError("lenid must fit in 12 bits")
    nibble_sum = (lenid >> 8) + ((lenid >> 4) & 0xF) + (lenid & 0xF)
    return (-nibble_sum) & 0xF


def calculate_checksum(ascii_payload: bytes) -> int:
    """Return the 16-bit two's-complement checksum over VER through INFO."""
    return (-sum(ascii_payload)) & 0xFFFF


def parse_frame(raw_frame: bytes) -> ParsedFrame:
    """Validate and parse one delimiter-complete HEX-ASCII frame."""
    if not isinstance(raw_frame, bytes):
        raise TypeError("raw_frame must be bytes")
    if len(raw_frame) < 18 or raw_frame[0] != SOI or raw_frame[-1] != EOI:
        raise FrameValidationError("incomplete_frame")

    body = raw_frame[1:-1]
    try:
        raw_ascii = raw_frame.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FrameValidationError("invalid_hex_ascii") from exc

    ver = _hex_int(body[0:2], "ver")
    adr = _hex_int(body[2:4], "adr")
    cid1 = _hex_int(body[4:6], "cid1")
    cid2_or_rtn = _hex_int(body[6:8], "cid2_or_rtn")
    length_field = _hex_int(body[8:12], "length")
    lchksum = length_field >> 12
    lenid = length_field & 0xFFF
    if lchksum != calculate_lchksum(lenid):
        raise FrameValidationError("invalid_lchksum")

    info_hex_bytes = body[12:-4]
    if len(info_hex_bytes) != lenid:
        raise FrameValidationError("invalid_length")
    if len(info_hex_bytes) % 2:
        raise FrameValidationError("invalid_length")
    try:
        info = bytes.fromhex(info_hex_bytes.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameValidationError("invalid_hex_ascii") from exc

    checksum_received = _hex_int(body[-4:], "checksum")
    checksum_valid = checksum_received == calculate_checksum(body[:-4])
    if not checksum_valid:
        raise FrameValidationError("invalid_checksum")

    return ParsedFrame(
        raw_frame=raw_frame,
        raw_ascii=raw_ascii,
        ver=ver,
        adr=adr,
        cid1=cid1,
        cid2_or_rtn=cid2_or_rtn,
        lenid=lenid,
        info_hex=info_hex_bytes.decode("ascii").upper(),
        info=info,
        checksum_received=checksum_received,
        checksum_valid=True,
    )


class ResponseCorrelator:
    """Pair passive responses to recent requests by ADR and reception order."""

    def __init__(self, timeout_seconds: float = 2.0, max_pending_per_adr: int = 16):
        if timeout_seconds <= 0 or max_pending_per_adr <= 0:
            raise ValueError("correlator limits must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.max_pending_per_adr = int(max_pending_per_adr)
        self._pending: dict[int, Deque[tuple[float, int]]] = defaultdict(deque)

    def _expire(self, now: float) -> None:
        for adr in tuple(self._pending):
            queue = self._pending[adr]
            while queue and now - queue[0][0] > self.timeout_seconds:
                queue.popleft()
            if not queue:
                del self._pending[adr]

    def observe(self, frame: ParsedFrame, received_at_monotonic: float) -> Correlation:
        now = float(received_at_monotonic)
        self._expire(now)
        if frame.is_request:
            queue = self._pending[frame.adr]
            queue.append((now, frame.cid2_or_rtn))
            while len(queue) > self.max_pending_per_adr:
                queue.popleft()
            return Correlation(frame, frame.cid2_or_rtn, False, now)

        queue = self._pending.get(frame.adr)
        if not queue:
            return Correlation(frame, None, False, None)
        request_timestamp, command = queue.popleft()
        if not queue:
            del self._pending[frame.adr]
        return Correlation(frame, command, True, request_timestamp)


def _signed_16(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


def decode_0x92(correlation: Correlation) -> dict:
    """Decode a successfully correlated 0x92 management response.

    Voltage uses the documented millivolt scale; current uses 0.1 A.  Both
    current fields retain the transmitted unsigned word and its signed
    two's-complement interpretation. Unknown trailing bytes remain available.
    """
    frame = correlation.frame
    quality = {
        "checksum_valid": frame.checksum_valid,
        "frame_complete": frame.frame_complete,
        "request_matched": correlation.request_matched,
        "decoder_supported": False,
        "source": frame.source,
    }
    if correlation.paired_command != 0x92 or frame.is_request:
        raise ValueError("frame is not a correlated 0x92 response")
    if frame.rtn != 0:
        return {
            "adr": frame.adr,
            "rtn": frame.rtn,
            **quality,
            "protocol_reference": frame.protocol_reference,
            "decoder_version": frame.decoder_version,
            "decode_error": "nonzero_rtn",
            "info_raw": frame.info_hex,
        }
    if len(frame.info) < 10:
        return {
            "adr": frame.adr,
            "rtn": frame.rtn,
            **quality,
            "protocol_reference": frame.protocol_reference,
            "decoder_version": frame.decoder_version,
            "decode_error": "info_too_short",
            "info_raw": frame.info_hex,
        }

    command_value = frame.info[0]
    cvl_raw = int.from_bytes(frame.info[1:3], "big")
    dvl_raw = int.from_bytes(frame.info[3:5], "big")
    ccl_raw = int.from_bytes(frame.info[5:7], "big")
    dcl_raw = int.from_bytes(frame.info[7:9], "big")
    ccl_signed = _signed_16(ccl_raw)
    dcl_signed = _signed_16(dcl_raw)
    status = frame.info[9]
    return {
        "adr": frame.adr,
        "rtn": frame.rtn,
        "command_value": command_value,
        "charge_voltage_limit_raw": cvl_raw,
        "charge_voltage_limit_v": cvl_raw / 1000.0,
        "discharge_voltage_limit_raw": dvl_raw,
        "discharge_voltage_limit_v": dvl_raw / 1000.0,
        "charge_current_limit_raw": ccl_raw,
        "charge_current_limit_a": ccl_signed / 10.0,
        "charge_current_limit_magnitude_a": abs(ccl_signed) / 10.0,
        "discharge_current_limit_raw": dcl_raw,
        "discharge_current_limit_a": dcl_signed / 10.0,
        "discharge_current_limit_magnitude_a": abs(dcl_signed) / 10.0,
        "status_raw": status,
        "charge_enable": bool(status & 0x80),
        "discharge_enable": bool(status & 0x40),
        "charge_immediately_1": bool(status & 0x20),
        "charge_immediately_2": bool(status & 0x10),
        "full_charge_request": bool(status & 0x08),
        "unknown_status_bits_raw": status & 0x07,
        "unknown_trailing_info": frame.info[10:].hex().upper(),
        **{**quality, "decoder_supported": True},
        "protocol_reference": frame.protocol_reference,
        "decoder_version": frame.decoder_version,
        "field_provenance": {
            "structure": "protocol_verified",
            "hycube_0x92_observation": "empirically_verified",
            "causal_interpretation": "unknown",
        },
        "info_raw": frame.info_hex,
    }


def _serial_quality(frame: ParsedFrame, request_matched: bool) -> dict:
    return {
        "adr": frame.adr,
        "checksum_valid": frame.checksum_valid,
        "frame_complete": frame.frame_complete,
        "request_matched": request_matched,
        "decoder_supported": False,
        "source": frame.source,
        "protocol_reference": frame.protocol_reference,
        "decoder_version": frame.decoder_version,
        "info_raw": frame.info_hex,
    }


def _serial_command_error(adr: int, command: int) -> str | None:
    if not 0x01 <= command <= 0x08:
        return "command_out_of_range"
    if command != adr:
        return "command_adr_mismatch"
    return None


def decode_0x93_request(frame: ParsedFrame) -> dict:
    """Validate the documented one-byte 0x93 serial-number request INFO."""
    if not frame.is_request or frame.command != 0x93:
        raise ValueError("frame is not a 0x93 request")
    result = _serial_quality(frame, False)
    if len(frame.info) != 1:
        return {**result, "decode_error": "invalid_info_length"}
    command = frame.info[0]
    error = _serial_command_error(frame.adr, command)
    if error:
        return {**result, "command": command, "decode_error": error}
    return {**result, "command": command, "decoder_supported": True}


def decode_0x93_info(adr: int, info: bytes) -> dict:
    """Decode documented 0x93 DATAI bytes independently of record age."""
    if not isinstance(info, bytes):
        raise TypeError("info must be bytes")
    if len(info) != 17:
        return {"decoder_supported": False, "decode_error": "invalid_info_length"}
    command = info[0]
    error = _serial_command_error(int(adr), command)
    if error:
        return {
            "command": command, "decoder_supported": False,
            "decode_error": error,
        }
    serial_bytes = info[1:]
    try:
        serial_string = serial_bytes.decode("ascii")
    except UnicodeDecodeError:
        return {
            "command": command,
            "serial_raw": serial_bytes.hex().upper(),
            "decoder_supported": False,
            "decode_error": "serial_not_ascii",
        }
    return {
        "command": command,
        "serial_raw": serial_bytes.hex().upper(),
        "serial_string": serial_string,
        "decoder_supported": True,
    }


def decode_0x93(correlation: Correlation) -> dict:
    """Decode a V3.3 0x93 response without inferring a module position.

    DATAI is exactly one command byte followed by exactly 16 ASCII serial
    bytes. The serial bytes are retained as uppercase hexadecimal and decoded
    byte-for-byte; no trimming, padding removal, or normalization is applied.
    """
    frame = correlation.frame
    if frame.is_request:
        raise ValueError("frame is not a 0x93 response")
    result = _serial_quality(frame, correlation.request_matched)
    if correlation.paired_command != 0x93 or not correlation.request_matched:
        return {**result, "rtn": frame.rtn, "decode_error": "unmatched_response"}
    if frame.rtn != 0:
        return {**result, "rtn": frame.rtn, "decode_error": "nonzero_rtn"}
    decoded = decode_0x93_info(frame.adr, frame.info)
    return {
        **result,
        "rtn": frame.rtn,
        **decoded,
    }


LOG = logging.getLogger("guardian_battery.rs485")


def open_passive_serial(port: str, baudrate: int, timeout: float):
    """Open pyserial with inactive control-line state and no RS485 TX mode.

    pyserial applies the stored DTR/RTS states during ``open()``.  Setting both
    before opening avoids intentionally asserting them. Driver-level glitches
    during open remain hardware-dependent and require a real adapter test.
    """
    connection = serial.Serial(
        port=None, baudrate=baudrate, timeout=timeout,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, xonxoff=False, rtscts=False,
        dsrdtr=False,
    )
    connection.rts = False
    connection.dtr = False
    connection.rs485_mode = None
    connection.port = port
    connection.open()
    return connection


class PassiveRs485Reader:
    """Isolated, reconnecting, read-only runtime consumer."""

    def __init__(
        self, port_resolver, baudrate: int = 115200, *, serial_opener=open_passive_serial,
        frame_callback=None, read_size: int = 4096, read_timeout: float = 0.25,
        reconnect_delays: tuple[float, ...] = (1.0, 2.0, 5.0, 30.0),
        max_adr_states: int = 256, wall_clock=time.time, monotonic=time.monotonic,
    ):
        if read_size <= 0 or max_adr_states <= 0 or not reconnect_delays:
            raise ValueError("reader limits must be positive")
        self.port_resolver = port_resolver
        self.baudrate = int(baudrate)
        self.serial_opener = serial_opener
        self.frame_callback = frame_callback
        self.read_size = int(read_size)
        self.read_timeout = float(read_timeout)
        self.reconnect_delays = tuple(float(value) for value in reconnect_delays)
        self.max_adr_states = int(max_adr_states)
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection = None
        self._lock = threading.Lock()
        self._framer = StreamFramer()
        self._correlator = ResponseCorrelator()
        self._first_valid_logged = False
        self._first_0x92_logged = False
        self.latest_management_by_adr: dict[int, dict] = {}
        self._status = {
            "enabled": True, "state": "starting", "resolved_port": None,
            "baudrate": self.baudrate, "last_byte_at": None, "last_frame_at": None,
            "last_valid_frame_at": None, "last_0x92_at": None, "frames_total": 0,
            "valid_frames": 0, "checksum_errors": 0, "frame_errors": 0,
            "requests_0x92": 0, "responses_0x92": 0, "unmatched_responses": 0,
            "requests_0x44": 0, "responses_0x44": 0,
            "last_error": None,
        }

    def status(self) -> dict:
        with self._lock:
            return {**self._status, "latest_management_adrs": sorted(self.latest_management_by_adr)}

    def management(self) -> dict[int, dict]:
        with self._lock:
            return {adr: dict(value) for adr, value in self.latest_management_by_adr.items()}

    def _set(self, **values) -> None:
        with self._lock:
            self._status.update(values)

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guardian-rs485-passive", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        self._connection = None
        self._set(state="disabled")

    def _process(self, chunk: bytes) -> None:
        if not chunk:
            return
        now = self.wall_clock()
        self._set(last_byte_at=now)
        for raw in self._framer.feed(chunk):
            self._set(frames_total=self.status()["frames_total"] + 1, last_frame_at=now)
            try:
                frame = parse_frame(raw)
            except FrameValidationError as exc:
                status = self.status()
                key = "checksum_errors" if exc.reason == "invalid_checksum" else "frame_errors"
                self._set(**{key: status[key] + 1})
                continue
            status = self.status()
            self._set(valid_frames=status["valid_frames"] + 1, last_valid_frame_at=now)
            if not self._first_valid_logged:
                LOG.info(
                    "RS485 first valid frame: adr=%02X cid1=%02X cid2_or_rtn=%02X",
                    frame.adr, frame.cid1, frame.cid2_or_rtn,
                )
                self._first_valid_logged = True
            correlation = self._correlator.observe(frame, self.monotonic())
            if frame.is_request and frame.command == 0x92:
                self._set(requests_0x92=self.status()["requests_0x92"] + 1)
            elif frame.is_request and frame.command == 0x44:
                self._set(requests_0x44=self.status()["requests_0x44"] + 1)
            elif not frame.is_request:
                if not correlation.request_matched:
                    self._set(unmatched_responses=self.status()["unmatched_responses"] + 1)
                if correlation.paired_command == 0x92:
                    self._set(responses_0x92=self.status()["responses_0x92"] + 1,
                              last_0x92_at=now)
                    decoded = decode_0x92(correlation)
                    if decoded.get("decoder_supported"):
                        value = {"timestamp": now, "raw_frame": raw, **decoded}
                        with self._lock:
                            if frame.adr not in self.latest_management_by_adr \
                                    and len(self.latest_management_by_adr) >= self.max_adr_states:
                                oldest = min(self.latest_management_by_adr,
                                             key=lambda adr: self.latest_management_by_adr[adr]["timestamp"])
                                del self.latest_management_by_adr[oldest]
                            self.latest_management_by_adr[frame.adr] = value
                        if not self._first_0x92_logged:
                            LOG.info(
                                "RS485 first valid 0x92: adr=%02X ccl=%.1fA dcl=%.1fA "
                                "charge_enable=%s discharge_enable=%s",
                                frame.adr, decoded["charge_current_limit_a"],
                                decoded["discharge_current_limit_a"],
                                decoded["charge_enable"], decoded["discharge_enable"],
                            )
                            self._first_0x92_logged = True
                elif correlation.paired_command == 0x44:
                    self._set(responses_0x44=self.status()["responses_0x44"] + 1)
            if self.frame_callback:
                self.frame_callback(frame, correlation)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            connection = None
            try:
                self._set(state="starting" if attempt == 0 else "reconnecting", last_error=None)
                port = self.port_resolver()
                self._set(resolved_port=port)
                connection = self.serial_opener(port, self.baudrate, self.read_timeout)
                self._connection = connection
                attempt = 0
                self._set(state="listening")
                LOG.info("RS485 passive sniffer listening: port=%s baud=%s", port, self.baudrate)
                while not self._stop.is_set():
                    self._process(connection.read(self.read_size))
            except Exception as exc:
                code = getattr(exc, "code", "error")
                state = {
                    "rs485_port_unavailable": "unavailable",
                    "ambiguous_rs485_port": "ambiguous_port",
                    "serial_role_conflict": "serial_role_conflict",
                }.get(code, "error")
                self._set(state=state, last_error=f"{code}: {exc}")
                LOG.warning("RS485 passive reader unavailable: %s", exc)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                self._connection = None
            if self._stop.is_set():
                break
            delay = self.reconnect_delays[min(attempt, len(self.reconnect_delays) - 1)]
            attempt += 1
            self._stop.wait(delay)
