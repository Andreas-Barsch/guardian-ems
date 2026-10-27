import pytest
from application_catalog import CatalogTail
from application_writer import ApplicationWriter
from raw_registry import RawRegistry, RegistryConflict
from raw_registry_catalog import RegistryCatalogConsumer
from source_catalog import SourceCatalog
from position_history import PositionHistoryLog
from config_history import ConfigHistory
from test_history_epoch_adapter import position
from test_config_history import options
from indexed_history_window import IndexedHistoryWindow


class InjectedCrash(Exception):
    pass


def setup(tmp_path):
    registry = RawRegistry.create(tmp_path/'registry')
    root = tmp_path/'logs'
    writer = ApplicationWriter(registry, root, stream='metadata', writer_epoch='synthetic')
    PositionHistoryLog(root/'positions.jsonl', append_backend=writer).append(position(100, 'A'))
    ConfigHistory(root/'config.jsonl', append_backend=writer).record_if_changed(options())
    catalog = SourceCatalog(tmp_path/'catalog')
    consumer = RegistryCatalogConsumer(registry, catalog, source_formats={
        'metadata/positions.jsonl':'daily_position', 'metadata/config.jsonl':None})
    tail = CatalogTail(consumer, name='positions')
    tail.initialize()
    return registry, catalog, consumer, tail


def finish(tail):
    for _ in range(100):
        if tail.step().get('status') == 'caught_up':
            return
    pytest.fail('catalog tail did not finish')


def test_explicit_metadata_fanout_never_interprets_config_as_position(tmp_path):
    registry, catalog, consumer, tail = setup(tmp_path)
    finish(tail)
    with IndexedHistoryWindow(catalog).read(99, 101) as rows:
        values = [value for value, _ in rows]
    assert len(values) == 1 and values[0]['positions']['1'] == 'A'
    assert catalog.status()['errors'] == 0
    assert tail.step()['raw_bytes'] == 0
    with pytest.raises(RegistryConflict):
        RegistryCatalogConsumer(registry, catalog, source_formats={'metadata/positions.jsonl':None}).initialize()


@pytest.mark.parametrize('stage', ['after_catalog_dispatch','after_catalog_commit','after_index_commit','after_catalog_ack'])
def test_actual_catalog_tail_restarts_at_child_and_ack_boundaries(tmp_path, stage):
    registry, catalog, consumer, tail = setup(tmp_path)
    def fault(point):
        if point == stage:
            raise InjectedCrash(point)
    with pytest.raises(InjectedCrash):
        for _ in range(100):
            tail.step(fault=fault)
    reopened = CatalogTail(consumer, name='positions')
    finish(reopened)
    with IndexedHistoryWindow(catalog).read(99, 101) as rows:
        assert len(list(rows)) == 1
