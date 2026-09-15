"""Bounded serial-centric reads of authoritative Guardian cell history."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import statistics
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maintenance import normalize_utc_timestamp
from history_block_index import index_path, selected_ranges

MAX_RECORDS = 10_000
MAX_POINTS = 6_000
MAX_CELLS = 15
FULL_DEFAULT_SECONDS = 86400
FULL_MAX_SECONDS = 7 * 86400
DISPLAY_MAX_SECONDS = 90 * 86400
_SERIAL_TOKEN_BYTES = re.compile(rb'"module_serial"\s*:\s*("(?:[^"\\]|\\.)*")')
RANGE_READ_CHUNK_BYTES = 256 * 1024


def iter_binary_range_lines(handle, range_start, range_end, *,
                            chunk_size=RANGE_READ_CHUNK_BYTES, profile=None,
                            file_profile=None, deadline=None, io_profile=None):
    """Yield legacy-equivalent binary lines from one bounded byte range."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    started = time.perf_counter(); handle.seek(range_start)
    if io_profile is not None:
        io_profile["timings_seconds"]["source_open_range_seek"] += (
            time.perf_counter() - started)
    if profile is not None:
        profile["stages_seconds"]["range_seek"] += time.perf_counter() - started
    remaining = max(0, range_end - range_start)
    carry = bytearray()
    while remaining:
        started = time.perf_counter()
        chunk = handle.read(min(chunk_size, remaining))
        if io_profile is not None:
            io_profile["timings_seconds"]["raw_chunk_read"] += (
                time.perf_counter() - started)
            io_profile["raw_chunk_reads"] += 1
        if profile is not None:
            profile["stages_seconds"]["raw_chunk_read"] += time.perf_counter() - started
            profile["counts"]["raw_chunk_reads"] += 1
            profile["counts"]["raw_bytes_read"] += len(chunk)
            file_profile["raw_bytes_read"] += len(chunk)
        if not chunk:
            break
        started = time.perf_counter()
        deadline_exceeded = deadline is not None and time.monotonic() > deadline
        if io_profile is not None:
            io_profile["timings_seconds"]["deadline_check"] += (
                time.perf_counter() - started)
        if profile is not None:
            profile["stages_seconds"]["deadline_check"] += time.perf_counter() - started
        if deadline_exceeded:
            raise ResearchQueryError("timeout", "research query timed out", 503)
        remaining -= len(chunk)
        framing_started = time.perf_counter() if io_profile is not None else None
        parts = chunk.split(b"\n")
        if len(parts) == 1:
            carry.extend(chunk)
            if io_profile is not None:
                io_profile["timings_seconds"]["binary_line_framing"] += (
                    time.perf_counter() - framing_started)
            continue
        carry.extend(parts[0]); framed = bytes(carry) + b"\n"; carry.clear()
        if io_profile is not None:
            io_profile["timings_seconds"]["binary_line_framing"] += (
                time.perf_counter() - framing_started)
        yield framed
        for part in parts[1:-1]:
            framing_started = time.perf_counter() if io_profile is not None else None
            framed = part + b"\n"
            if io_profile is not None:
                io_profile["timings_seconds"]["binary_line_framing"] += (
                    time.perf_counter() - framing_started)
            yield framed
        framing_started = time.perf_counter() if io_profile is not None else None
        carry.extend(parts[-1])
        if io_profile is not None:
            io_profile["timings_seconds"]["binary_line_framing"] += (
                time.perf_counter() - framing_started)
    if carry:
        # The index contract is line-aligned. Preserve legacy readline behavior
        # fail-closed even for an externally supplied partial range boundary.
        started = time.perf_counter(); tail = handle.readline()
        if io_profile is not None:
            io_profile["timings_seconds"]["raw_chunk_read"] += (
                time.perf_counter() - started)
            io_profile["raw_chunk_reads"] += 1
        if profile is not None:
            profile["stages_seconds"]["range_tail_read"] += time.perf_counter() - started
            profile["counts"]["range_tail_reads"] += 1
            profile["counts"]["raw_bytes_read"] += len(tail)
            file_profile["raw_bytes_read"] += len(tail)
        framing_started = time.perf_counter() if io_profile is not None else None
        carry.extend(tail); framed = bytes(carry)
        if io_profile is not None:
            io_profile["timings_seconds"]["binary_line_framing"] += (
                time.perf_counter() - framing_started)
        yield framed


class ResearchQueryError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


class CursorCodec:
    """Signed opaque cursor bound to the complete normalized query."""
    def __init__(self, secret): self.secret = secret
    def encode(self, query_hash, offset):
        raw = json.dumps({"q": query_hash, "o": offset}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(
            raw + hmac.new(self.secret, raw, hashlib.sha256).digest()).decode().rstrip("=")
    def decode(self, token, query_hash):
        try:
            blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            raw, signature = blob[:-32], blob[-32:]
            if not hmac.compare_digest(signature, hmac.new(self.secret, raw, hashlib.sha256).digest()):
                raise ValueError
            value = json.loads(raw)
            if value["q"] != query_hash or type(value["o"]) is not int or value["o"] < 0:
                raise ValueError
            return value["o"]
        except Exception as exc:
            raise ResearchQueryError("cursor_invalid", "cursor is invalid or belongs to another query") from exc


class ResearchTimeseriesService:
    METRICS = frozenset({"soc", "module_voltage", "module_current",
        "module_temperature", "cell_voltage", "cell_temperature", "cell_deviation",
        "cell_spread"})

    def __init__(self, directory, identity_resolver, cursor_codec):
        self.directory = Path(directory)
        self.identity = identity_resolver
        self.cursor = cursor_codec

    @staticmethod
    def normalize_range(timestamp_from, timestamp_to):
        try:
            start = normalize_utc_timestamp(timestamp_from, "from")
            end = normalize_utc_timestamp(timestamp_to, "to")
        except Exception as exc:
            raise ResearchQueryError("invalid_argument", "from/to must be timezone-aware ISO-8601") from exc
        if datetime.fromisoformat(start) > datetime.fromisoformat(end):
            raise ResearchQueryError("invalid_argument", "from must not exceed to")
        return start, end

    def _paths(self, start, end):
        day = datetime.fromisoformat(start).astimezone(timezone.utc).date()
        last = datetime.fromisoformat(end).astimezone(timezone.utc).date()
        while day <= last:
            path = self.directory / f"{day.isoformat()}.jsonl"
            if path.is_file(): yield path
            day += timedelta(days=1)

    @staticmethod
    def _bounded_ranges(path, start_epoch, end_epoch):
        present = index_path(path).is_file()
        try:
            ranges, _ = selected_ranges(path, start_epoch, end_epoch,
                timestamp_field="timestamp", iso_timestamp=False)
            return ranges, present, True, "indexed_chunk"
        except Exception:
            return ((0, path.stat().st_size),), present, False, "full_scan_fallback"

    def _soc_candidates(self, physical_serial, start, end, *, deadline=None,
                        io_profile=None, end_exclusive_epoch=None):
        """Read validated SOC observations without eagerly projecting identity."""
        start_epoch = datetime.fromisoformat(start).timestamp()
        end_epoch = datetime.fromisoformat(end).timestamp()
        selected_paths = list(self._paths(start, end))
        if io_profile is not None:
            io_profile.update({"files_discovered": len(selected_paths), "files_opened": 0,
                "bytes_read": 0, "records_inspected": 0, "samples_returned": 0,
                "index_present": False, "index_valid": True, "read_mode": "indexed_chunk",
                "selected_bytes": 0, "full_json_decode_count": 0,
                "records_skipped_serial_prefilter": 0, "serial_at_calls": 0,
                "position_at_calls": 0, "materialized_output_points": 0,
                "additional_source_records": 0, "reused_evidence_records": 0})
        signatures = []
        candidates = []
        for path in selected_paths:
            stat = path.stat(); signatures.append((path.name, stat.st_size, stat.st_mtime_ns))
            ranges, present, valid, mode = self._bounded_ranges(path, start_epoch, end_epoch)
            if io_profile is not None:
                io_profile["index_present"] = io_profile["index_present"] or present
                io_profile["index_valid"] = io_profile["index_valid"] and valid
                io_profile["selected_bytes"] += sum(stop - begin for begin, stop in ranges)
                if mode != "indexed_chunk": io_profile["read_mode"] = mode
            with path.open("rb") as handle:
                if io_profile is not None: io_profile["files_opened"] += 1
                for range_start, range_end in ranges:
                    for line in iter_binary_range_lines(handle, range_start, range_end,
                                                        deadline=deadline):
                        if io_profile is not None:
                            io_profile["bytes_read"] += len(line)
                            io_profile["records_inspected"] += 1
                        if deadline is not None and time.monotonic() > deadline:
                            raise ResearchQueryError("timeout", "research query timed out", 503)
                        serial_token = _SERIAL_TOKEN_BYTES.search(line)
                        if serial_token is not None:
                            try: explicit_serial = json.loads(serial_token.group(1))
                            except (UnicodeDecodeError, json.JSONDecodeError): explicit_serial = None
                            if isinstance(explicit_serial, str) and explicit_serial != physical_serial:
                                if io_profile is not None:
                                    io_profile["records_skipped_serial_prefilter"] += 1
                                continue
                        try:
                            record = json.loads(line); epoch = float(record["timestamp"])
                        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                            continue
                        if io_profile is not None: io_profile["full_json_decode_count"] += 1
                        if not start_epoch <= epoch <= end_epoch:
                            continue
                        if end_exclusive_epoch is not None and epoch >= end_exclusive_epoch:
                            continue
                        observed_serial = record.get("module_serial")
                        direct_serial = observed_serial is not None
                        if not direct_serial:
                            position = int(record.get("module", 0))
                            if not 1 <= position <= 6:
                                continue
                            timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                            observed_serial = self.identity.serial_at(position, timestamp).get(
                                "physical_serial")
                            if io_profile is not None: io_profile["serial_at_calls"] += 1
                        if observed_serial != physical_serial:
                            continue
                        value = record.get("soc_percent")
                        if value is None: continue
                        try: value = float(value)
                        except (ValueError, TypeError): continue
                        candidates.append((epoch, value, direct_serial))
                        if io_profile is not None:
                            io_profile["additional_source_records"] += 1
        return candidates, signatures

    @staticmethod
    def _merge_source_signatures(*groups):
        by_name = {}
        for group in groups:
            for signature in group:
                by_name[signature[0]] = tuple(signature)
        return [by_name[name] for name in sorted(by_name)]

    def _project_soc_candidates(self, candidates, *, physical_serial, start, end,
                                selected_resolution, max_points, offset=0,
                                source_signature=(), evidence_identities=None,
                                io_profile=None):
        candidates.sort(key=lambda item: item[0])
        source_points = len(candidates)
        observation_times = sorted(set(item[0] for item in candidates))
        selected = candidates
        if selected_resolution == "display" and len(selected) > max_points:
            if max_points == 1: selected = selected[:1]
            else:
                step = (len(selected) - 1) / (max_points - 1)
                selected = [selected[round(index * step)] for index in range(max_points)]
        page_limit = min(max_points, MAX_RECORDS)
        selected_page = selected[offset:offset + page_limit]
        truncated = offset + len(selected_page) < len(selected)
        points = []
        for epoch, value, direct_serial in selected_page:
            timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
            cached = evidence_identities.get((epoch, value, direct_serial)) \
                if evidence_identities else None
            identity = cached or self.identity.position_at(physical_serial, timestamp)
            if io_profile is not None and cached is None: io_profile["position_at_calls"] += 1
            points.append({"timestamp": timestamp, "value": value,
                "physical_serial": physical_serial,
                "position_at_time": identity.get("position_at_time"),
                "position_history_id": identity.get("position_history_id"),
                "identity_epoch_id": identity.get("identity_epoch_id"),
                "identity_resolved": identity.get("identity_resolved", identity.get("resolved", False)),
                "identity_source": ("record_module_serial" if direct_serial else "position_history")})
        gaps = [right - left for left, right in zip(observation_times, observation_times[1:])
                if right >= left]
        first = (datetime.fromtimestamp(min(observation_times), timezone.utc).isoformat()
                 if observation_times else None)
        last = (datetime.fromtimestamp(max(observation_times), timezone.utc).isoformat()
                if observation_times else None)
        cadence = statistics.median(gaps) if gaps else None
        gap_limit = cadence * 3 if cadence else None
        missing = [{"from": datetime.fromtimestamp(left, timezone.utc).isoformat(),
                    "to": datetime.fromtimestamp(right, timezone.utc).isoformat()}
                   for left, right in zip(observation_times, observation_times[1:])
                   if gap_limit is not None and right - left > gap_limit]
        covered = []
        if observation_times:
            interval_start = observation_times[0]
            for left, right in zip(observation_times, observation_times[1:]):
                if gap_limit is not None and right - left > gap_limit:
                    covered.append({"from": datetime.fromtimestamp(
                        interval_start, timezone.utc).isoformat(),
                        "to": datetime.fromtimestamp(left, timezone.utc).isoformat()})
                    interval_start = right
            covered.append({"from": datetime.fromtimestamp(
                interval_start, timezone.utc).isoformat(),
                "to": datetime.fromtimestamp(observation_times[-1], timezone.utc).isoformat()})
        coverage = {"requested_range": {"from": start, "to": end},
            "covered_intervals": covered,
            "missing_intervals": missing if observation_times else [{"from": start, "to": end}],
            "first_observation": first, "last_observation": last,
            "sample_count": len(observation_times), "expected_cadence_seconds": cadence,
            "largest_gap_seconds": max(gaps) if gaps else None,
            "quality": "complete" if observation_times and not missing and not truncated else
                       "partial" if observation_times else "absent"}
        if io_profile is not None:
            io_profile["samples_returned"] = len(points)
            io_profile["materialized_output_points"] = len(points)
        signature = [tuple(item) for item in source_signature]
        return {"metric": "soc", "physical_serial": physical_serial, "points": points,
            "point_count": len(points), "source_point_count": source_points,
            "resolution": selected_resolution, "coverage": coverage, "truncated": truncated,
            "next_cursor": None,
            "source_fingerprint": hashlib.sha256(json.dumps(signature).encode()).hexdigest()}

    def soc_query_with_evidence(self, *, physical_serial, timestamp_from, timestamp_to,
                                reusable_evidence, reusable_from, resolution="auto",
                                max_points=MAX_POINTS, deadline=None, io_profile=None):
        """Build one SOC query from an older scan plus reusable recent evidence."""
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        reuse_start = normalize_utc_timestamp(reusable_from, "reusable_from")
        reuse_epoch = datetime.fromisoformat(reuse_start).timestamp()
        candidates, older_signatures = self._soc_candidates(
            physical_serial, start, reuse_start, deadline=deadline,
            io_profile=io_profile, end_exclusive_epoch=reuse_epoch)
        identities = {}
        reused = 0
        start_epoch = datetime.fromisoformat(start).timestamp()
        end_epoch = datetime.fromisoformat(end).timestamp()
        for row in reusable_evidence["records"].get(physical_serial, ()):
            epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
            if not reuse_epoch <= epoch <= end_epoch or epoch < start_epoch:
                continue
            value = row.get("soc")
            if value is None: continue
            direct = row.get("identity_source") == "record_module_serial"
            candidate = (epoch, float(value), direct)
            candidates.append(candidate); reused += 1
            identities[candidate] = row
        if io_profile is not None: io_profile["reused_evidence_records"] = reused
        signatures = self._merge_source_signatures(
            older_signatures,
            (item for item in reusable_evidence.get("source_signature", ())
             if start[:10] <= item[0][:10] <= end[:10]))
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        selected_resolution = ("full" if span <= FULL_DEFAULT_SECONDS else "display") \
            if resolution == "auto" else resolution
        return self._project_soc_candidates(candidates, physical_serial=physical_serial,
            start=start, end=end, selected_resolution=selected_resolution,
            max_points=max_points, source_signature=signatures,
            evidence_identities=identities, io_profile=io_profile)

    def soc_current_by_serial(self, physical_serials, timestamp_from, timestamp_to,
                              deadline=None, profile=None):
        """Read SOC/current for several identities in one authoritative scan."""
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        start_epoch = datetime.fromisoformat(start).timestamp()
        end_epoch = datetime.fromisoformat(end).timestamp()
        wanted = set(physical_serials)
        result = {serial: [] for serial in wanted}
        started = time.perf_counter()
        paths = list(self._paths(start, end)) if profile is not None else self._paths(start, end)
        if profile is not None:
            profile["stages_seconds"]["file_discovery"] = time.perf_counter() - started
            profile["counts"]["files_discovered"] = len(paths)
        if profile is not None:
            started = time.perf_counter()
            self.identity.epochs(timestamp_from=start, timestamp_to=end)
            profile["stages_seconds"]["identity_epoch_preparation"] = (
                time.perf_counter() - started)
        scan_started = time.perf_counter()
        try:
          for path in paths:
            file_profile = None
            if profile is not None:
                size = path.stat().st_size
                discovery_started = time.perf_counter()
                index_present = index_path(path).is_file()
                profile["stages_seconds"]["block_index_discovery"] += (
                    time.perf_counter() - discovery_started)
                file_profile = {"file": path.name, "size_bytes": size,
                    "index_present": index_present, "index_valid": False,
                    "selection_mode": "full_scan_fallback", "selected_bytes": size,
                    "range_count": 1, "records_inspected": 0, "raw_bytes_read": 0,
                    "record_bytes_inspected": 0,
                    "average_raw_line_bytes": 0.0, "maximum_raw_line_bytes": 0,
                    "selected_progress_bytes": 0, "selected_progress_percent": 0.0}
            selection_started = time.perf_counter()
            try:
                ranges, selection = selected_ranges(path, start_epoch, end_epoch,
                    timestamp_field="timestamp", iso_timestamp=False)
                if file_profile is not None:
                    file_profile.update({"index_valid": True,
                        "selection_mode": selection["mode"],
                        "selected_bytes": sum(stop - begin for begin, stop in ranges),
                        "range_count": len(ranges)})
            except Exception:
                ranges = ((0, path.stat().st_size),)
            if profile is not None:
                profile["stages_seconds"]["indexed_range_selection"] += (
                    time.perf_counter() - selection_started)
                profile["files"].append(file_profile)
            with path.open("rb") as handle:
              for range_start, range_end in ranges:
                for line in iter_binary_range_lines(
                        handle, range_start, range_end, profile=profile,
                        file_profile=file_profile, deadline=deadline):
                    if profile is not None:
                        profile["counts"]["raw_records_inspected"] += 1
                        line_size = len(line)
                        profile["counts"]["record_bytes_inspected"] += line_size
                        profile["counts"]["maximum_raw_line_bytes"] = max(
                            profile["counts"]["maximum_raw_line_bytes"], line_size)
                        file_profile["records_inspected"] += 1
                        file_profile["record_bytes_inspected"] += line_size
                        file_profile["maximum_raw_line_bytes"] = max(
                            file_profile["maximum_raw_line_bytes"], line_size)
                    operation_started = time.perf_counter()
                    deadline_exceeded = deadline is not None and time.monotonic() > deadline
                    if profile is not None:
                        profile["stages_seconds"]["deadline_check"] += (
                            time.perf_counter() - operation_started)
                    if deadline_exceeded:
                        raise ResearchQueryError("timeout", "research query timed out", 503)
                    operation_started = time.perf_counter()
                    serial_token = _SERIAL_TOKEN_BYTES.search(line)
                    if profile is not None:
                        profile["stages_seconds"]["serial_prefilter"] += (
                            time.perf_counter() - operation_started)
                    if serial_token is not None:
                        operation_started = time.perf_counter()
                        try:
                            explicit_serial = json.loads(serial_token.group(1))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            explicit_serial = None
                        if profile is not None:
                            profile["stages_seconds"]["serial_token_decode"] += (
                                time.perf_counter() - operation_started)
                        if isinstance(explicit_serial, str) and explicit_serial not in wanted:
                            if profile is not None:
                                profile["counts"]["records_skipped_serial_prefilter"] += 1
                            continue
                    operation_started = time.perf_counter()
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                        if profile is not None:
                            profile["stages_seconds"]["full_json_decode"] += (
                                time.perf_counter() - operation_started)
                        continue
                    if profile is not None:
                        profile["stages_seconds"]["full_json_decode"] += (
                            time.perf_counter() - operation_started)
                        profile["counts"]["records_fully_decoded"] += 1
                    operation_started = time.perf_counter()
                    try:
                        epoch = float(record["timestamp"])
                        in_range = start_epoch <= epoch <= end_epoch
                    except (ValueError, TypeError, KeyError):
                        if profile is not None:
                            profile["stages_seconds"]["timestamp_parse_range_check"] += (
                                time.perf_counter() - operation_started)
                        continue
                    if profile is not None:
                        profile["stages_seconds"]["timestamp_parse_range_check"] += (
                            time.perf_counter() - operation_started)
                    if not in_range:
                        if profile is not None:
                            profile["counts"]["records_skipped_timestamp"] += 1
                        continue
                    operation_started = time.perf_counter()
                    timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                    if profile is not None:
                        profile["stages_seconds"]["timestamp_format"] += (
                            time.perf_counter() - operation_started)
                    position = int(record.get("module", 0))
                    observed = record.get("module_serial")
                    if observed is None and 1 <= position <= 6:
                        observed = self.identity.serial_at(position, timestamp).get("physical_serial")
                    if observed not in wanted: continue
                    identity_started = time.perf_counter()
                    identity = self.identity.position_at(observed, timestamp)
                    if profile is not None:
                        profile["stages_seconds"]["identity_assignment"] += (
                            time.perf_counter() - identity_started)
                    operation_started = time.perf_counter()
                    try:
                        soc, current = float(record["soc_percent"]), float(record["current_a"])
                    except (ValueError, TypeError, KeyError):
                        if profile is not None:
                            profile["stages_seconds"]["soc_current_extract"] += (
                                time.perf_counter() - operation_started)
                        continue
                    if profile is not None:
                        profile["stages_seconds"]["soc_current_extract"] += (
                            time.perf_counter() - operation_started)
                    operation_started = time.perf_counter()
                    voltages = record.get("voltages_mv") or []
                    lowest_cell = voltages.index(min(voltages)) + 1 if voltages else None
                    cell_spread_mv = max(voltages) - min(voltages) if voltages else None
                    if profile is not None:
                        profile["stages_seconds"]["cell_context_extract"] += (
                            time.perf_counter() - operation_started)
                    result[observed].append({"timestamp": timestamp, "soc": soc,
                        "current": current, "identity_epoch_id": identity.get("identity_epoch_id"),
                        "position_at_time": identity.get("position_at_time"),
                        "lowest_cell": lowest_cell, "cell_spread_mv": cell_spread_mv})
                    if profile is not None:
                        profile["counts"]["relevant_soc_current_samples"] += 1
        finally:
            if profile is not None:
                profile["stages_seconds"]["jsonl_scan"] = time.perf_counter() - scan_started
                count = profile["counts"]["raw_records_inspected"]
                profile["counts"]["average_raw_line_bytes"] = (
                    profile["counts"]["record_bytes_inspected"] / count if count else 0.0)
                for item in profile["files"]:
                    records = item["records_inspected"]
                    item["average_raw_line_bytes"] = (
                        item["record_bytes_inspected"] / records if records else 0.0)
                    item["selected_progress_bytes"] = min(
                        item["raw_bytes_read"], item["selected_bytes"])
                    item["selected_progress_percent"] = (
                        item["selected_progress_bytes"] / item["selected_bytes"] * 100
                        if item["selected_bytes"] else 100.0)
        for rows in result.values(): rows.sort(key=lambda item: item["timestamp"])
        return result

    def evidence_by_serial(self, physical_serials, timestamp_from, timestamp_to,
                           deadline=None, max_records=10_000, io_profile=None,
                           profile_target_serial=None):
        """Read at most ``max_records`` observations per identity in one scan."""
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        start_epoch = datetime.fromisoformat(start).timestamp()
        end_epoch = datetime.fromisoformat(end).timestamp()
        setup_started = time.perf_counter() if io_profile is not None else None
        wanted = set(physical_serials)
        result = {serial: deque(maxlen=max_records) for serial in wanted}
        truncated = False; truncated_serials = set()
        selected_paths = list(self._paths(start, end))
        if io_profile is not None:
            io_profile.update({"files_discovered": len(selected_paths), "files_opened": 0,
                "bytes_read": 0, "raw_bytes_read": 0,
                "records_inspected": 0, "raw_records_inspected": 0,
                "samples_returned": 0,
                "index_present": False, "index_valid": True,
                "read_mode": "indexed_chunk", "selected_bytes": 0,
                "requested_serials": len(wanted),
                "requested_window_seconds": end_epoch - start_epoch,
                "selected_progress_bytes": 0, "selected_progress_percent": 0.0,
                "range_count": 0, "raw_chunk_reads": 0,
                "records_skipped_serial_prefilter": 0,
                "records_accepted_wanted_serial": 0,
                "target_records_accepted": 0, "peer_records_accepted": 0,
                "full_json_decode_count": 0, "identity_assignment_count": 0,
                "cell_array_conversion_count": 0,
                "derived_cell_context_count": 0,
                "record_materialization_count": 0,
                "timings_seconds": {name: 0.0 for name in (
                    "raw_chunk_read", "serial_prefilter", "full_json_decode",
                    "timestamp_range_check", "identity_assignment",
                    "cell_array_conversion", "derived_cell_context",
                    "record_materialization", "deadline_check",
                    "balancing_extraction", "temperature_extraction",
                    "module_metric_extraction", "file_discovery_setup",
                    "block_index_load_validate_select", "source_open_range_seek",
                    "binary_line_framing", "result_sort_signature_fingerprint")}})
            io_profile["timings_seconds"]["file_discovery_setup"] += (
                time.perf_counter() - setup_started)
        for path in selected_paths:
            operation_started = time.perf_counter() if io_profile is not None else None
            ranges, present, valid, mode = self._bounded_ranges(path, start_epoch, end_epoch)
            if io_profile is not None:
                io_profile["timings_seconds"]["block_index_load_validate_select"] += (
                    time.perf_counter() - operation_started)
                io_profile["index_present"] = io_profile["index_present"] or present
                io_profile["index_valid"] = io_profile["index_valid"] and valid
                if mode != "indexed_chunk": io_profile["read_mode"] = mode
                io_profile["selected_bytes"] += sum(end - begin for begin, end in ranges)
                io_profile["range_count"] += len(ranges)
            operation_started = time.perf_counter() if io_profile is not None else None
            handle = path.open("rb")
            if io_profile is not None:
                io_profile["timings_seconds"]["source_open_range_seek"] += (
                    time.perf_counter() - operation_started)
            with handle:
                if io_profile is not None: io_profile["files_opened"] += 1
                for range_start, range_end in ranges:
                    for line in iter_binary_range_lines(handle, range_start, range_end,
                                                        deadline=deadline,
                                                        io_profile=io_profile):
                        if io_profile is not None:
                            io_profile["bytes_read"] += len(line)
                            io_profile["raw_bytes_read"] += len(line)
                            io_profile["records_inspected"] += 1
                            io_profile["raw_records_inspected"] += 1
                        operation_started = time.perf_counter() if io_profile is not None else None
                        deadline_exceeded = deadline is not None and time.monotonic() > deadline
                        if io_profile is not None:
                            io_profile["timings_seconds"]["deadline_check"] += (
                                time.perf_counter() - operation_started)
                        if deadline_exceeded:
                            raise ResearchQueryError("timeout", "research query timed out", 503)
                        operation_started = time.perf_counter() if io_profile is not None else None
                        serial_token = _SERIAL_TOKEN_BYTES.search(line)
                        if serial_token is not None:
                            try: explicit_serial = json.loads(serial_token.group(1))
                            except (UnicodeDecodeError, json.JSONDecodeError): explicit_serial = None
                            if isinstance(explicit_serial, str) and explicit_serial not in wanted:
                                if io_profile is not None:
                                    io_profile["records_skipped_serial_prefilter"] += 1
                                    io_profile["timings_seconds"]["serial_prefilter"] += (
                                        time.perf_counter() - operation_started)
                                continue
                        if io_profile is not None:
                            io_profile["timings_seconds"]["serial_prefilter"] += (
                                time.perf_counter() - operation_started)
                        operation_started = time.perf_counter() if io_profile is not None else None
                        try:
                            record = json.loads(line); epoch = float(record["timestamp"])
                        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                            if io_profile is not None:
                                io_profile["timings_seconds"]["full_json_decode"] += (
                                    time.perf_counter() - operation_started)
                            continue
                        if io_profile is not None:
                            io_profile["timings_seconds"]["full_json_decode"] += (
                                time.perf_counter() - operation_started)
                            io_profile["full_json_decode_count"] += 1
                        operation_started = time.perf_counter() if io_profile is not None else None
                        if not start_epoch <= epoch <= end_epoch:
                            if io_profile is not None:
                                io_profile["timings_seconds"]["timestamp_range_check"] += (
                                    time.perf_counter() - operation_started)
                            continue
                        timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                        position = int(record.get("module", 0))
                        observed = record.get("module_serial")
                        if observed is None and 1 <= position <= 6:
                            observed = self.identity.serial_at(position, timestamp).get("physical_serial")
                        if observed not in wanted:
                            if io_profile is not None:
                                io_profile["timings_seconds"]["timestamp_range_check"] += (
                                    time.perf_counter() - operation_started)
                            continue
                        if io_profile is not None:
                            io_profile["timings_seconds"]["timestamp_range_check"] += (
                                time.perf_counter() - operation_started)
                            io_profile["records_accepted_wanted_serial"] += 1
                            if observed == profile_target_serial:
                                io_profile["target_records_accepted"] += 1
                            else:
                                io_profile["peer_records_accepted"] += 1
                        if len(result[observed]) >= max_records:
                            truncated = True
                            truncated_serials.add(observed)
                        operation_started = time.perf_counter() if io_profile is not None else None
                        identity = self.identity.position_at(observed, timestamp)
                        if io_profile is not None:
                            io_profile["timings_seconds"]["identity_assignment"] += (
                                time.perf_counter() - operation_started)
                            io_profile["identity_assignment_count"] += 1
                        operation_started = time.perf_counter() if io_profile is not None else None
                        voltages = tuple(float(value) for value in record.get("voltages_mv", ()))
                        voltage_conversion_elapsed = (time.perf_counter() - operation_started
                                                      if io_profile is not None else 0.0)
                        operation_started = time.perf_counter() if io_profile is not None else None
                        temperatures = tuple(float(value) for value in record.get("temperatures_c", ()))
                        if io_profile is not None:
                            temperature_elapsed = time.perf_counter() - operation_started
                            conversion_elapsed = voltage_conversion_elapsed + temperature_elapsed
                            io_profile["timings_seconds"]["cell_array_conversion"] += conversion_elapsed
                            io_profile["timings_seconds"]["temperature_extraction"] += temperature_elapsed
                            io_profile["cell_array_conversion_count"] += 1
                        operation_started = time.perf_counter() if io_profile is not None else None
                        median = statistics.median(voltages) if voltages else None
                        derived = {"minimum_cell_voltage_mv": min(voltages) if voltages else None,
                            "maximum_cell_voltage_mv": max(voltages) if voltages else None,
                            "cell_spread_mv": max(voltages) - min(voltages) if voltages else None,
                            "lowest_cell": voltages.index(min(voltages)) + 1 if voltages else None,
                            "highest_cell": voltages.index(max(voltages)) + 1 if voltages else None,
                            "cell_deviation_from_module_median_mv": [value - median for value in voltages]
                            if voltages else []}
                        if io_profile is not None:
                            io_profile["timings_seconds"]["derived_cell_context"] += (
                                time.perf_counter() - operation_started)
                            io_profile["derived_cell_context_count"] += 1
                        operation_started = time.perf_counter() if io_profile is not None else None
                        balancing = record.get("balancing")
                        if io_profile is not None:
                            io_profile["timings_seconds"]["balancing_extraction"] += (
                                time.perf_counter() - operation_started)
                        operation_started = time.perf_counter() if io_profile is not None else None
                        soc = record.get("soc_percent")
                        module_current = record.get("current_a")
                        module_voltage = record.get("module_voltage_v")
                        module_power = record.get("power_w")
                        if io_profile is not None:
                            io_profile["timings_seconds"]["module_metric_extraction"] += (
                                time.perf_counter() - operation_started)
                        operation_started = time.perf_counter() if io_profile is not None else None
                        row = {"timestamp": timestamp, "physical_serial": observed,
                            "position_at_time": identity.get("position_at_time"),
                            "position_history_id": identity.get("position_history_id"),
                            "identity_epoch_id": identity.get("identity_epoch_id"),
                            "identity_resolved": identity.get("resolved", False),
                            "identity_source": ("record_module_serial"
                                if record.get("module_serial") is not None else "position_history"),
                            "soc": soc, "module_current_a": module_current,
                            "module_voltage_v": module_voltage,
                            "module_power_w": module_power,
                            "cell_voltages_mv": list(voltages), "cell_temperatures_c": list(temperatures),
                            "balancing": balancing}
                        if row["module_voltage_v"] is None and voltages:
                            row["module_voltage_v"] = sum(voltages) / 1000
                        if row["module_power_w"] is None and row["module_voltage_v"] is not None \
                                and row["module_current_a"] is not None:
                            row["module_power_w"] = (float(row["module_voltage_v"])
                                                      * float(row["module_current_a"]))
                        row["derived"] = derived
                        result[observed].append(row)
                        if io_profile is not None:
                            io_profile["timings_seconds"]["record_materialization"] += (
                                time.perf_counter() - operation_started)
                            io_profile["record_materialization_count"] += 1
        operation_started = time.perf_counter() if io_profile is not None else None
        records = {serial: sorted(rows, key=lambda item: item["timestamp"])
                   for serial, rows in result.items()}
        if io_profile is not None:
            io_profile["samples_returned"] = sum(len(rows) for rows in records.values())
            io_profile["selected_progress_bytes"] = min(
                io_profile["bytes_read"], io_profile["selected_bytes"])
            io_profile["selected_progress_percent"] = (
                io_profile["selected_progress_bytes"] / io_profile["selected_bytes"] * 100
                if io_profile["selected_bytes"] else 100.0)
        source_signature = [(path.name, path.stat().st_size, path.stat().st_mtime_ns)
                            for path in selected_paths]
        response = {"records": records, "truncated": truncated,
                "truncated_serials": sorted(truncated_serials),
                "source_signature": source_signature,
                "source_fingerprint": hashlib.sha256(json.dumps(
                    source_signature, separators=(",", ":")).encode()).hexdigest()}
        if io_profile is not None:
            io_profile["timings_seconds"]["result_sort_signature_fingerprint"] += (
                time.perf_counter() - operation_started)
        return response

    def queries_from_evidence(self, evidence, *, physical_serial, timestamp_from,
                              timestamp_to, metrics, resolution="auto", max_points=MAX_POINTS):
        """Project several existing query contracts from one bounded evidence scan."""
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        selected_resolution = ("full" if span <= FULL_DEFAULT_SECONDS else "display") \
            if resolution == "auto" else resolution
        if selected_resolution not in {"full", "display"}:
            raise ResearchQueryError("invalid_argument", "resolution must be auto, full, or display")
        if span > (FULL_MAX_SECONDS if selected_resolution == "full" else DISPLAY_MAX_SECONDS):
            raise ResearchQueryError("range_too_large", "requested range exceeds resolution limit")
        if type(max_points) is not int or not 1 <= max_points <= MAX_POINTS:
            raise ResearchQueryError("invalid_argument", f"max_points must be 1..{MAX_POINTS}")
        start_epoch = datetime.fromisoformat(start).timestamp()
        end_epoch = datetime.fromisoformat(end).timestamp()
        rows = [row for row in evidence["records"].get(physical_serial, ())
                if start_epoch <= datetime.fromisoformat(row["timestamp"]).timestamp() <= end_epoch]
        results = {}
        for metric in metrics:
            if metric not in self.METRICS:
                raise ResearchQueryError("invalid_argument", "metric is unsupported")
            points, observation_times = [], []
            for row in rows:
                epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
                pseudo_record = {"soc_percent": row.get("soc"),
                    "current_a": row.get("module_current_a"),
                    "module_voltage_v": row.get("module_voltage_v"),
                    "voltages_mv": row.get("cell_voltages_mv", ()),
                    "temperatures_c": row.get("cell_temperatures_c", ())}
                observed_values = self._values(pseudo_record, metric, ())
                if any(value is not None for _, value in observed_values):
                    observation_times.append(epoch)
                for cell, value in observed_values:
                    if value is None: continue
                    point = {"timestamp": row["timestamp"], "value": float(value),
                        "physical_serial": physical_serial,
                        "position_at_time": row.get("position_at_time"),
                        "position_history_id": row.get("position_history_id"),
                        "identity_epoch_id": row.get("identity_epoch_id"),
                        "identity_resolved": row.get("identity_resolved", False),
                        "identity_source": row.get("identity_source", "position_history")}
                    if cell is not None: point["cell_number"] = cell
                    points.append(point)
            points.sort(key=lambda item: (item["timestamp"], item.get("cell_number", 0)))
            source_points = len(points)
            if selected_resolution == "display" and len(points) > max_points:
                if max_points == 1: points = points[:1]
                else:
                    step = (len(points) - 1) / (max_points - 1)
                    points = [points[round(index * step)] for index in range(max_points)]
            page = points[:min(max_points, MAX_RECORDS)]
            truncated = (len(page) < len(points)
                         or physical_serial in evidence.get("truncated_serials", ()))
            observation_times = sorted(set(observation_times))
            gaps = [right - left for left, right in zip(observation_times,
                    observation_times[1:]) if right >= left]
            cadence = statistics.median(gaps) if gaps else None
            gap_limit = cadence * 3 if cadence else None
            missing = [{"from": datetime.fromtimestamp(left, timezone.utc).isoformat(),
                        "to": datetime.fromtimestamp(right, timezone.utc).isoformat()}
                       for left, right in zip(observation_times, observation_times[1:])
                       if gap_limit is not None and right - left > gap_limit]
            covered = []
            if observation_times:
                interval_start = observation_times[0]
                for left, right in zip(observation_times, observation_times[1:]):
                    if gap_limit is not None and right - left > gap_limit:
                        covered.append({"from": datetime.fromtimestamp(
                            interval_start, timezone.utc).isoformat(),
                            "to": datetime.fromtimestamp(left, timezone.utc).isoformat()})
                        interval_start = right
                covered.append({"from": datetime.fromtimestamp(
                    interval_start, timezone.utc).isoformat(),
                    "to": datetime.fromtimestamp(observation_times[-1], timezone.utc).isoformat()})
            first = (datetime.fromtimestamp(min(observation_times), timezone.utc).isoformat()
                     if observation_times else None)
            last = (datetime.fromtimestamp(max(observation_times), timezone.utc).isoformat()
                    if observation_times else None)
            coverage = {"requested_range": {"from": start, "to": end},
                "covered_intervals": covered,
                "missing_intervals": missing if observation_times else [{"from": start, "to": end}],
                "first_observation": first, "last_observation": last,
                "sample_count": len(observation_times), "expected_cadence_seconds": cadence,
                "largest_gap_seconds": max(gaps) if gaps else None,
                "quality": "complete" if observation_times and not missing and not truncated else
                           "partial" if observation_times else "absent"}
            results[metric] = {"metric": metric, "physical_serial": physical_serial,
                "points": page, "point_count": len(page), "source_point_count": source_points,
                "resolution": selected_resolution, "coverage": coverage,
                "truncated": truncated, "next_cursor": None,
                "source_fingerprint": hashlib.sha256(json.dumps(
                    evidence["source_signature"]).encode()).hexdigest()}
        return results

    @staticmethod
    def _values(record, metric, cells):
        if metric == "soc": return ((None, record.get("soc_percent")),)
        if metric == "module_current": return ((None, record.get("current_a")),)
        if metric == "module_voltage":
            value = record.get("module_voltage_v")
            if value is None and record.get("voltages_mv"):
                value = sum(float(item) for item in record["voltages_mv"]) / 1000
            return ((None, value),)
        if metric == "module_temperature":
            values = record.get("temperatures_c") or ()
            return ((None, max(values) if values else None),)
        if metric == "cell_spread":
            values = [float(item) for item in record.get("voltages_mv", ())]
            return ((None, max(values) - min(values) if values else None),)
        values = record.get("voltages_mv" if metric != "cell_temperature" else "temperatures_c") or ()
        selected = cells or tuple(range(1, len(values) + 1))
        median = statistics.median(float(item) for item in values) if values else None
        return tuple((cell, (float(values[cell - 1]) - median
                            if metric == "cell_deviation" else values[cell - 1]))
                     for cell in selected if 1 <= cell <= len(values))

    def query(self, *, metric, physical_serial, timestamp_from, timestamp_to,
              resolution="auto", max_points=MAX_POINTS, cells=(), cursor=None,
              deadline=None, io_profile=None):
        if metric not in self.METRICS:
            raise ResearchQueryError("invalid_argument", "metric is unsupported")
        if not physical_serial:
            raise ResearchQueryError("invalid_argument", "physical_serial is required")
        start, end = self.normalize_range(timestamp_from, timestamp_to)
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        if resolution not in {"auto", "full", "display"}:
            raise ResearchQueryError("invalid_argument", "resolution must be auto, full, or display")
        selected_resolution = ("full" if span <= FULL_DEFAULT_SECONDS else "display") \
            if resolution == "auto" else resolution
        if span > (FULL_MAX_SECONDS if selected_resolution == "full" else DISPLAY_MAX_SECONDS):
            raise ResearchQueryError("range_too_large", "requested range exceeds resolution limit")
        if type(max_points) is not int or not 1 <= max_points <= MAX_POINTS:
            raise ResearchQueryError("invalid_argument", f"max_points must be 1..{MAX_POINTS}")
        cells = tuple(sorted(set(cells)))
        if len(cells) > MAX_CELLS or any(not 1 <= cell <= 15 for cell in cells):
            raise ResearchQueryError("invalid_argument", "cell numbers must be 1..15")
        selected_paths = list(self._paths(start, end))
        if io_profile is not None:
            io_profile.update({"files_discovered": len(selected_paths), "files_opened": 0,
                "bytes_read": 0, "records_inspected": 0, "samples_returned": 0,
                "index_present": False, "index_valid": True,
                "read_mode": "indexed_chunk"})
        source_signature = [(path.name, path.stat().st_size, path.stat().st_mtime_ns)
                            for path in selected_paths]
        query_hash = hashlib.sha256(json.dumps((metric, physical_serial, start, end,
            selected_resolution, max_points, cells, source_signature),
            separators=(",", ":")).encode()).hexdigest()
        offset = self.cursor.decode(cursor, query_hash) if cursor else 0
        start_epoch, end_epoch = datetime.fromisoformat(start).timestamp(), datetime.fromisoformat(end).timestamp()
        if metric == "soc":
            candidates, signatures = self._soc_candidates(
                physical_serial, start, end, deadline=deadline, io_profile=io_profile)
            result = self._project_soc_candidates(candidates,
                physical_serial=physical_serial, start=start, end=end,
                selected_resolution=selected_resolution, max_points=max_points,
                offset=offset, source_signature=signatures, io_profile=io_profile)
            result["next_cursor"] = (self.cursor.encode(query_hash, offset + result["point_count"])
                                     if result["truncated"] else None)
            return result
        points, observation_times, signatures = [], [], []
        for path in selected_paths:
            stat = path.stat(); signatures.append((path.name, stat.st_size, stat.st_mtime_ns))
            ranges, present, valid, mode = self._bounded_ranges(path, start_epoch, end_epoch)
            if io_profile is not None:
                io_profile["index_present"] = io_profile["index_present"] or present
                io_profile["index_valid"] = io_profile["index_valid"] and valid
                if mode != "indexed_chunk": io_profile["read_mode"] = mode
            with path.open("rb") as handle:
                if io_profile is not None: io_profile["files_opened"] += 1
                for range_start, range_end in ranges:
                  for line in iter_binary_range_lines(handle, range_start, range_end,
                                                       deadline=deadline):
                    if io_profile is not None:
                        io_profile["bytes_read"] += len(line)
                        io_profile["records_inspected"] += 1
                    if deadline is not None and time.monotonic() > deadline:
                        raise ResearchQueryError("timeout", "research query timed out", 503)
                    serial_token = _SERIAL_TOKEN_BYTES.search(line)
                    if serial_token is not None:
                        try: explicit_serial = json.loads(serial_token.group(1))
                        except (UnicodeDecodeError, json.JSONDecodeError): explicit_serial = None
                        if isinstance(explicit_serial, str) and explicit_serial != physical_serial:
                            continue
                    try:
                        record = json.loads(line); epoch = float(record["timestamp"])
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                        continue
                    if not start_epoch <= epoch <= end_epoch: continue
                    timestamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                    position = int(record.get("module", 0))
                    resolved = self.identity.serial_at(position, timestamp) if 1 <= position <= 6 else {}
                    observed_serial = record.get("module_serial")
                    if ((observed_serial is not None and observed_serial != physical_serial) or
                            (observed_serial is None and
                             resolved.get("physical_serial") != physical_serial)):
                        continue
                    identity = self.identity.position_at(physical_serial, timestamp)
                    observed_values = self._values(record, metric, cells)
                    if any(value is not None for _, value in observed_values):
                        observation_times.append(epoch)
                    for cell, value in observed_values:
                        if value is None: continue
                        point = {"timestamp": timestamp, "value": float(value),
                            "physical_serial": physical_serial,
                            "position_at_time": identity.get("position_at_time"),
                            "position_history_id": identity.get("position_history_id"),
                            "identity_epoch_id": identity.get("identity_epoch_id"),
                            "identity_resolved": identity.get("resolved", False),
                            "identity_source": ("record_module_serial" if observed_serial is not None
                                                else "position_history")}
                        if cell is not None: point["cell_number"] = cell
                        points.append(point)
        points.sort(key=lambda item: (item["timestamp"], item.get("cell_number", 0)))
        source_points = len(points)
        if selected_resolution == "display" and len(points) > max_points:
            if max_points == 1: points = points[:1]
            else:
                step = (len(points) - 1) / (max_points - 1)
                points = [points[round(index * step)] for index in range(max_points)]
        page_limit = min(max_points, MAX_RECORDS)
        page = points[offset:offset + page_limit]
        truncated = offset + len(page) < len(points)
        observation_times = sorted(set(observation_times))
        gaps = [right - left for left, right in zip(observation_times, observation_times[1:])
                if right >= left]
        first = (datetime.fromtimestamp(min(observation_times), timezone.utc).isoformat()
                 if observation_times else None)
        last = (datetime.fromtimestamp(max(observation_times), timezone.utc).isoformat()
                if observation_times else None)
        cadence = statistics.median(gaps) if gaps else None
        gap_limit = cadence * 3 if cadence else None
        missing = [{"from": datetime.fromtimestamp(left, timezone.utc).isoformat(),
                    "to": datetime.fromtimestamp(right, timezone.utc).isoformat()}
                   for left, right in zip(observation_times, observation_times[1:])
                   if gap_limit is not None and right - left > gap_limit]
        covered = []
        if observation_times:
            interval_start = observation_times[0]
            for left, right in zip(observation_times, observation_times[1:]):
                if gap_limit is not None and right - left > gap_limit:
                    covered.append({"from": datetime.fromtimestamp(interval_start, timezone.utc).isoformat(),
                                    "to": datetime.fromtimestamp(left, timezone.utc).isoformat()})
                    interval_start = right
            covered.append({"from": datetime.fromtimestamp(interval_start, timezone.utc).isoformat(),
                            "to": datetime.fromtimestamp(observation_times[-1], timezone.utc).isoformat()})
        coverage = {"requested_range": {"from": start, "to": end},
            "covered_intervals": covered,
            "missing_intervals": missing if observation_times else [{"from": start, "to": end}],
            "first_observation": first, "last_observation": last,
            "sample_count": len(observation_times),
            "expected_cadence_seconds": cadence,
            "largest_gap_seconds": max(gaps) if gaps else None,
            "quality": "complete" if observation_times and not missing and not truncated else
                       "partial" if observation_times else "absent"}
        if io_profile is not None: io_profile["samples_returned"] = len(page)
        return {"metric": metric, "physical_serial": physical_serial, "points": page,
            "point_count": len(page), "source_point_count": source_points,
            "resolution": selected_resolution, "coverage": coverage, "truncated": truncated,
            "next_cursor": self.cursor.encode(query_hash, offset + len(page)) if truncated else None,
            "source_fingerprint": hashlib.sha256(json.dumps(signatures).encode()).hexdigest()}
