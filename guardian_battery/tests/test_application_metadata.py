from datetime import datetime, timezone
import pytest
from application_metadata import MetadataTail, HeadMetadataReader, ConfirmedIdentity
from projection_repository import Unavailable
from test_metadata_log_consumer import setup, append, drain
from test_metadata_revision import publish
from test_history_epoch_adapter import position


class InjectedCrash(Exception):
    pass


def initial(tmp_path):
    registry, receipt, repo, revision, consumer = setup(tmp_path)
    receipt = append(registry, receipt, [position(100, 'A')])
    drain(consumer)
    publish(revision)
    tail = MetadataTail(repo, registry, head='current', mapping={'positions': 'position'},
                        origin_proof={'confirmed': 'synthetic empty origin'})
    tail.initialize()
    return registry, receipt, repo, tail


def finish(tail):
    outcomes = []
    for _ in range(1000):
        result = tail.step()
        outcomes.append(result)
        if result.get('status') == 'caught_up':
            return outcomes
    pytest.fail('metadata tail not caught up')


def test_live_identity_waits_for_confirmed_tail_and_request_keeps_revision(tmp_path):
    registry, receipt, repo, tail = initial(tmp_path)
    reader = HeadMetadataReader(repo, 'current')
    identity = ConfirmedIdentity(tail, reader)
    when = datetime.fromtimestamp(300, timezone.utc)
    assert identity.identity_at(1, when)[0] == 'A'
    with reader.capture():
        receipt = append(registry, receipt, [position(200, 'B')])
        with pytest.raises(Unavailable, match='pending'):
            identity.identity_at(1, when)
        outcomes = finish(tail)
        assert sum(item.get('decodes', 0) for item in outcomes) == 1
        assert reader.serial_at(1, when)['physical_serial'] == 'A'
    assert reader.serial_at(1, when)['physical_serial'] == 'B'
    assert identity.identity_at(1, when)[0] == 'B'
    assert tail.step()['raw_bytes'] == 0
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM metadata_reader').fetchone()[0] == 0


@pytest.mark.parametrize('stage', ['after_metadata_dispatch', 'after_metadata_receipt_commit', 'after_metadata_publish_commit'])
def test_metadata_actual_append_and_restart_at_commit_boundaries(tmp_path, stage):
    registry, receipt, repo, tail = initial(tmp_path)
    append(registry, receipt, [position(200, 'B')])
    def fault(point):
        if point == stage:
            raise InjectedCrash(point)
    with pytest.raises(InjectedCrash):
        for _ in range(1000):
            tail.step(fault=fault)
    reopened = MetadataTail(repo, registry, head=tail.head, mapping=tail.mapping, origin_proof=tail.origin_proof)
    finish(reopened)
    reader = HeadMetadataReader(repo, 'current')
    assert reader.serial_at(1, '1970-01-01T00:05:00+00:00')['physical_serial'] == 'B'
