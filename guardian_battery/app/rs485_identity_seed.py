"""Explicit bounded one-file migration; never called during normal startup."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from rs485_identity_store import observed_identity, apply_observation, validate
from rs485_sniffer import parse_frame, decode_0x93_info


def seed_from_file(path, *, expected_adrs=None, max_bytes=32 * 1024 * 1024, max_records=40000):
    path = Path(path)
    identities, digest, total = {}, hashlib.sha256(), 0
    with path.open('rb') as handle:
        for line_number in range(1, max_records + 2):
            line = handle.readline(min(65537, max_bytes - total + 1))
            if not line:
                break
            total += len(line)
            if total > max_bytes or line_number > max_records or len(line) > 65536:
                raise ValueError('explicit identity migration budget exceeded')
            digest.update(line)
            record = json.loads(line)
            if not (record.get('record_type') == 'frame' and record.get('direction') == 'response'
                    and record.get('paired_command') == 0x93
                    and record.get('request_matched') is True
                    and record.get('checksum_valid') is True and record.get('frame_complete') is True):
                continue
            frame = parse_frame(bytes.fromhex(record['raw_frame']))
            if frame.is_request or frame.rtn != 0 or frame.adr != record['adr']:
                raise ValueError('inconsistent historical identity frame')
            decoded = decode_0x93_info(frame.adr, frame.info)
            if not decoded.get('decoder_supported'):
                continue
            value = observed_identity(frame, decoded, record['timestamp'], source='historical_raw_redecode',
                evidence={'source': str(path), 'line': line_number, 'paired_command': 0x93,
                          'request_matched': True})
            apply_observation(identities, frame.adr, value)
    if expected_adrs is not None and set(identities) != set(expected_adrs):
        raise ValueError('historical identity addresses differ from reviewed set')
    for value in identities.values():
        value['evidence']['source_sha256'] = digest.hexdigest()
    payload = {'schema_version': 1, 'identities': list(identities.values())}
    validate(payload)
    return payload
