"""One bounded, durable projection publication; never discovers history files.

The intent owns at most one confirmed block plus exact metadata/inventory images.
Recovery is a writer/startup operation, never an interactive read side effect.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from raw_registry import MAX_BLOCK, file_signature
from projection_repository import Unavailable

MAX_INTENT = 2 * MAX_BLOCK + 16384


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.publication-tmp')
    with temporary.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def bounded_bytes(path, limit=8192):
    if not path.exists():
        return None
    with path.open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise Unavailable('projection publication metadata exceeds budget')
    return value


class ProjectionPublication:
    def __init__(self, backend, directory, inventory, *, fault=None):
        self.backend = backend
        self.directory = Path(directory).resolve()
        self.inventory = Path(inventory).resolve()
        self.intent = self.inventory.with_name(self.inventory.name + '.publication.json')
        self.fault = fault or (lambda point: None)

    def publish(self, day, payload, start, metadata, inventory):
        if self.intent.exists():
            raise Unavailable('projection publication recovery required')
        if len(payload) > MAX_BLOCK:
            raise Unavailable('projection flush exceeds one confirmed block budget')
        meta_path = self.directory / (day + '.meta.json')
        value = dict(version=1, day=day, directory=str(self.directory),
            inventory_path=str(self.inventory), operation='projection:' + uuid.uuid4().hex,
            payload=base64.b64encode(payload).decode(), start=start,
            metadata=metadata, inventory=inventory,
            prior_metadata=self._fingerprint(meta_path),
            prior_inventory=self._fingerprint(self.inventory))
        raw = encoded(value)
        envelope = encoded(dict(value=value, sha256=digest(raw)))
        if len(envelope) > MAX_INTENT:
            raise Unavailable('projection publication intent exceeds budget')
        atomic(self.intent, envelope)
        self.fault('after_intent')
        self.recover()

    @staticmethod
    def _fingerprint(path):
        value = bounded_bytes(path)
        return None if value is None else digest(value)

    def recover(self):
        data = bounded_bytes(self.intent, MAX_INTENT)
        if data is None:
            return False
        try:
            envelope = json.loads(data)
            value = envelope['value']
            if digest(encoded(value)) != envelope['sha256']:
                raise ValueError('intent checksum mismatch')
            if (value['version'] != 1 or value['directory'] != str(self.directory)
                    or value['inventory_path'] != str(self.inventory)
                    or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value['day'])):
                raise ValueError('intent binding mismatch')
            payload = base64.b64decode(value['payload'], validate=True)
            start = value['start']
            if type(start) is not int or start < 0 or len(payload) > MAX_BLOCK:
                raise ValueError('intent byte range invalid')
            end = start + len(payload)
            if value['metadata']['projection_end_offset'] != end:
                raise ValueError('intent endpoint mismatch')
            path = self.directory / (value['day'] + '.jsonl')
            meta = self.directory / (value['day'] + '.meta.json')
            # Never overwrite independently changed metadata or inventory.
            for target, key, prior in ((meta, 'metadata', 'prior_metadata'),
                                      (self.inventory, 'inventory', 'prior_inventory')):
                if self._fingerprint(target) not in {value[prior], digest(encoded(value[key]))}:
                    raise ValueError('publication target changed independently')
            self.backend.registry.recover()
            state = self.backend.registry.state(self.backend.source(path))
            if payload:
                already = state is not None and state['operation'] == value['operation']
                if (state is None and start != 0) or (state is not None and
                        state['end'] != (end if already else start)):
                    raise ValueError('confirmed projection frontier changed')
                receipt = self.backend.append(path, payload, operation=value['operation'])
                if receipt['end'] != end or file_signature(path) != receipt['signature']:
                    raise ValueError('confirmed projection signature changed')
            else:
                if not path.exists() and start == 0:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('xb') as handle:
                        handle.flush(); os.fsync(handle.fileno())
                    sync_directory(path.parent)
                if path.stat().st_size != start:
                    raise ValueError('empty projection frontier changed')
            self.fault('after_projection')
            atomic(meta, encoded(value['metadata']))
            self.fault('after_metadata')
            atomic(self.inventory, encoded(value['inventory']))
            self.fault('after_inventory')
            self.intent.unlink()
            sync_directory(self.intent.parent)
            return True
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise Unavailable('projection publication recovery required: ' + str(exc)) from exc
