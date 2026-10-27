"""Confirmed-prefix handover only; synthetic Raw files and explicit registries."""
import os
import pytest
from confirmed_prefix_reader import ConfirmedPrefixReader
from indexed_history_window import IndexedHistoryWindow
from research_budget import ResearchQueryError
from test_application_tail import setup, append, drain, bounds
from raw_registry import encoded, hashed


def prepared(tmp_path):
    env, preparation, tail = setup(tmp_path)
    start, _ = bounds(env['origin'])
    append(env, start+10)
    drain(preparation, tail)
    window = IndexedHistoryWindow(env['catalog'], prefix_reader=ConfirmedPrefixReader(env['rawreg']))
    return env, window, start


def rows(window, start):
    with window.read(start+9, start+11) as records:
        return list(records)


def test_delayed_catalog_excludes_new_bytes_with_bounded_confirmed_append_proof(tmp_path, monkeypatch):
    env, window, start = prepared(tmp_path)
    expected = rows(window,start)
    frozen = env['catalog'].progress('raw','r1')
    for cycle in range(40):
        append(env,start+20+cycle)
    monkeypatch.setattr(env['catalog'],'index_step',lambda *a,**k:pytest.fail('GET index build'))
    assert [record for record, _ in rows(window,start)] == [record for record, _ in expected]
    assert env['catalog'].progress('raw','r1') == frozen
    state = window.metrics
    assert state['decodes'] == 1
    assert state['confirmed_prefixes'][0]['pending_confirmed_bytes'] > 0
    assert 0 < state['proof_metadata_bytes'] <= 10*65536
    assert state['latest_raw_coverage'].startswith('not_asserted')


@pytest.mark.parametrize('change', ['unconfirmed_append','rewrite','truncate','replace','revision','missing_receipt'])
def test_unproven_prefix_change_stays_unavailable_without_repair(tmp_path, change):
    env, window, start = prepared(tmp_path)
    receipt = env['rawreg'].state('raw')
    from pathlib import Path
    path = Path(receipt['path'])
    if change == 'unconfirmed_append':
        with path.open('ab') as handle:handle.write(b'{}\n')
    elif change == 'rewrite':
        stamp = path.stat()
        raw = path.read_bytes()
        assert b'3300' in raw
        path.write_bytes(raw.replace(b'3300',b'3301',1))
        os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    elif change == 'truncate':
        path.write_bytes(b'')
    elif change == 'replace':
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement,path)
    elif change == 'revision':
        env['rawreg'].register('raw','r2',path,writer_epoch=receipt['writer_epoch'],
            namespace=receipt['namespace'],operation='explicit-revision',
            expected_receipt=hashed(encoded(receipt)),allow_existing=True)
    else:
        env['rawreg']._receipt_path(receipt['seq']).unlink()
    before = path.read_bytes()
    with pytest.raises(ResearchQueryError,match='prefix|record hash|confirmed'):
        rows(window,start)
    assert path.read_bytes() == before


def test_append_proof_exhaustion_is_explicit_and_does_not_read_raw_tail(tmp_path, monkeypatch):
    env, window, start = prepared(tmp_path)
    for cycle in range(257):append(env,start+20+cycle)
    from raw_registry import RawRegistry
    monkeypatch.setattr(RawRegistry,'_raw_range',lambda *a,**k:pytest.fail('proof read Raw tail'))
    original = RawRegistry._read
    metadata_bytes = []
    def tracked(registry,*args,**kwargs):
        before = registry.metrics['metadata_bytes']
        result = original(registry,*args,**kwargs)
        metadata_bytes.append(registry.metrics['metadata_bytes']-before)
        return result
    monkeypatch.setattr(RawRegistry,'_read',tracked)
    with pytest.raises(ResearchQueryError,match='proof budget exceeded'):
        rows(window,start)
    assert 0 < sum(metadata_bytes) <= 5*65536


def test_unchanged_source_does_not_walk_unrelated_registry_history(tmp_path, monkeypatch):
    env, window, start = prepared(tmp_path)
    expected = [record for record, _ in rows(window, start)]
    registry = env['rawreg']
    other = registry.register('other', 'r1', tmp_path/'other.jsonl',
        writer_epoch='synthetic', namespace='legacy-local', operation='other-registration')
    for i in range(300):
        other = registry.append('other', b'{}\n', expected_receipt=hashed(encoded(other)),
                                operation='other-'+str(i))
    from raw_registry import RawRegistry
    monkeypatch.setattr(RawRegistry, 'read_page', lambda *a, **k:pytest.fail('unrelated journal scan'))
    assert [record for record, _ in rows(window, start)] == expected
    assert window.metrics['confirmed_prefixes'][0]['proof_records'] == 0
    assert window.metrics['proof_metadata_bytes'] < 20*65536
