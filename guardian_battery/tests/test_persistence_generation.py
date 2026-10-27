"""Real-store regression for audit aggregate_persistence_ack; no runtime IO."""
import json
import threading

import pytest

from cell_diagnostics import CellDiagnosticStore, CellSample
from config_ui import DEFAULTS
from derived_persistence import DerivedPersistenceWorker
from diagnostic_aggregates import DiagnosticAggregateStore


def sample(stamp):
    return CellSample(stamp, 1, [3300] * 15, -2., 50., [25.] * 15,
                      [False] * 15, 'SN-A', 'PHS-1')


def stores(tmp_path):
    return (CellDiagnosticStore(tmp_path / 'cells.json'),
            DiagnosticAggregateStore(tmp_path / 'aggregates.json', CellDiagnosticStore.phases))


def test_audit_successful_worker_write_acknowledges_actual_store(tmp_path):
    cells, aggregates = stores(tmp_path)
    cells.add(sample(1)); aggregates.add(sample(1), DEFAULTS)
    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    worker.start()
    assert worker.stop(5)
    assert worker.status()['last_completed']['success']
    assert not aggregates._dirty
    assert aggregates.persistence_payload() is None
    assert DiagnosticAggregateStore(aggregates.path, CellDiagnosticStore.phases).records == aggregates.records


def test_older_success_does_not_ack_newer_mutation(tmp_path, monkeypatch):
    cells, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    entered, release = threading.Event(), threading.Event()
    persist = aggregates.persist_payload

    def blocked(payload):
        entered.set()
        assert release.wait(5)
        return persist(payload)

    monkeypatch.setattr(aggregates, 'persist_payload', blocked)
    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    worker.start()
    try:
        assert entered.wait(2)
        aggregates.add(sample(2), DEFAULTS)
    finally:
        release.set()
        assert worker.stop(5)
    assert aggregates._dirty
    assert all(v['sample_count'] == 1 for v in json.loads(aggregates.path.read_text())['records'].values())
    assert all(v['sample_count'] == 2 for v in aggregates.persistence_payload()['records'].values())
    assert aggregates.save()
    assert not aggregates._dirty


def test_save_cannot_clear_mutation_during_write(tmp_path, monkeypatch):
    _, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    persist = aggregates.persist_payload

    def mutate(payload):
        aggregates.add(sample(2), DEFAULTS)
        return persist(payload)

    monkeypatch.setattr(aggregates, 'persist_payload', mutate)
    assert aggregates.save()
    assert aggregates._dirty


@pytest.mark.parametrize('failure', ['cell', 'aggregate'])
def test_failed_worker_does_not_ack(tmp_path, monkeypatch, failure):
    cells, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    def fail(_payload):
        raise OSError('synthetic write failure')
    monkeypatch.setattr(cells if failure == 'cell' else aggregates, 'persist_payload', fail)
    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    worker.start()
    assert worker.stop(5)
    assert not worker.status()['last_completed']['success']
    assert aggregates._dirty


def test_receipt_is_process_local_and_not_serialized(tmp_path):
    _, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    payload = aggregates.persistence_payload()
    assert set(payload) == {'schema_version', 'updated_at', 'retention_days', 'backfill_sources', 'records'}
    assert aggregates.persist_payload(payload)
    restored = DiagnosticAggregateStore(aggregates.path, CellDiagnosticStore.phases)
    restored.add(sample(2), DEFAULTS)
    assert not restored.acknowledge_persistence(payload)
    assert not aggregates.acknowledge_persistence(dict(payload))
    assert restored._dirty
    assert aggregates._dirty
    assert aggregates.acknowledge_persistence(payload)
    assert not aggregates._dirty


def test_coalescing_acknowledges_latest_written_snapshot(tmp_path, monkeypatch):
    cells, aggregates = stores(tmp_path)
    entered, release = threading.Event(), threading.Event()
    persist = aggregates.persist_payload
    generations = []
    def blocked(payload):
        if not generations:
            entered.set()
            assert release.wait(5)
        generations.append(payload.generation)
        return persist(payload)
    monkeypatch.setattr(aggregates, 'persist_payload', blocked)
    aggregates.add(sample(1), DEFAULTS)
    first = aggregates.persistence_payload()
    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.submit(cells.persistence_payload(), first)
    worker.start()
    try:
        assert entered.wait(2)
        for stamp in (2, 3):
            aggregates.add(sample(stamp), DEFAULTS)
            worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    finally:
        release.set()
        assert worker.stop(5)
    assert generations == [1, 3]
    assert worker.status()['coalesced'] == 1
    assert not aggregates._dirty
    # Late duplicate completion of an old write must not undo a newer ACK.
    assert aggregates.acknowledge_persistence(first)
    assert not aggregates._dirty
    assert all(v['sample_count'] == 3 for v in json.loads(aggregates.path.read_text())['records'].values())


def test_startup_explicit_dirty_assignment_advances_generation(tmp_path):
    _, aggregates = stores(tmp_path)
    aggregates._dirty = True
    first = aggregates.persistence_payload()
    aggregates._dirty = True
    assert aggregates.acknowledge_persistence(first)
    assert aggregates._dirty


def test_unconfirmed_write_is_failure_and_stays_dirty(tmp_path, monkeypatch):
    cells, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    monkeypatch.setattr(aggregates, 'persist_payload', lambda payload: False)
    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    worker.start()
    assert worker.stop(5)
    assert worker.status()['persisted'] == 0
    assert not worker.status()['last_completed']['success']
    assert aggregates._dirty


def test_pruning_and_backfill_mutations_survive_older_ack(tmp_path):
    _, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    payload = aggregates.persistence_payload()
    aggregates.prune_through('2030-01-01')
    assert aggregates.acknowledge_persistence(payload)
    assert aggregates._dirty
    after_prune = aggregates.persistence_payload()
    assert aggregates.merge_backfill_records(payload['records']) == 15
    assert aggregates.acknowledge_persistence(after_prune)
    assert aggregates._dirty
    after_merge = aggregates.persistence_payload()
    aggregates.mark_source_complete(tmp_path / 'synthetic.jsonl', {'size': 1},
                                    'test-config', set(aggregates.records), {})
    assert aggregates.acknowledge_persistence(after_merge)
    assert aggregates._dirty


def test_failure_recovery_acks_only_successful_generation(tmp_path, monkeypatch):
    cells, aggregates = stores(tmp_path)
    aggregates.add(sample(1), DEFAULTS)
    original = aggregates.persist_payload
    def fail(payload):
        raise OSError('synthetic failure')
    monkeypatch.setattr(aggregates, 'persist_payload', fail)
    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    worker.start()
    assert worker.stop(5)
    assert aggregates._dirty
    aggregates.add(sample(2), DEFAULTS)
    monkeypatch.setattr(aggregates, 'persist_payload', original)
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    worker.start()
    assert worker.stop(5)
    assert worker.status()['persisted'] == 1
    assert worker.status()['last_completed']['generation'] == 2
    assert not aggregates._dirty
