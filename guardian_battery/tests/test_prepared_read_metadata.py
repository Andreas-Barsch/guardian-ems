from uuid import UUID
import pytest
from position_history import PositionSnapshot
from research_identity import ResearchIdentityResolver
from read_metadata_index import PreparedMetadata, stamp
from projection_repository import ProjectionRepository, Conflict
from research_budget import ResearchQueryError
from timeline import TimelineEvent


def resolver():
    snapshots=[]
    for i, positions in enumerate([{'1':'A','2':'B'},{'1':'B','3':'A'},{'1':'B'}],1):
        snapshots.append(PositionSnapshot(1,'PHS-'+str(UUID(int=i)),f'2026-10-{i:02}T00:00:00Z',
            f'2026-10-{i:02}T01:00:00Z','MEV-synthetic',
            {str(p):positions.get(str(p)) for p in range(1,7)}))
    return ResearchIdentityResolver(snapshots)


def prepared(tmp_path):
    repo=ProjectionRepository(tmp_path/'metadata.sqlite')
    value=PreparedMetadata(repo,'synthetic-v1');value.initialize()
    return value


@pytest.mark.parametrize('when',['2026-09-30T23:59:59Z','2026-10-01T00:00:00Z',
    '2026-10-02T00:00:00Z','2026-10-03T12:00:00Z'])
def test_prepared_identity_matches_actual_resolver(tmp_path,when):
    original=resolver();value=prepared(tmp_path);value.prepare_identity(original)
    assert value.topology_at(when)==original.topology_at(when)
    for serial in ('A','B','unknown'):
        assert value.position_at(serial,when)==original.position_at(serial,when)
        assert value.epoch_at(serial,when)==original.epoch_at(serial,when)
        assert value.epochs(serial,when,when)==original.epochs(serial,when,when)
    for pos in range(1,7):assert value.serial_at(pos,when)==original.serial_at(pos,when)
    assert value.epochs()==original.epochs()


def test_metadata_unprepared_immutable_and_reopened(tmp_path):
    value=prepared(tmp_path)
    with pytest.raises(ResearchQueryError,match='unavailable'):value.topology_at('2026-10-01T00:00:00Z')
    original=resolver();value.prepare_identity(original);value.prepare_identity(original)
    reopened=PreparedMetadata(ProjectionRepository(value.repository.path),value.revision)
    assert reopened.epochs()==original.epochs()
    with pytest.raises(Conflict,match='immutable'):value.prepare_identity(ResearchIdentityResolver([]))
    assert reopened.epochs()==original.epochs()
    assert reopened.position_window(stamp('2026-10-02T12:00:00Z'),stamp('2026-10-03T12:00:00Z'))==[s.to_dict() for s in original.snapshots[1:]]


def test_timeline_preparation_retains_archived_history(tmp_path):
    value=prepared(tmp_path)
    events=[TimelineEvent('maintenance','2026-10-01T00:00:00Z','Synthetic','', 'maintenance','m1',metadata={'archived_at':'2026-10-02T00:00:00Z'}),
        TimelineEvent('alarm_started','2026-10-01T00:00:01Z','Synthetic','','technical','a1',module_number=2)]
    value.prepare_timeline(events);value.prepare_timeline(events)
    kwargs={'timestamp_from':'2026-10-01T00:00:00Z','timestamp_to':'2026-10-01T00:00:01Z'}
    assert value.query(**kwargs)==events[1:]
    assert value.query(**kwargs,include_archived=True)==list(reversed(events))
    assert value.query(**kwargs,module_number=1)==[]
    with pytest.raises(Conflict):value.prepare_timeline([])
