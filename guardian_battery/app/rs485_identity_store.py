"""Bounded current identity checkpoint; raw evidence remains the audit source.

One writer (the passive reader). Prepared mode retains its existing confirmed
catalog restoration instead of opening a second checkpoint for that runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from datetime import datetime

DEFAULT_IDENTITY_FILE = Path('/share/guardian_battery/rs485_identities.json')
MAX_BYTES = 1024 * 1024


def observation_time(value):
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('identity time requires timezone')
        stamp = parsed.timestamp()
    elif type(value) in (int, float):
        stamp = float(value)
    else:
        raise ValueError('invalid identity observation time')
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError('invalid identity observation time')
    return stamp


def apply_observation(identities, adr, value):
    """Latest observed assignment wins; invalidate displaced addresses explicitly."""
    stamp = observation_time(value['timestamp'])
    involved = [key for key, old in identities.items()
                if key == adr or old['serial_string'] == value['serial_string']]
    if any(observation_time(identities[key]['timestamp']) > stamp for key in involved):
        return False
    # Equal-time conflicting observations are ambiguous, never choose by address.
    ambiguous = [key for key in involved if observation_time(identities[key]['timestamp']) == stamp
                 and (key != adr or identities[key]['serial_string'] != value['serial_string'])]
    if ambiguous:
        for key in set(ambiguous + [adr]):
            identities.pop(key, None)
        return True
    displaced = [key for key in involved if key != adr]
    for key in displaced:
        del identities[key]
    previous = identities.get(adr)
    value = dict(value)
    if displaced:
        value['displaced_adrs'] = displaced
    elif previous and previous.get('displaced_adrs'):
        value['displaced_adrs'] = previous['displaced_adrs']
    if previous and previous['serial_string'] != value['serial_string']:
        value['previous_serial'] = previous['serial_string']
    elif previous and previous.get('previous_serial'):
        value['previous_serial'] = previous['previous_serial']
    identities[adr] = value
    return True


def observed_identity(frame, decoded, timestamp, *, source='live_0x93', evidence=None):
    return {'adr': frame.adr, 'serial_string': decoded['serial_string'],
            'serial_raw': decoded['serial_raw'], 'timestamp': timestamp,
            'decode_source': source, 'identity_known': True,
            'identity_currently_confirmed': source == 'live_0x93',
            'raw_frame': frame.raw_frame.hex().upper(),
            'source_frame_reference': hashlib.sha256(frame.raw_frame).hexdigest(),
            'evidence': evidence or {'source': 'rs485_passive', 'paired_command': 0x93,
                                     'request_matched': True},
            'quality': {'evidence_level': 'observation', 'identity_source': 'direct_0x93',
                        'causality': 'not_determined'}}


def validate(payload):
    from rs485_sniffer import parse_frame, decode_0x93_info
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise ValueError('unsupported identity checkpoint schema')
    entries = payload.get('identities')
    if not isinstance(entries, list) or len(entries) > 256:
        raise ValueError('invalid identity checkpoint entries')
    result, serials = {}, set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('invalid identity checkpoint entry')
        adr = entry.get('adr')
        if type(adr) is not int or not 0 <= adr <= 255 or adr in result:
            raise ValueError('invalid/duplicate identity address')
        observation_time(entry['timestamp'])
        raw = bytes.fromhex(entry['raw_frame'])
        frame = parse_frame(raw)
        decoded = decode_0x93_info(adr, frame.info)
        evidence = entry.get('evidence', {})
        if not isinstance(evidence, dict):
            raise ValueError('invalid identity evidence')
        if (frame.adr != adr or frame.is_request or frame.rtn != 0
                or not decoded.get('decoder_supported')
                or entry['serial_string'] != decoded['serial_string']
                or entry['serial_raw'] != decoded['serial_raw']
                or entry['source_frame_reference'] != hashlib.sha256(raw).hexdigest()
                or evidence.get('paired_command') != 0x93
                or evidence.get('request_matched') is not True
                or not isinstance(entry.get('decode_source'), str)):
            raise ValueError('invalid identity checkpoint evidence')
        if entry['serial_string'] in serials:
            raise ValueError('duplicate identity serial')
        serials.add(entry['serial_string'])
        result[adr] = {**entry, 'identity_known': True, 'identity_currently_confirmed': False}
    return result


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class IdentityStore:
    def __init__(self, path=DEFAULT_IDENTITY_FILE, *, refresh_seconds=300):
        if refresh_seconds <= 0:
            raise ValueError('identity refresh interval must be positive')
        self.path = Path(path)
        self.refresh_seconds = refresh_seconds
        self.last_saved = {}
        self.error = None
        self.writes = 0
        self.blocked = False

    def _read(self):
        with self.path.open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('identity checkpoint too large')
        return raw, validate(json.loads(raw))

    def load(self):
        try:
            _, self.last_saved = self._read()
            return dict(self.last_saved)
        except FileNotFoundError:
            return {}
        except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
            self.error = type(exc).__name__ + ': invalid identity checkpoint'
            self.blocked = True
            return {}

    def save(self, identities, *, force=False):
        if self.blocked:
            return False  # Preserve corrupt source and previous backup for inspection.
        try:
            changed = set(identities) != set(self.last_saved) or any(
                old is None or old['serial_string'] != value['serial_string']
                or observation_time(value['timestamp']) - observation_time(old['timestamp']) >= self.refresh_seconds
                for adr, value in identities.items() for old in [self.last_saved.get(adr)])
            if not force and not changed:
                return False
            data = json.dumps({'schema_version': 1, 'identities': [
                {**value, 'identity_currently_confirmed': False} for _, value in sorted(identities.items())
            ]}, ensure_ascii=True, sort_keys=True).encode('ascii')
            if len(data) > MAX_BYTES:
                raise ValueError('identity checkpoint too large')
            validate(json.loads(data))
            if self.path.exists():
                previous, _ = self._read()
                atomic_write(self.path.with_suffix('.json.previous'), previous)
            atomic_write(self.path, data)
            self.last_saved = {key: dict(value) for key, value in identities.items()}
            self.writes += 1
            self.error = None
            return True
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            self.error = type(exc).__name__ + ': identity checkpoint write failed'
            return False
