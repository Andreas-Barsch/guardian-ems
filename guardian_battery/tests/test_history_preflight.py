import importlib.util
from pathlib import Path
import shlex
import pytest

spec = importlib.util.spec_from_file_location('history_preflight', Path(__file__).parents[1]/'tools/history_preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def test_window_selects_only_explicit_days_and_context():
    paths = preflight.selected_paths('2026-10-18T16:39:00Z','2026-10-19T16:39:00Z')
    assert 'rs485_history/2026-10-18.jsonl' in paths
    assert 'rs485_history/2026-10-19.jsonl' in paths
    assert 'rs485_history/2026-10-17.jsonl' not in paths
    assert 'cell_history/2026-10-17.jsonl' in paths
    assert 'cell_history/2026-10-20.jsonl' in paths
    assert 'canonical_phase_v2/2026-10-17.json.gz' in paths
    assert 'hycube_history_projection/2026-10-18.jsonl.idx' in paths
    assert len(paths) < 100


@pytest.mark.parametrize('start,end', [
    ('2026-10-18T00:00:00','2026-10-19T00:00:00Z'),
    ('2026-10-18T00:00:00Z','2026-10-18T00:00:00Z'),
    ('2026-10-19T00:00:00Z','2026-10-18T00:00:00Z'),
    ('2026-10-18T00:00:00Z','2026-10-21T00:00:00Z'),
])
def test_unbounded_or_ambiguous_window_rejected(start,end):
    with pytest.raises(ValueError): preflight.selected_paths(start,end)


def test_inventory_never_reads_contents_or_discovers_older_history(tmp_path, monkeypatch):
    source = tmp_path/'cell_history/2026-10-18.jsonl'
    source.parent.mkdir()
    source.write_bytes(b'not parsed or repaired')
    before = source.stat()
    def forbidden(*args, **kwargs): raise AssertionError('content/discovery access')
    monkeypatch.setattr(Path,'open',forbidden)
    monkeypatch.setattr(Path,'glob',forbidden)
    monkeypatch.setattr(Path,'iterdir',forbidden)
    result = preflight.inventory(tmp_path, {'cell_history/2026-10-18.jsonl':'source', 'missing.idx':'index'})
    assert result['files'][0]['bytes'] == before.st_size
    assert result['files'][0]['validity'] == 'not_checked'
    assert result['files'][1]['status'] == 'missing'
    assert not result['ready_for_preparation']
    assert source.stat().st_mtime_ns == before.st_mtime_ns


def test_outside_symlink_is_not_inspected(tmp_path):
    root = tmp_path/'root'; root.mkdir()
    (tmp_path/'other').write_text('private')
    (root/'link').symlink_to(tmp_path/'other')
    assert preflight.inventory(root,{'link':'source'})['files'][0]['status'] == 'outside_root_not_inspected'
    with pytest.raises(ValueError): preflight.inventory(root, {'../other':'source'})


def test_shell_paths_are_quoted_no_glob_or_untrusted_execution():
    root = '/share/a $(touch nope); x'
    command = preflight.shell_query(root, {'day.jsonl':'source'})
    words = shlex.split(command)
    assert str(Path(root)/'day.jsonl')+';' in words
    assert 'find ' not in command and 'cat ' not in command


def test_timezone_normalization_keeps_utc_file_selection():
    a = preflight.selected_paths('2026-10-18T18:39:00+02:00','2026-10-19T18:39:00+02:00')
    b = preflight.selected_paths('2026-10-18T16:39:00Z','2026-10-19T16:39:00Z')
    assert a == b
