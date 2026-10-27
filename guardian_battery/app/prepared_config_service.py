"""Explicit configuration transport and bounded provenance for the candidate.

The existing HTTP validation/save/verify/restart sequence remains in config_ui.
No Supervisor address, credential discovery, or history preparation occurs here.
"""
import json
from pathlib import Path
from raw_registry import MAX_BLOCK, hashed
from projection_repository import Unavailable


class PreparedConfigService:
    def __init__(self, *, options_path, config_history, supervisor_api):
        if config_history.append_backend is None:
            raise ValueError('confirmed configuration history required')
        self.options_path = Path(options_path)
        self.history = config_history
        self.api = supervisor_api

    def read_options(self):
        from config_ui import DEFAULTS
        with self.options_path.open('rb') as handle:
            raw = handle.read(MAX_BLOCK + 1)
        if len(raw) > MAX_BLOCK:
            raise Unavailable('configuration exceeds bounded options budget')
        return {**DEFAULTS, **json.loads(raw)}

    def last_record(self):
        backend = self.history.append_backend
        state = backend.registry.state(backend.source(self.history.path))
        if state is None or not state['end']:
            return {}
        if state['end'] - state['start'] > MAX_BLOCK:
            raise Unavailable('configuration provenance exceeds confirmed block budget')
        raw = backend.registry._raw_range(state['path'], state['start'], state['end'])
        if hashed(raw) != state['block_hash']:
            raise Unavailable('configuration provenance changed')
        return json.loads(raw.splitlines()[-1])
