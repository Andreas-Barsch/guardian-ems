import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import soc_crash_events
from soc_crash_detector import SocObservation
from soc_crash_events import SocCrashEvent, SocCrashEventStore, make_event


def event_at(offset=0):
    return make_event(SocObservation(2,49,offset+10), SocObservation(1,50,offset+10),
                      SocObservation(2,43.9,offset+20),5,300)


def historical_equal_values():
    values = event_at().to_dict()
    # Confirmation/drop are not ID inputs. This is an old, valid >= record.
    values.update(candidate_soc_at_confirmation=44.0, observed_drop_pp=5.0)
    return values


def initial_tie_event():
    return make_event(SocObservation(2,49,10), None, SocObservation(2,43.9,20),5,300)


@pytest.mark.parametrize('drop,valid', [(4.9, False), (5.0, False), (5.1, True)])
def test_new_event_validation_is_strict(drop, valid):
    start = SocObservation(2, 49, 10)
    reference = SocObservation(1, 50, 10)
    current = SocObservation(2, 49 - drop, 110)
    if valid:
        assert make_event(start, reference, current, 5, 300).observed_drop_pp == drop
    else:
        with pytest.raises(ValueError, match='threshold'):
            make_event(start, reference, current, 5, 300)


def test_historical_equality_readable_but_not_new_or_appendable(tmp_path):
    values = historical_equal_values()
    raw = (json.dumps(values, indent=None) + '\n').encode()
    path = tmp_path / 'events.jsonl'
    path.write_bytes(raw)
    store = SocCrashEventStore(path)
    event = store.get(values['event_id'])
    assert event.to_dict() == values
    assert SocCrashEventStore(path).get(event.event_id).to_dict() == values
    with pytest.raises(ValueError, match='threshold'):
        SocCrashEvent(**values)
    with pytest.raises(ValueError, match='threshold'):
        store.append(event)
    other = SocCrashEventStore(tmp_path / 'new' / 'events.jsonl')
    with pytest.raises(ValueError, match='threshold'):
        other.append(event)
    assert not other.path.parent.exists()
    assert path.read_bytes() == raw
    assert store.append(event_at(100))
    assert path.read_bytes().startswith(raw)
    assert store.get(event.event_id).to_dict() == values


@pytest.mark.parametrize('changes', [
    {'observed_drop_pp': 4.9}, {'candidate_module': 0},
    {'reference_module': 2}, {'causality': 'determined'},
    {'confirmed_at': '1970-01-01T00:06:00+00:00'},
    {'event_id': 'SCS-' + '0' * 64}, {'_persisted': True},
])
def test_historical_read_keeps_other_validation(changes):
    with pytest.raises((TypeError, ValueError)):
        SocCrashEvent.from_persisted(historical_equal_values() | changes)


def test_null_reference_round_trip_and_validation(tmp_path):
    event = initial_tie_event()
    assert event.reference_module is None and event.reference_physical_serial is None
    assert '_persisted' not in event.to_dict()
    store = SocCrashEventStore(tmp_path / 'events.jsonl')
    assert store.append(event)
    assert SocCrashEventStore(store.path).get(event.event_id) == event
    assert not store.append(event)
    with pytest.raises(ValueError, match='reference serial'):
        replace(event, reference_physical_serial='ARBITRARY')


def test_read_only_empty_store_creates_nothing(tmp_path):
    path = tmp_path / 'not-created' / 'events.jsonl'
    store = SocCrashEventStore(path)
    assert store.get('missing') is None
    assert not path.parent.exists()


def test_append_only_idempotent_deterministic_compact_and_durable(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    store = SocCrashEventStore(path)
    event = event_at()
    assert event == event_at()
    flushed_records = []
    real_fsync = soc_crash_events.os.fsync
    def fsync(fd):
        # The data must already be visible through a separate reader (flush).
        flushed_records.append(path.read_bytes())
        real_fsync(fd)
    monkeypatch.setattr(soc_crash_events.os, 'fsync', fsync)
    assert store.append(event)
    prefix = path.read_bytes()
    assert flushed_records and flushed_records[0] == prefix
    assert len(prefix) < 1500
    assert SocCrashEventStore(path).append(event) is False
    assert path.read_bytes() == prefix
    assert store.append(event_at(100))
    assert path.read_bytes().startswith(prefix)
    assert len(path.read_bytes().splitlines()) == 2
    assert set(json.loads(prefix)) == {
        'event_id', 'schema_version', 'detector_version', 'observation_started_at',
        'confirmed_at', 'candidate_module', 'candidate_physical_serial',
        'reference_module', 'reference_physical_serial', 'candidate_soc_at_start',
        'candidate_soc_at_confirmation', 'observed_drop_pp', 'x_pp', 'y_seconds', 'causality'}


@pytest.mark.parametrize('tail', [b'{"damaged":', b'broken\n', b'{"incomplete":true}'])
def test_damaged_tail_preserved_and_valid_events_recover(tmp_path, tail):
    path = tmp_path / 'events.jsonl'
    store = SocCrashEventStore(path)
    first, second = event_at(), event_at(100)
    store.append(first)
    with path.open('ab') as handle:
        handle.write(tail)
    damaged = path.read_bytes()
    assert store.get(first.event_id) == first
    assert store.append(second)
    assert path.read_bytes().startswith(damaged)
    restarted = SocCrashEventStore(path)
    assert restarted.get(first.event_id) == first
    assert restarted.get(second.event_id) == second


def test_conflicting_revision_cannot_rewrite_identity(tmp_path):
    store = SocCrashEventStore(tmp_path / 'events.jsonl')
    event = event_at()
    store.append(event)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='immutable'):
        store.append(replace(event, candidate_physical_serial='LATER'))
    assert store.path.read_bytes() == before


def test_complete_event_without_newline_is_not_duplicated(tmp_path):
    path = tmp_path / 'events.jsonl'; event = event_at()
    partial = json.dumps(event.to_dict()).encode()
    path.write_bytes(partial)
    store = SocCrashEventStore(path)
    assert store.get(event.event_id) is None
    assert store.append(event) is False
    assert path.read_bytes() == partial + b'\n'


def test_concurrent_store_instances_append_once(tmp_path):
    path = tmp_path / 'events.jsonl'
    event = event_at()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: SocCrashEventStore(path).append(event), range(12)))
    assert sum(results) == 1
    assert len(path.read_bytes().splitlines()) == 1


def test_sorted_bounded_list_and_incremental_lookup_without_raw_changes(tmp_path):
    raw = tmp_path / 'cell_history.jsonl'; raw.write_bytes(b'raw unchanged\n')
    store = SocCrashEventStore(tmp_path / 'events.jsonl')
    reader = SocCrashEventStore(store.path)
    late, early = event_at(100), event_at()
    store.append(late)
    assert reader.get(late.event_id) == late
    store.append(early)
    events, truncated = reader.list(datetime.fromtimestamp(0, timezone.utc),
        datetime.fromtimestamp(1000, timezone.utc), 1)
    assert events == [early] and truncated
    assert raw.read_bytes() == b'raw unchanged\n'


def test_failed_fsync_retry_does_not_duplicate(tmp_path, monkeypatch):
    store = SocCrashEventStore(tmp_path / 'events.jsonl'); event = event_at()
    original = soc_crash_events.os.fsync
    monkeypatch.setattr(soc_crash_events.os, 'fsync', lambda _: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        store.append(event)
    monkeypatch.setattr(soc_crash_events.os, 'fsync', original)
    assert store.append(event) is False
    assert store.get(event.event_id) == event
    assert len(store.path.read_bytes().splitlines()) == 1
