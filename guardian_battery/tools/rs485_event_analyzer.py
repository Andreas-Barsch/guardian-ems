#!/usr/bin/env python3
"""Read-only analyzer for Guardian passive RS485 evidence JSONL."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


MANAGEMENT_FIELDS = (
    "charge_current_limit_a", "discharge_current_limit_a",
    "charge_enable", "discharge_enable",
    "charge_voltage_limit_v", "discharge_voltage_limit_v",
    "charge_immediately_1", "charge_immediately_2", "full_charge_request",
)
DISPLAY_FIELDS = MANAGEMENT_FIELDS[:6]


def parse_timestamp(value: str) -> datetime:
    """Parse an offset-aware ISO-8601 timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp requires an explicit UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc)


def parse_int_list(value: str | None) -> set[int] | None:
    if not value:
        return None
    return {int(part.strip(), 0) for part in value.split(",") if part.strip()}


def _valid_frame(record: dict) -> bool:
    return record.get("checksum_valid") is True and record.get("frame_complete") is True


def _is_response(record: dict, command: int) -> bool:
    return (record.get("record_type") == "frame" and record.get("direction") == "response"
            and record.get("paired_command") == command)


def _valid_0x92(record: dict) -> bool:
    return (_valid_frame(record) and record.get("request_matched") is True
            and record.get("decoder_supported") is True
            and isinstance(record.get("decoded"), dict))


def _valid_0x44(record: dict) -> bool:
    return _valid_frame(record) and isinstance(record.get("info_raw"), str)


def _valid_0x93(record: dict) -> bool:
    decoded = record.get("decoded")
    if not (_valid_frame(record) and record.get("request_matched") is True
            and record.get("decoder_supported") is True and isinstance(decoded, dict)):
        return False
    try:
        adr = int(record["adr"])
        command = int(decoded["command"])
        serial_raw = bytes.fromhex(decoded["serial_raw"])
        info_raw = bytes.fromhex(record["info_raw"])
        serial_string = decoded["serial_string"]
        serial_encoded = serial_string.encode("ascii")
    except (KeyError, TypeError, ValueError, UnicodeEncodeError, AttributeError):
        return False
    return (len(serial_raw) == 16 and len(info_raw) == 17
            and info_raw == bytes([command]) + serial_raw
            and 0x01 <= command <= 0x08 and command == adr
            and isinstance(serial_string, str) and serial_encoded == serial_raw)


def _management_state(record: dict) -> tuple:
    decoded = record["decoded"]
    return tuple(decoded.get(field) for field in MANAGEMENT_FIELDS)


def _hex_info(record: dict) -> bytes | None:
    try:
        return bytes.fromhex(record["info_raw"])
    except (KeyError, TypeError, ValueError):
        return None


def _local_time(timestamp: datetime, output_timezone) -> str:
    return timestamp.astimezone(output_timezone).strftime("%H:%M:%S.%f")[:-3]


def _number(value, unit: str, signed=False) -> str:
    if value is None:
        return "—"
    prefix = "+" if signed and float(value) >= 0 else ""
    return f"{prefix}{float(value):.1f} {unit}"


def _management_line(timestamp, adr, decoded, output_timezone, marker=None) -> str:
    head = f"{_local_time(timestamp, output_timezone)} | ADR {adr:02X}"
    if marker:
        head += f" | 0x92 {marker}"
    enabled = lambda value: "ENABLED" if value is True else "STOP REQUEST" if value is False else "—"
    return (f"{head} | CCL {_number(decoded.get('charge_current_limit_a'), 'A', True)}"
            f" | DCL {_number(decoded.get('discharge_current_limit_a'), 'A', True)}"
            f" | CHG {enabled(decoded.get('charge_enable'))}"
            f" | DSG {enabled(decoded.get('discharge_enable'))}"
            f" | CVL {_number(decoded.get('charge_voltage_limit_v'), 'V')}"
            f" | DVL {_number(decoded.get('discharge_voltage_limit_v'), 'V')}")


def _state_change_line(timestamp, record, output_timezone) -> str:
    return (f"{_local_time(timestamp, output_timezone)} | ADR {int(record['adr']):02X}"
            f" | CHANGE | {record.get('field', 'unknown')}"
            f" | {record.get('old_value')!r} -> {record.get('new_value')!r}")


def _byte_diff(old: bytes, new: bytes) -> list[dict]:
    changes = []
    for offset in range(max(len(old), len(new))):
        before = old[offset] if offset < len(old) else None
        after = new[offset] if offset < len(new) else None
        if before != after:
            changes.append({
                "offset": offset,
                "old": "--" if before is None else f"{before:02X}",
                "new": "--" if after is None else f"{after:02X}",
            })
    return changes


def _byte_change_line(timestamp, adr, old, new, changes, output_timezone) -> str:
    details = ", ".join(
        f"offset={item['offset']} old={item['old']} new={item['new']}" for item in changes)
    return (f"{_local_time(timestamp, output_timezone)} | ADR {adr:02X} | 0x44 CHANGE"
            f" | info_length={len(new)} | changed_bytes={len(changes)} | {details}")


def _serial_line(timestamp, adr, serial, output_timezone, marker=None, old=None) -> str:
    head = f"{_local_time(timestamp, output_timezone)} | ADR {adr:02X} | 0x93 SERIAL"
    if marker == "CHANGE":
        return f"{head} CHANGE | old={old} -> new={serial}"
    if marker:
        head += f" {marker}"
    return f"{head} | {serial}"


def analyze_file(path, timestamp_from, timestamp_to, *, adrs=None, commands=None,
                 changes_only=False) -> dict:
    """Analyze one JSONL file without modifying it or any Guardian state."""
    start = parse_timestamp(timestamp_from) if isinstance(timestamp_from, str) else timestamp_from
    end = parse_timestamp(timestamp_to) if isinstance(timestamp_to, str) else timestamp_to
    if start >= end:
        raise ValueError("--from must be earlier than --to")
    output_timezone = (datetime.fromisoformat(timestamp_from.replace("Z", "+00:00")).tzinfo
                       if isinstance(timestamp_from, str) else start.tzinfo)
    commands = {0x44, 0x92, 0x93} if commands is None else set(commands)
    records = []
    counters = {"lines_examined": 0, "malformed_json": 0, "invalid_timestamp": 0,
                "invalid_evidence": 0}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            counters["lines_examined"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                counters["malformed_json"] += 1
                continue
            try:
                timestamp = parse_timestamp(record.get("timestamp"))
            except ValueError:
                counters["invalid_timestamp"] += 1
                continue
            try:
                adr = int(record.get("adr"))
            except (TypeError, ValueError):
                counters["invalid_evidence"] += 1
                continue
            if adrs is not None and adr not in adrs:
                continue
            records.append((timestamp, line_number, adr, record))
    records.sort(key=lambda item: (item[0], item[1]))

    events = []
    totals = {"valid_0x92": 0, "valid_0x44": 0, "changes_0x44": 0,
              "valid_0x93": 0, "changes_0x93": 0, "state_changes": 0}
    per_adr = defaultdict(lambda: {"responses_0x92": 0, "responses_0x44": 0,
                                   "changes_0x44": 0, "serial": None,
                                   "serial_observations": 0, "serial_changes": 0,
                                   "last_management": None})
    last_44 = {}
    last_92 = {}
    last_93 = {}
    visible_92 = set()
    observed = set()

    for timestamp, line_number, adr, record in records:
        in_window = start <= timestamp <= end
        if record.get("record_type") == "state_change":
            if in_window and 0x92 in commands:
                totals["state_changes"] += 1
                observed.add(adr)
                events.append({"timestamp": timestamp, "line_number": line_number,
                               "type": "state_change", "adr": adr, "record": record})
            continue

        if _is_response(record, 0x44) and 0x44 in commands:
            info = _hex_info(record) if _valid_0x44(record) else None
            if info is None:
                if in_window:
                    counters["invalid_evidence"] += 1
                continue
            previous = last_44.get(adr)
            changes = _byte_diff(previous, info) if previous is not None else []
            last_44[adr] = info
            if not in_window:
                continue
            observed.add(adr)
            totals["valid_0x44"] += 1
            per_adr[adr]["responses_0x44"] += 1
            if changes:
                totals["changes_0x44"] += 1
                per_adr[adr]["changes_0x44"] += 1
                events.append({"timestamp": timestamp, "line_number": line_number,
                               "type": "0x44_change", "adr": adr, "old": previous,
                               "new": info, "changes": changes})
            continue

        if _is_response(record, 0x92) and 0x92 in commands:
            if not _valid_0x92(record):
                if in_window:
                    counters["invalid_evidence"] += 1
                continue
            state = _management_state(record)
            previous = last_92.get(adr)
            last_92[adr] = state
            if not in_window:
                continue
            observed.add(adr)
            totals["valid_0x92"] += 1
            per_adr[adr]["responses_0x92"] += 1
            per_adr[adr]["last_management"] = {
                field: record["decoded"].get(field) for field in MANAGEMENT_FIELDS
            }
            marker = None
            emit = True
            if changes_only:
                if adr not in visible_92 and previous is None:
                    marker = "BASELINE"
                elif previous == state:
                    emit = False
                else:
                    marker = "CHANGE"
            visible_92.add(adr)
            if emit:
                events.append({"timestamp": timestamp, "line_number": line_number,
                               "type": "0x92", "adr": adr,
                               "decoded": record["decoded"], "marker": marker})
            continue

        if _is_response(record, 0x93) and 0x93 in commands:
            if not _valid_0x93(record):
                if in_window:
                    counters["invalid_evidence"] += 1
                continue
            serial = record["decoded"]["serial_string"]
            previous = last_93.get(adr)
            last_93[adr] = serial
            if not in_window:
                continue
            observed.add(adr)
            totals["valid_0x93"] += 1
            values = per_adr[adr]
            values["serial"] = serial
            values["serial_observations"] += 1
            marker = "BASELINE" if previous is None else None
            if previous is not None and previous != serial:
                marker = "CHANGE"
                totals["changes_0x93"] += 1
                values["serial_changes"] += 1
            if not changes_only or marker is not None:
                events.append({"timestamp": timestamp, "line_number": line_number,
                               "type": "0x93", "adr": adr, "serial": serial,
                               "old": previous, "marker": marker})

    events.sort(key=lambda item: (item["timestamp"], item["line_number"], item["type"]))
    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "output_timezone": output_timezone,
        "events": events, "counters": counters, "totals": totals,
        "adrs_observed": sorted(observed), "per_adr": dict(sorted(per_adr.items())),
    }


def render_text(result: dict) -> str:
    timezone_used = result["output_timezone"]
    lines = []
    for event in result["events"]:
        if event["type"] == "0x92":
            lines.append(_management_line(event["timestamp"], event["adr"], event["decoded"],
                                          timezone_used, event["marker"]))
        elif event["type"] == "state_change":
            lines.append(_state_change_line(event["timestamp"], event["record"], timezone_used))
        elif event["type"] == "0x93":
            lines.append(_serial_line(event["timestamp"], event["adr"], event["serial"],
                                      timezone_used, event["marker"], event["old"]))
        else:
            lines.append(_byte_change_line(event["timestamp"], event["adr"], event["old"],
                                           event["new"], event["changes"], timezone_used))
    start = datetime.fromisoformat(result["window"]["from"]).astimezone(timezone_used)
    end = datetime.fromisoformat(result["window"]["to"]).astimezone(timezone_used)
    totals, counters = result["totals"], result["counters"]
    lines.extend(["", "Summary:",
                  f"Window: {start.isoformat()} – {end.isoformat()}",
                  f"Records examined: {counters['lines_examined']}",
                  f"Invalid records: {counters['malformed_json'] + counters['invalid_timestamp'] + counters['invalid_evidence']}",
                  f"Valid 0x92 responses: {totals['valid_0x92']}",
                  f"Valid 0x44 responses: {totals['valid_0x44']}",
                  f"0x44 changes: {totals['changes_0x44']}",
                  f"Valid 0x93 responses: {totals['valid_0x93']}",
                  f"0x93 serial changes: {totals['changes_0x93']}",
                  f"State changes: {totals['state_changes']}",
                  "ADRs observed: " + (", ".join(f"{adr:02X}" for adr in result["adrs_observed"]) or "none")])
    for adr, values in result["per_adr"].items():
        latest = values["last_management"] or {}
        enabled = lambda value: "ENABLED" if value is True else "STOP REQUEST" if value is False else "—"
        lines.extend([f"ADR {adr:02X}:",
                      f"  0x92 responses: {values['responses_0x92']}",
                      f"  0x44 responses: {values['responses_0x44']}",
                      f"  0x44 changes: {values['changes_0x44']}",
                      f"  serial: {values['serial'] if values['serial'] is not None else 'unknown'}",
                      f"  serial observations: {values['serial_observations']}",
                      f"  serial changes: {values['serial_changes']}",
                      f"  last CCL: {_number(latest.get('charge_current_limit_a'), 'A', True)}",
                      f"  last DCL: {_number(latest.get('discharge_current_limit_a'), 'A', True)}",
                      f"  last CHG: {enabled(latest.get('charge_enable'))}",
                      f"  last DSG: {enabled(latest.get('discharge_enable'))}"])
    return "\n".join(lines)


def _json_safe(result: dict) -> dict:
    def convert(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bytes):
            return value.hex().upper()
        if isinstance(value, dict):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value
    safe = convert(result)
    safe.pop("output_timezone", None)
    return safe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="Guardian RS485 JSONL file")
    parser.add_argument("--from", dest="timestamp_from", required=True,
                        help="inclusive offset-aware ISO-8601 start")
    parser.add_argument("--to", dest="timestamp_to", required=True,
                        help="inclusive offset-aware ISO-8601 end")
    parser.add_argument("--adr", help="comma-separated ADRs, e.g. 2,3,4")
    parser.add_argument("--commands", help="comma-separated commands, e.g. 0x44,0x92,0x93")
    parser.add_argument("--changes-only", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = analyze_file(args.file, args.timestamp_from, args.timestamp_to,
                              adrs=parse_int_list(args.adr),
                              commands=parse_int_list(args.commands),
                              changes_only=args.changes_only)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
