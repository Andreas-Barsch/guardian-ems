import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from soc_crash_detector import SocCrashDetector
from soc_crash_events import SocCrashEventStore
from version import DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION

# Existing main has a mkdir at import. Never touch the productive path in tests.
with patch.object(Path, 'mkdir', return_value=None):
    import main


def modules(timestamp, candidate):
    return [SimpleNamespace(module=1, soc_percent=50, pwr_sample_at=timestamp),
            SimpleNamespace(module=2, soc_percent=candidate, pwr_sample_at=timestamp)]


def test_runtime_uses_sample_timestamp_and_retries_store_failure(tmp_path, caplog):
    detector = SocCrashDetector(); pending = {}
    real = SocCrashEventStore(tmp_path / 'events.jsonl')
    class Store:
        failed = False
        def append(self, event):
            if not self.failed:
                self.failed = True
                raise OSError('test failure')
            return real.append(event)
    store = Store()
    for timestamp, soc in [(0, 60), (10, 49), (37, 43.9)]:
        main.record_soc_crash_observations(modules(timestamp, soc), detector, store, pending)
    assert len(pending) == 1 and 'persistence failed' in caplog.text
    event = next(iter(pending.values()))
    assert event.confirmed_at == '1970-01-01T00:00:37.000000+00:00'
    main.record_soc_crash_observations(modules(40, 43), detector, store, pending)
    assert pending == {} and real.get(event.event_id) == event
    assert len(real.path.read_bytes().splitlines()) == 1


def test_cached_documentary_identity_only_and_metadata_failure_does_not_stop_detection(tmp_path):
    from position_history import DocumentedIdentityResolver
    from test_research_api import snapshot
    resolver = DocumentedIdentityResolver([snapshot('1970-01-01T00:00:00Z', {1: 'R', 2: 'C'})])
    store = SocCrashEventStore(tmp_path / 'events.jsonl'); detector = SocCrashDetector(); pending = {}
    for timestamp, soc in [(0, 60), (10, 49), (20, 43.9)]:
        main.record_soc_crash_observations(modules(timestamp, soc), detector, store, pending, resolver)
    import json
    value = json.loads(store.path.read_text())
    assert value['candidate_physical_serial'] == 'C'
    assert value['reference_serials_at_confirmation'] == ['R']


def test_runtime_hook_does_not_issue_io_queries_or_change_acquisition_order():
    source = Path(main.__file__).read_text()
    tree = ast.parse(source)
    hook = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == 'record_soc_crash_observations')
    hook_source = ast.get_source_segment(source, hook)
    assert '.command(' not in hook_source and '.from_path(' not in hook_source
    assert 'module.pwr_sample_at' in hook_source
    runtime = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    calls = [n for n in ast.walk(runtime) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'record_soc_crash_observations']
    assert len(calls) == 1
    assert source.index('module.pwr_sample_at = pwr_sample_at') < source.index(
        'record_soc_crash_observations(modules, soc_crash_detector')
    assert DIAGNOSTIC_ENGINE_VERSION == '0.4.12'


def test_manifest_policy_defaults_and_bounds_for_release_0_9_0():
    config = yaml.safe_load((Path(main.__file__).parents[1] / 'config.yaml').read_text())
    assert config['version'] == GUARDIAN_VERSION == '0.9.0'
    assert config['options']['soc_crash_gap_pp'] == 5.0
    assert config['options']['soc_crash_window_seconds'] == 300
    assert config['schema']['soc_crash_gap_pp'] == 'float(0.01,100)'
    assert 'soc_crash_drop_pp' not in config['options']
    assert config['schema']['soc_crash_drop_pp'] == 'float(0.01,100)?'
    assert config['schema']['soc_crash_window_seconds'] == 'int(1,86400)'


def test_live_pwr_hook_and_offline_replay_have_identical_events_and_candidate_counts(tmp_path):
    from test_soc_crash_replay import source, BASE
    from soc_crash_replay import replay_saved_pwr
    frames = [(0, [50, 60]), (10, [50, 49]), (100, [50]),
              (200, [50, 45]), (210, [35, 45]), (220, [30, 45])]
    path = source(tmp_path, frames)
    before = path.read_bytes()
    result = replay_saved_pwr([path], start='2026-09-11T00:00:00Z',
                             end='2026-09-11T00:10:00Z')
    detector = SocCrashDetector()
    store = SocCrashEventStore(tmp_path / 'live-test' / 'events.jsonl')
    pending = {}
    resolver = SimpleNamespace(identity_at=lambda module, at: ('S'+str(module), None))
    for offset, values in frames:
        observations = [SimpleNamespace(module=module, soc_percent=soc, pwr_sample_at=BASE+offset)
                        for module, soc in enumerate(values, 1)]
        main.record_soc_crash_observations(observations, detector, store, pending, resolver)
    import json
    assert [json.loads(line) for line in store.path.read_text().splitlines()] == result['events']
    assert detector.candidates_started == result['candidates_started'] == 2
    assert result['crashes'] == 2 and pending == {}
    assert result['suspended_groups'] == 1 and result['appended'] == 0
    assert result['events'][1]['confirmed_at'] == result['events'][1]['observation_started_at']
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
