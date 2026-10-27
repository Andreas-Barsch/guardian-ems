import hashlib
import pytest
from application_migration import LegacyWriterAdoption
from application_writer import ApplicationWriter
from raw_registry import RawRegistry, encoded, file_signature
from projection_repository import ProjectionRepository, Conflict


class InjectedCrash(Exception):
    pass


def setup(tmp_path):
    root = tmp_path/'legacy'
    root.mkdir()
    path = root/'old.jsonl'
    path.write_bytes(b''.join(encoded({'timestamp':index,'schema_version':1})+b'\n' for index in range(3000)))
    registry = RawRegistry.create(tmp_path/'registry')
    writer = ApplicationWriter(registry, root, stream='cell', writer_epoch='synthetic')
    repo = ProjectionRepository(tmp_path/'repo')
    migration = LegacyWriterAdoption(repo, 'legacy', [(writer,path)])
    migration.initialize()
    migration.begin(provenance={'synthetic':'frozen original file'}, rollback={'legacy':'unchanged'})
    return writer, path, migration


@pytest.mark.parametrize('stage', ['after_adoption_registration','after_adoption_block','after_adoption_ack'])
def test_explicit_bounded_adoption_restart_preserves_bytes_and_file_metadata(tmp_path, stage):
    writer, path, migration = setup(tmp_path)
    before = path.read_bytes()
    signature = file_signature(path)
    def fault(point):
        if point == stage:
            raise InjectedCrash(point)
    with pytest.raises(InjectedCrash):
        for _ in range(30):
            migration.step(fault=fault)
    resumed = LegacyWriterAdoption(migration.repository, 'legacy', [(writer,path)])
    for _ in range(30):
        result = resumed.step()
        assert result['raw_bytes'] <= 65536
        if result['status'] == 'complete':
            break
    else:
        pytest.fail('adoption not complete')
    assert path.read_bytes() == before
    assert file_signature(path) == signature
    assert writer.registry.metrics['raw_bytes_written'] == 0
    assert writer.registry.state(writer.source(path))['end'] == len(before)


def test_changed_frozen_source_is_not_adopted_or_repaired(tmp_path):
    writer, path, migration = setup(tmp_path)
    with path.open('ab') as handle:
        handle.write(b'{}\n')
    changed = path.read_bytes()
    with pytest.raises(Conflict, match='frozen legacy source changed'):
        migration.step()
    assert path.read_bytes() == changed
    assert writer.registry.state(writer.source(path)) is None


def test_legacy_adoption_does_not_finish_an_unrelated_pending_raw_append(tmp_path):
    from raw_registry import hashed
    writer, path, migration = setup(tmp_path)
    other = writer.root/'other.jsonl'
    other.touch()
    receipt = writer.registry.register('other', 'initial', other,
        writer_epoch='synthetic', namespace='legacy-local', operation='other-register')
    def fault(stage):
        if stage == 'after_intent':
            raise InjectedCrash(stage)
    writer.registry.fault = fault
    with pytest.raises(InjectedCrash):
        writer.registry.append('other', b'{"timestamp":1}\n',
            expected_receipt=hashed(encoded(receipt)), operation='other-append')
    original = path.read_bytes()
    original_signature = file_signature(path)
    intent = (writer.registry.root/'intent.json').read_bytes()
    with pytest.raises(Conflict, match='pending Raw append requires writer recovery'):
        migration.step()
    assert other.read_bytes() == b''
    assert path.read_bytes() == original
    assert file_signature(path) == original_signature
    assert (writer.registry.root/'intent.json').read_bytes() == intent
    assert migration.state()['ordinal'] == 0
