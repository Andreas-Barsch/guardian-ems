"""Synthetic standalone storage tests; no application/HA integration."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from projection_repository import Conflict, Cursor, ProjectionRepository, Unavailable


@pytest.fixture
def repo(tmp_path):
    r = ProjectionRepository(tmp_path / 'synthetic.sqlite')
    r.register_source('raw', 'r1', metadata={'origin': 'synthetic'}, durable_end=100)
    return r


def draft(r, value, offset, *, head='main'):
    work = r.begin(head, expected_bundle=r.head(head))
    for product in ('current', 'aggregate'):
        r.stage(work, product, 'day', {'row': {'value': value}}, metadata={'coverage': 'synthetic'})
        r.stage_cursor(work, product, 'raw', 'r1', expected=r.cursor(product, 'raw', 'r1'), next_offset=offset)
    return work


def state(r, bundle):
    return [r.read(bundle, p, 'day')['rows'] for p in ('current', 'aggregate')]


def sql(r, statement, args=()):
    with sqlite3.connect(r.path) as db:
        return db.execute(statement, args).fetchall()


def test_restart_and_old_reader_after_new_commit(repo):
    n = repo.publish(draft(repo, 1, 10))
    token = repo.pin(n, owner='reader')
    before = state(repo, n)
    w = draft(repo, 2, 20)
    restart = ProjectionRepository(repo.path)
    assert restart.head('main') == n
    assert restart.cursor('current', 'raw', 'r1') == Cursor(10, n)
    newer = restart.publish(w)
    assert state(ProjectionRepository(repo.path), n) == before
    assert state(repo, newer) == [[('row', {'value': 2})]] * 2
    assert repo.inputs(n)[('current', 'raw', 'r1')] == Cursor(10, n)
    assert repo.inputs(newer)[('current', 'raw', 'r1')] == Cursor(20, newer)
    repo.release(token)


STAGES = ['after_partition_cursor:current', 'after_partition_cursor:aggregate', 'after_partition_rows:current', 'after_partition_publication:current', 'after_partition_rows:aggregate', 'after_partition_publication:aggregate', 'after_rows', 'after_cursors', 'after_publications', 'after_bundle', 'after_head', 'after_commit']


@pytest.mark.parametrize('stage', STAGES)
def test_exception_atomicity_and_retry(repo, stage):
    n = repo.publish(draft(repo, 1, 10))
    w = draft(repo, 2, 20)
    def fail(at):
        # A separate read connection must still see the full old bundle precommit.
        if at != 'after_commit':
            assert repo.head('main') == n
            assert state(repo, n) == [[('row', {'value': 1})]] * 2
        if at == stage:
            raise RuntimeError('injected')
    with pytest.raises(RuntimeError, match='injected'):
        repo.publish(w, fault=fail)
    restart = ProjectionRepository(repo.path)
    if stage == 'after_commit':
        assert restart.cursor('current', 'raw', 'r1').offset == 20
        with pytest.raises(Unavailable):
            restart.publish(w)
    else:
        assert restart.head('main') == n
        assert restart.cursor('current', 'raw', 'r1') == Cursor(10, n)
        restart.publish(w)
    assert state(restart, restart.head('main')) == [[('row', {'value': 2})]] * 2
    assert state(restart, n) == [[('row', {'value': 1})]] * 2


@pytest.mark.parametrize('stage', STAGES)
def test_process_exit_recovery(repo, stage):
    n = repo.publish(draft(repo, 1, 10))
    w = draft(repo, 2, 20)
    program = """
import os,sys
from pathlib import Path
from projection_repository import ProjectionRepository
r=ProjectionRepository(Path(sys.argv[1]))
r.publish(sys.argv[2], fault=lambda at: os._exit(73) if at==sys.argv[3] else None)
"""
    child = subprocess.run([sys.executable, '-c', program, str(repo.path), w, stage], env=os.environ.copy(), timeout=15, capture_output=True)
    assert child.returncode == 73, child.stderr.decode()
    r = ProjectionRepository(repo.path)
    committed = stage == 'after_commit'
    assert (r.head('main') != n) == committed
    assert r.cursor('current', 'raw', 'r1').offset == (20 if committed else 10)
    assert state(r, r.head('main')) == [[('row', {'value': 2 if committed else 1})]] * 2
    if not committed:
        r.publish(w)
    assert sql(r, 'PRAGMA integrity_check') == [('ok',)]
    assert sql(r, 'PRAGMA foreign_key_check') == []


def test_cursor_and_head_cas(repo):
    a, b = draft(repo, 1, 10), draft(repo, 2, 20)
    n = repo.publish(a)
    with pytest.raises(Conflict, match='head'):
        repo.publish(b)
    repo.discard(b)
    w = draft(repo, 3, 30)
    repo.stage_cursor(w, 'current', 'raw', 'r1', expected=Cursor(10, n + 10), next_offset=30)
    with pytest.raises(Conflict, match='cursor'):
        repo.publish(w)
    assert repo.head('main') == n


def test_other_head_cannot_mutate_shared_partition(repo):
    repo.publish(draft(repo, 1, 10))
    w = draft(repo, 2, 20, head='other')
    with pytest.raises(Conflict, match='partition'):
        repo.publish(w)
    assert repo.head('other') is None


def test_source_frontier_and_revision(repo):
    w = draft(repo, 1, 101)
    with pytest.raises(Conflict, match='confirmed source'):
        repo.publish(w)
    repo.confirm_source('raw', 'r1', expected_end=100, durable_end=101)
    repo.publish(w)
    with pytest.raises(Conflict):
        repo.confirm_source('raw', 'r1', expected_end=100, durable_end=102)
    with pytest.raises(Conflict):
        repo.register_source('raw', 'r1', metadata={'origin': 'rewritten'}, durable_end=101)
    repo.register_source('raw', 'r2', metadata={'origin': 'replacement'}, durable_end=3)
    assert repo.cursor('current', 'raw', 'r2') is None


def test_cursor_only_and_backwards_forbidden(repo):
    w = repo.begin('main', expected_bundle=None)
    repo.stage_cursor(w, 'current', 'raw', 'r1', expected=None, next_offset=1)
    with pytest.raises(ValueError, match='cursor-only'):
        repo.publish(w)
    with pytest.raises(ValueError):
        repo.stage_cursor(w, 'current', 'raw', 'r1', expected=Cursor(5, 1), next_offset=4)


def test_delta_sharing_retire_and_immutable_payload(repo):
    w = repo.begin('main', expected_bundle=None)
    repo.stage(w, 'current', 'day', {f'{i:04}': i for i in range(1000)}, metadata={})
    n = repo.publish(w)
    before = repo.read(n, 'current', 'day', limit=1024)
    w = repo.begin('main', expected_bundle=n)
    repo.stage(w, 'current', 'day', {'0001': 5000, '0002': None, '0003': 3}, metadata={})
    newer = repo.publish(w)
    assert sql(repo, 'SELECT COUNT(*) FROM value_version') == [(1001,)]
    assert repo.read(n, 'current', 'day', limit=1024) == before
    rows = dict(repo.read(newer, 'current', 'day', limit=1024)['rows'])
    assert rows['0001'] == 5000 and '0002' not in rows and len(rows) == 999
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        sql(repo, "UPDATE value_version SET payload='{}'")
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        sql(repo, 'UPDATE value_version SET retired=NULL WHERE retired IS NOT NULL')
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        sql(repo, "UPDATE publication SET metadata='{}'")


@pytest.mark.parametrize('root', ['pin', 'draft', 'dependency'])
def test_gc_protects_references_then_collects(repo, root):
    n = repo.publish(draft(repo, 1, 10))
    before = state(repo, n)
    token = repo.pin(n, owner='reader') if root == 'pin' else None
    private = repo.begin('main', expected_bundle=n) if root == 'draft' else None
    newer = repo.publish(draft(repo, 2, 20), requires=(n,) if root == 'dependency' else ())
    for _ in range(10):
        result = repo.collect(page_size=1)
        assert all(c['examined'] <= 1 for c in result.values())
    assert state(repo, n) == before
    if token:
        repo.release(token)
    if private:
        repo.discard(private)
    if root == 'dependency':
        repo.publish(draft(repo, 3, 30))
    for _ in range(30):
        repo.collect(page_size=2)
    with pytest.raises(Unavailable):
        state(repo, n)
    assert repo.read(repo.head('main'), 'current', 'day')['rows']
    assert sql(repo, 'PRAGMA foreign_key_check') == []


@pytest.mark.parametrize('stage', ['before_gc_commit', 'after_gc_commit'])
def test_gc_crash_preserves_pinned_reader(repo, stage):
    n = repo.publish(draft(repo, 1, 10))
    repo.pin(n, owner='reader')
    repo.publish(draft(repo, 2, 20))
    repo.publish(draft(repo, 3, 30))
    code = """
import os,sys
from pathlib import Path
from projection_repository import ProjectionRepository
r=ProjectionRepository(Path(sys.argv[1]))
r.collect(fault=lambda at: os._exit(74) if at==sys.argv[2] else None)
"""
    child = subprocess.run([sys.executable, '-c', code, str(repo.path), stage], env=os.environ.copy(), timeout=15, capture_output=True)
    assert child.returncode == 74, child.stderr.decode()
    r = ProjectionRepository(repo.path)
    assert state(r, n) == [[('row', {'value': 1})]] * 2
    assert state(r, r.head('main')) == [[('row', {'value': 3})]] * 2
    assert sql(r, 'PRAGMA foreign_key_check') == []


def test_structural_limits_and_pagination(repo):
    w = repo.begin('main', expected_bundle=None)
    with pytest.raises(ValueError):
        repo.stage(w, 'p', 'd', {'': 1}, metadata={})
    with pytest.raises(ValueError):
        repo.stage(w, 'p', 'd', {'a': 'x' * 65536}, metadata={})
    repo.stage(w, 'p', 'd', {str(i): i for i in range(1024)}, metadata={})
    with pytest.raises(ValueError, match='limit'):
        repo.stage(w, 'p', 'd', {'extra': 1}, metadata={})
    n = repo.publish(w)
    first = repo.read(n, 'p', 'd', limit=10)['rows']
    second = repo.read(n, 'p', 'd', limit=10, after_key=first[-1][0])['rows']
    assert len(first) == len(second) == 10
    assert set(dict(first)).isdisjoint(dict(second))
    with pytest.raises(ValueError):
        repo.read(n, 'p', 'd', limit=1025)


def test_foreign_database_not_modified(tmp_path):
    path = tmp_path / 'foreign.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE foreign_data(value TEXT)')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='unsupported'):
        ProjectionRepository(path)
    assert path.read_bytes() == before


def test_no_runtime_import():
    app = Path(__file__).resolve().parents[1] / 'app'
    users = [p.name for p in app.glob('*.py') if p.name != 'projection_repository.py' and 'import projection_repository' in p.read_text()]
    assert users == []


def test_test_guard_denies_production_paths():
    # This attempt must be rejected by the preloaded test guard before filesystem IO.
    with pytest.raises(PermissionError):
        open('/share/guardian-phase2a-forbidden', 'rb')


def test_unrelated_partition_and_derived_bundle_preserve_input(repo):
    n = repo.publish(draft(repo, 1, 10))
    result = repo.begin('diagnostic', expected_bundle=None)
    repo.stage(result, 'diagnostic', 'day', {'result': 42}, metadata={'input_bundle': n})
    derived = repo.publish(result, requires=(n,))
    repo.publish(draft(repo, 2, 20))
    for _ in range(8):
        repo.collect()
    assert state(repo, n) == [[('row', {'value': 1})]] * 2
    assert repo.read(derived, 'diagnostic', 'day')['metadata']['input_bundle'] == n
    assert repo.read(derived, 'diagnostic', 'day')['rows'] == [('result', 42)]


# Large corrections use a complete partition stream, explicitly separate from
# the small delta contract. Existing small-delta tests above remain unchanged.
def replacement(repo, total=1301, page_size=127):
    work = repo.begin('main', expected_bundle=repo.head('main'))
    for product in ('current', 'aggregate'):
        checkpoint = repo.begin_replacement(work, product, 'day', metadata={'kind': 'complete replacement'})
        for start in range(0, total, page_size):
            checkpoint = repo.stage_replacement(work, product, 'day',
                {f'{i:06}': i for i in range(start, min(start + page_size, total))}, expected=checkpoint)
        repo.seal_replacement(work, product, 'day', expected=checkpoint)
        repo.stage_cursor(work, product, 'raw', 'r1', expected=repo.cursor(product, 'raw', 'r1'), next_offset=20)
    return work


def all_rows(repo, bundle, product='current'):
    result, key = [], ''
    while True:
        rows = repo.read(bundle, product, 'day', after_key=key, limit=97)['rows']
        if not rows:
            return result
        result.extend(rows)
        key = rows[-1][0]


def test_large_private_resume_no_premature_cursor_or_head(repo):
    n = repo.publish(draft(repo, 1, 10))
    repo.pin(n, owner='old reader')
    before = state(repo, n)
    work = repo.begin('main', expected_bundle=n)
    for product in ('current', 'aggregate'):
        progress = repo.begin_replacement(work, product, 'day', metadata={})
        for start in range(0, 2107, 113):
            # New repository instance after EVERY page, including >1024 rows.
            repo = ProjectionRepository(repo.path)
            assert repo.replacement_progress(work, product, 'day') == progress
            progress = repo.stage_replacement(work, product, 'day',
                {f'{i:06}': i for i in range(start, min(start + 113, 2107))}, expected=progress)
            assert repo.head('main') == n
            for p in ('current', 'aggregate'):
                assert repo.cursor(p, 'raw', 'r1') == Cursor(10, n)
            assert state(repo, n) == before
        assert progress.rows == 2107
        repo.seal_replacement(work, product, 'day', expected=progress)
        repo.stage_cursor(work, product, 'raw', 'r1', expected=Cursor(10, n), next_offset=20)
    # GC must preserve prepared private lineages, including sealed ones.
    for _ in range(7):
        repo.collect(page_size=1024)
    newer = repo.publish(work)
    expected = [(f'{i:06}', i) for i in range(2107)]
    for p in ('current', 'aggregate'):
        assert all_rows(repo, newer, p) == expected
        assert repo.cursor(p, 'raw', 'r1') == Cursor(20, newer)
    assert state(ProjectionRepository(repo.path), n) == before
    assert repo.inputs(n)[('aggregate', 'raw', 'r1')] == Cursor(10, n)


@pytest.mark.parametrize('stage', STAGES)
def test_large_finalize_process_crash(repo, stage):
    n = repo.publish(draft(repo, 1, 10))
    work = replacement(repo)
    code = """
import os,sys
from pathlib import Path
from projection_repository import ProjectionRepository
r=ProjectionRepository(Path(sys.argv[1]))
r.publish(sys.argv[2], fault=lambda at: os._exit(75) if at==sys.argv[3] else None)
"""
    child = subprocess.run([sys.executable, '-c', code, str(repo.path), work, stage],
                           env=os.environ.copy(), timeout=15, capture_output=True)
    assert child.returncode == 75, child.stderr.decode()
    r = ProjectionRepository(repo.path)
    committed = stage == 'after_commit'
    assert (r.head('main') != n) == committed
    for product in ('current', 'aggregate'):
        assert r.cursor(product, 'raw', 'r1').offset == (20 if committed else 10)
        if not committed:
            assert r.replacement_progress(work, product, 'day').rows == 1301
    assert state(r, n) == [[('row', {'value': 1})]] * 2
    if not committed:
        r.publish(work)
    else:
        with pytest.raises(Unavailable):
            r.publish(work)
    for product in ('current', 'aggregate'):
        assert all_rows(r, r.head('main'), product) == [(f'{i:06}', i) for i in range(1301)]
    assert sql(r, 'PRAGMA integrity_check') == [('ok',)]
    assert sql(r, 'PRAGMA foreign_key_check') == []


@pytest.mark.parametrize('stage', ['after_replacement_rows', 'after_replacement_checkpoint', 'after_replacement_commit'])
def test_replacement_page_process_crash_and_resume(repo, stage):
    n = repo.publish(draft(repo, 1, 10))
    work = repo.begin('main', expected_bundle=n)
    progress = repo.begin_replacement(work, 'current', 'day', metadata={})
    progress = repo.stage_replacement(work, 'current', 'day',
        {f'{i:06}': i for i in range(1024)}, expected=progress)
    code = """
import os,sys
from pathlib import Path
from projection_repository import ProjectionRepository
r=ProjectionRepository(Path(sys.argv[1])); w=sys.argv[2]
p=r.replacement_progress(w,'current','day')
r.stage_replacement(w,'current','day',{'001024':1024},expected=p,
 fault=lambda at: os._exit(76) if at==sys.argv[3] else None)
"""
    child = subprocess.run([sys.executable, '-c', code, str(repo.path), work, stage],
                           env=os.environ.copy(), timeout=15, capture_output=True)
    assert child.returncode == 76, child.stderr.decode()
    r = ProjectionRepository(repo.path)
    observed = r.replacement_progress(work, 'current', 'day')
    assert observed.rows == (1025 if stage == 'after_replacement_commit' else 1024)
    if observed.rows == 1024:
        observed = r.stage_replacement(work, 'current', 'day', {'001024': 1024}, expected=observed)
    else:
        with pytest.raises(Conflict):
            r.stage_replacement(work, 'current', 'day', {'001024': 1024}, expected=progress)
    r.seal_replacement(work, 'current', 'day', expected=observed)
    assert r.head('main') == n
    assert r.cursor('current', 'raw', 'r1') == Cursor(10, n)
    assert len(all_rows(r, r.publish(work))) == 1025


def test_replacement_requires_explicit_seal_and_monotonic_checkpoint(repo):
    from projection_repository import ReplacementProgress
    n = repo.publish(draft(repo, 1, 10))
    work = repo.begin('main', expected_bundle=n)
    initial = repo.begin_replacement(work, 'current', 'day', metadata={})
    progress = repo.stage_replacement(work, 'current', 'day', {'b': 2}, expected=initial)
    for rows, expected in [({'c': 3}, initial), ({'a': 1}, progress), ({'b': 2}, progress)]:
        with pytest.raises(Conflict):
            repo.stage_replacement(work, 'current', 'day', rows, expected=expected)
    with pytest.raises(ValueError):
        repo.stage_replacement(work, 'current', 'day', {str(i): i for i in range(1025)}, expected=progress)
    with pytest.raises(Conflict, match='not sealed'):
        repo.publish(work)
    with pytest.raises(Conflict):
        repo.seal_replacement(work, 'current', 'day', expected=ReplacementProgress(2, 'c', False))
    with pytest.raises(Conflict):
        repo.stage(work, 'current', 'day', {'c': 3}, metadata={})
    sealed = repo.seal_replacement(work, 'current', 'day', expected=progress)
    with pytest.raises(Conflict):
        repo.stage_replacement(work, 'current', 'day', {'c': 3}, expected=sealed)
    newer = repo.publish(work)
    assert all_rows(repo, newer) == [('b', 2)]  # old key omitted, not inherited
    assert state(repo, n)[0] == [('row', {'value': 1})]


def test_small_deltas_after_large_replacement_do_not_copy_or_leak(repo):
    n = repo.publish(draft(repo, 1, 10))
    large = repo.publish(replacement(repo))
    repo.pin(large, owner='previous correction')
    count = sql(repo, 'SELECT COUNT(*) FROM value_version')[0][0]
    work = repo.begin('main', expected_bundle=large)
    repo.stage(work, 'current', 'day', {'000001': 9999, '000002': None, '000003': 3}, metadata={})
    newer = repo.publish(work)
    assert sql(repo, 'SELECT COUNT(*) FROM value_version')[0][0] == count + 1
    rows = dict(all_rows(repo, newer))
    assert len(rows) == 1300 and rows['000001'] == 9999 and '000002' not in rows
    assert 'row' not in rows  # prior lineage cannot leak into replacement
    assert dict(all_rows(repo, large))['000001'] == 1
    again = repo.publish(replacement(repo, total=1101))
    for _ in range(12):
        repo.collect(page_size=1024)
    assert all_rows(repo, large) == [(f'{i:06}', i) for i in range(1301)]
    assert all_rows(repo, again) == [(f'{i:06}', i) for i in range(1101)]


def test_discard_large_draft_gc_is_paged(repo):
    n = repo.publish(draft(repo, 1, 10))
    work = replacement(repo)
    repo.discard(work)
    for _ in range(50):
        stats = repo.collect(page_size=73)
        assert stats['value_version']['examined'] <= 73
    assert sql(repo, 'SELECT COUNT(*) FROM value_version') == [(2,)]
    assert state(repo, n) == [[('row', {'value': 1})]] * 2


def test_large_finalize_stale_head_leaves_prepared_data_private(repo):
    n = repo.publish(draft(repo, 1, 10))
    work = replacement(repo)
    newer = repo.publish(draft(repo, 3, 30))
    with pytest.raises(Conflict, match='head'):
        repo.publish(work)
    assert repo.replacement_progress(work, 'current', 'day').rows == 1301
    assert repo.head('main') == newer
    assert repo.cursor('current', 'raw', 'r1') == Cursor(30, newer)
    assert state(repo, n) == [[('row', {'value': 1})]] * 2


def test_finalize_work_independent_of_prepared_row_count(tmp_path):
    from contextlib import contextmanager
    class Instrumented(ProjectionRepository):
        @contextmanager
        def _connection(self):
            with super()._connection() as db:
                self.statements = []
                self.vm_steps = 0
                def step():
                    self.vm_steps += 1
                    return 0
                db.set_trace_callback(self.statements.append)
                db.set_progress_handler(step, 1)
                yield db
                self.writes = db.total_changes
    measurements = []
    for total in (129, 4097):
        r = Instrumented(tmp_path / f'{total}.sqlite')
        r.register_source('raw', 'r1', metadata={}, durable_end=100)
        r.publish(draft(r, 1, 10))
        work = replacement(r, total=total)
        r.publish(work)
        # No payload DML/selection in large finalization; only bounded manifests.
        assert all('value_version' not in q.lower() for q in r.statements)
        measurements.append((r.writes, r.vm_steps, len(r.statements)))
        assert len(all_rows(r, r.head('main'))) == total
    # Local SQL VM work, not a disk-I/O/HA latency or total-preparation bound.
    print('finalize_sql_measurements=' + json.dumps({
        'rows_per_product': [129, 4097], 'products': 2,
        'columns': ['changed_rows', 'sqlite_vm_steps', 'traced_statements'],
        'measurements': measurements}))
    assert measurements[0] == measurements[1]


def test_kernel_v1_rejected_without_mutation(tmp_path):
    path = tmp_path / 'unreleased-v1.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE schema_info(version INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO schema_info VALUES(1)')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='unsupported'):
        ProjectionRepository(path)
    assert path.read_bytes() == before


def test_empty_replacement_retains_old_generation(repo):
    n = repo.publish(draft(repo, 1, 10))
    w = repo.begin('main', expected_bundle=n)
    empty = repo.begin_replacement(w, 'current', 'day', metadata={'coverage': 'explicitly empty'})
    repo.seal_replacement(w, 'current', 'day', expected=empty)
    newer = repo.publish(w)
    assert all_rows(repo, newer) == []
    assert state(repo, n) == [[('row', {'value': 1})]] * 2
    assert repo.read(newer, 'aggregate', 'day')['generation'] == n


def test_mixed_small_delta_and_large_replacement_commit_together(repo):
    n = repo.publish(draft(repo, 1, 10))
    w = repo.begin('main', expected_bundle=n)
    p = repo.begin_replacement(w, 'current', 'day', metadata={})
    for start in range(0, 1200, 100):
        p = repo.stage_replacement(w, 'current', 'day',
            {f'{i:06}': i for i in range(start, start + 100)}, expected=p)
    repo.stage(w, 'aggregate', 'day', {'row': 1200}, metadata={})
    for product in ('current', 'aggregate'):
        repo.stage_cursor(w, product, 'raw', 'r1', expected=Cursor(10, n), next_offset=20)
    with pytest.raises(Conflict, match='not sealed'):
        repo.publish(w)
    assert repo.head('main') == n
    assert state(repo, n) == [[('row', {'value': 1})]] * 2
    repo.seal_replacement(w, 'current', 'day', expected=p)
    newer = repo.publish(w)
    assert len(all_rows(repo, newer)) == 1200
    assert repo.read(newer, 'aggregate', 'day')['rows'] == [('row', 1200)]
    for product in ('current', 'aggregate'):
        assert repo.cursor(product, 'raw', 'r1') == Cursor(20, newer)
