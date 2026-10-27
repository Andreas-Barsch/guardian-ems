"""Reader I/O regressions: bounded syscalls without changing observations."""
import io
import json
from pathlib import Path

import pytest

from history_block_index import build_index
from research_timeseries import ResearchQueryError
from test_research_api import environment, get


def test_fallback_has_bounded_raw_reads_and_no_per_record_seek(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    api._refresh_identity()
    source = api.paths.cell_history / '2026-09-11.jsonl'
    row = json.loads(source.read_text().splitlines()[0])
    # Several MiB, representative of the failing production day's 5667 rows.
    raw = ((json.dumps({**row, 'padding': 'x' * 500}) + '\n') * 6000).encode()
    source.write_bytes(raw)
    calls = {'read': 0, 'seek': 0, 'tell': 0}
    handles = []
    class CountingRaw(io.BytesIO):
        def readinto(self, data):
            calls['read'] += 1
            return super().readinto(data)
        def seek(self, *args):
            calls['seek'] += 1
            return super().seek(*args)
        def tell(self):
            calls['tell'] += 1
            return super().tell()
    original = Path.open
    def opened(path, mode='r', buffering=-1, *args, **kwargs):
        if path == source and mode == 'rb':
            handle = io.BufferedReader(CountingRaw(raw), buffer_size=buffering if buffering > 0 else io.DEFAULT_BUFFER_SIZE)
            handles.append(handle)
            return handle
        return original(path, mode, buffering, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opened)
    records = list(api.series._records('2026-09-11T00:00:00+00:00', '2026-09-11T23:59:59+00:00'))
    assert len(records) == 6000
    assert calls['seek'] <= 2 and calls['tell'] <= 2
    assert calls['read'] < 64
    assert all(handle.closed for handle in handles)


@pytest.mark.parametrize('indexed', [False, True])
def test_disordered_records_block_boundaries_and_open_tail_are_preserved(tmp_path, indexed):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / '2026-09-11.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    # Reverse order explicitly prevents an early-stop optimisation losing data.
    path.write_text(''.join(json.dumps(row) + '\n' for row in reversed(rows)))
    query = 'cell-history?metric=cell_voltage&physical_serial=SERIAL-M4&resolution=full&from=2026-09-11T10:02:00Z&to=2026-09-11T10:06:00Z'
    expected = get(api, query).body
    if indexed:
        build_index(path, timestamp_field='timestamp', iso_timestamp=False, block_records=2, complete=False)
    actual = get(api, query).body
    assert actual == expected
    assert actual['data']['point_count'] == 45
    assert {p['cell_number'] for p in actual['data']['points']} == set(range(1, 16))


def test_missing_metric_and_internal_gaps_are_not_zero_filled(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / '2026-09-11.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row.get('soc_percent') in (69, 67): row.pop('current_a')
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = get(api, 'module-history?metric=module_current&physical_serial=SERIAL-M4&resolution=full&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z').body['data']
    assert [p['value'] for p in result['points']] == [-2, -2, -2]
    assert result['coverage']['largest_gap_seconds'] == 360
    absent = get(api, 'module-history?metric=module_current&physical_serial=SERIAL-M4&resolution=full&from=2026-09-11T11:00:00Z&to=2026-09-11T12:00:00Z').body['data']
    assert absent['points'] == [] and absent['coverage']['quality'] == 'absent'


def test_all_fifteen_cells_paginate_exactly_without_loss(tmp_path):
    api, _, _ = environment(tmp_path)
    base = 'cell-history?metric=cell_voltage&physical_serial=SERIAL-M4&resolution=full&from=2026-09-11T00:00:00Z&to=2026-09-12T23:59:59Z'
    complete = get(api, base).body['data']
    points = []
    cursor = None
    for _ in range(12):
        page = get(api, base + '&max_points=7' + ('&cursor=' + cursor if cursor else '')).body['data']
        points.extend(page['points'])
        assert page['coverage'] == complete['coverage']
        assert page['source_point_count'] == 75
        cursor = page['next_cursor']
        if cursor is None: break
    assert cursor is None and points == complete['points']


def test_timeout_closes_file_and_api_gate_recovers(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    api._refresh_identity()
    path = api.paths.cell_history / '2026-09-11.jsonl'
    original = Path.open
    handles = []
    def opened(p, *args, **kwargs):
        handle = original(p, *args, **kwargs)
        if p == path: handles.append(handle)
        return handle
    monkeypatch.setattr(Path, 'open', opened)
    with pytest.raises(ResearchQueryError, match='timed out'):
        list(api.series._records('2026-09-11T00:00:00+00:00', '2026-09-12T00:00:00+00:00', deadline=-1))
    assert handles and all(h.closed for h in handles)
    with monkeypatch.context() as patch:
        patch.setattr('research_api.QUERY_TIMEOUT_SECONDS', -1)
        result = get(api, 'cell-history?metric=cell_voltage&physical_serial=SERIAL-M4&resolution=full&from=2026-09-11T00:00:00Z&to=2026-09-12T00:00:00Z')
        assert result.status == 503 and result.body['error']['code'] == 'timeout'
    assert api.gate.active == 0
    assert get(api, 'status').status == 200


@pytest.mark.parametrize('ending', [b'\n', b'\r\n'])
@pytest.mark.parametrize('tail', ['complete', 'no_newline', 'invalid'])
def test_byte_offsets_with_utf8_crlf_and_final_line(tmp_path, ending, tail):
    api, _, _ = environment(tmp_path)
    api._refresh_identity()
    path = api.paths.cell_history / '2026-09-11.jsonl'
    base = json.loads(path.read_text().splitlines()[0])
    rows = [{**base, 'note': 'Größe ⚡ 模块', 'soc_percent': v} for v in (50, 49, 48)]
    encoded = [json.dumps(r, ensure_ascii=False).encode('utf-8') for r in rows]
    raw = ending.join(encoded)
    if tail == 'complete': raw += ending
    elif tail == 'invalid': raw = ending.join(encoded[:2]) + ending + encoded[2][:-2]
    path.write_bytes(raw)
    records = list(api.series._records('2026-09-11T00:00:00+00:00', '2026-09-12T00:00:00+00:00'))
    assert [r for r, _ in records] == rows[:2 if tail == 'invalid' else 3]
    assert path.read_bytes() == raw


def test_disjoint_index_offsets_read_exactly_selected_lines(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / '2026-09-11.jsonl'
    row = json.loads(path.read_text().splitlines()[0])
    rows = [dict(row), {**row, 'timestamp': row['timestamp'] + 3600}, {**row, 'soc_percent': 45}]
    path.write_bytes(b''.join(json.dumps(r).encode() + b'\r\n' for r in rows))
    build_index(path, timestamp_field='timestamp', iso_timestamp=False, block_records=1)
    decoded = []
    original = json.loads
    def loads(raw, *args, **kwargs):
        value = original(raw, *args, **kwargs)
        if isinstance(value, dict) and 'soc_percent' in value: decoded.append(value)
        return value
    monkeypatch.setattr('research_timeseries.json.loads', loads)
    records = list(api.series._records('2026-09-11T10:00:00+00:00', '2026-09-11T10:00:00+00:00'))
    assert decoded == [rows[0], rows[2]]
    assert [r for r, _ in records] == decoded


def test_append_after_range_selection_does_not_expand_snapshot(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / '2026-09-11.jsonl'
    before = path.read_bytes()
    original = Path.open
    extra = json.loads(before.splitlines()[0]); extra['soc_percent'] = 12
    armed = True
    def opened(p, mode='r', *args, **kwargs):
        nonlocal armed
        if p == path and mode == 'rb' and armed:
            armed = False
            with original(path, 'ab') as handle: handle.write(json.dumps(extra).encode() + b'\n')
        return original(p, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opened)
    records = list(api.series._records('2026-09-11T00:00:00+00:00', '2026-09-12T00:00:00+00:00'))
    assert [r for r, _ in records] == [json.loads(line) for line in before.splitlines()]


def test_rotation_after_open_keeps_original_descriptor(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / '2026-09-11.jsonl'
    before = [json.loads(line) for line in path.read_text().splitlines()]
    iterator = api.series._records('2026-09-11T00:00:00+00:00', '2026-09-12T00:00:00+00:00')
    first = next(iterator)
    replacement = tmp_path / 'replacement'
    replacement.write_text(json.dumps({**before[0], 'soc_percent': 12}) + '\n')
    replacement.replace(path)
    assert [r for r, _ in [first, *iterator]] == before


def test_deadline_mid_buffer_and_explicit_close_release_handle(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / '2026-09-11.jsonl'
    handles = []
    original = Path.open
    def opened(p, *args, **kwargs):
        handle = original(p, *args, **kwargs)
        if p == path: handles.append(handle)
        return handle
    monkeypatch.setattr(Path, 'open', opened)
    clock = [0]
    monkeypatch.setattr('research_timeseries.time.monotonic', lambda: clock[0])
    iterator = api.series._records('2026-09-11T00:00:00+00:00', '2026-09-12T00:00:00+00:00', deadline=1)
    next(iterator)
    clock[0] = 2
    with pytest.raises(ResearchQueryError, match='timed out'): next(iterator)
    assert all(h.closed for h in handles)
    iterator = api.series._records('2026-09-11T00:00:00+00:00', '2026-09-12T00:00:00+00:00')
    next(iterator); iterator.close()
    assert all(h.closed for h in handles)
