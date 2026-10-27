"""Standalone transactional projection kernel; deliberately not wired to runtime.

Raw files and application semantics are outside this module. Callers explicitly
register confirmed source bounds. Published values are immutable MVCC rows;
work drafts, cursors, and heads are not publications. No implicit production path.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


class Conflict(RuntimeError):
    """The expected confirmed state no longer matches."""


class Unavailable(RuntimeError):
    """Requested publication is absent or has been collected."""


MAX_ITEMS = 1024  # Structural batch/page limits, not production latency budgets.
MAX_JSON_BYTES = 65536


def _json(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_JSON_BYTES:
        raise ValueError("JSON value exceeds structural limit")
    return encoded


def _bounded(db, table, where, args):
    # LIMIT bounds even the limit check; no unbounded COUNT/materialization.
    rows = db.execute(f"SELECT * FROM {table} WHERE {where} LIMIT ?", (*args, MAX_ITEMS + 1)).fetchall()
    if len(rows) > MAX_ITEMS:
        raise ValueError("structural batch limit exceeded")
    return rows


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class ReplacementProgress:
    rows: int
    last_key: str
    sealed: bool


@dataclass(frozen=True)
class Cursor:
    offset: int
    generation: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info(version INTEGER PRIMARY KEY CHECK(version=2));
INSERT OR IGNORE INTO schema_info VALUES(2);
CREATE TABLE IF NOT EXISTS source_revision(
 source TEXT, revision TEXT, metadata TEXT NOT NULL, durable_end INTEGER NOT NULL CHECK(durable_end>=0),
 PRIMARY KEY(source,revision));
CREATE TABLE IF NOT EXISTS generation(id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE IF NOT EXISTS publication(
 id INTEGER PRIMARY KEY, product TEXT NOT NULL, partition_key TEXT NOT NULL,
 lineage INTEGER NOT NULL REFERENCES generation,
 read_gen INTEGER NOT NULL REFERENCES generation, metadata TEXT NOT NULL,
 UNIQUE(product,partition_key,read_gen));
CREATE TABLE IF NOT EXISTS value_version(
 id INTEGER PRIMARY KEY, product TEXT NOT NULL, partition_key TEXT NOT NULL, row_key TEXT NOT NULL,
 lineage INTEGER NOT NULL REFERENCES generation,
 born INTEGER NOT NULL REFERENCES generation, retired INTEGER REFERENCES generation,
 payload TEXT NOT NULL, digest TEXT NOT NULL, CHECK(retired IS NULL OR retired>born),
 UNIQUE(product,partition_key,lineage,row_key,born));
CREATE UNIQUE INDEX IF NOT EXISTS one_current_value ON value_version(product,partition_key,lineage,row_key)
 WHERE retired IS NULL;
CREATE INDEX IF NOT EXISTS visible_values ON value_version(product,partition_key,lineage,row_key,born,retired);
CREATE INDEX IF NOT EXISTS publication_range ON publication(product,partition_key,lineage,read_gen);
CREATE TABLE IF NOT EXISTS partition_tip(product TEXT, partition_key TEXT,
 publication_id INTEGER NOT NULL REFERENCES publication, PRIMARY KEY(product,partition_key));
CREATE INDEX IF NOT EXISTS tip_publication ON partition_tip(publication_id);
CREATE TABLE IF NOT EXISTS bundle(id INTEGER PRIMARY KEY REFERENCES generation);
CREATE TABLE IF NOT EXISTS member(bundle_id INTEGER REFERENCES bundle ON DELETE CASCADE,
 product TEXT, partition_key TEXT, publication_id INTEGER REFERENCES publication,
 PRIMARY KEY(bundle_id,product,partition_key));
CREATE INDEX IF NOT EXISTS member_publication ON member(publication_id);
CREATE TABLE IF NOT EXISTS bundle_cursor(bundle_id INTEGER REFERENCES bundle ON DELETE CASCADE,
 product TEXT, source TEXT, revision TEXT, offset INTEGER NOT NULL,
 generation INTEGER NOT NULL REFERENCES generation,
 PRIMARY KEY(bundle_id,product,source,revision));
CREATE TRIGGER IF NOT EXISTS immutable_bundle_cursor BEFORE UPDATE ON bundle_cursor
 BEGIN SELECT RAISE(ABORT,'bundle cursor immutable'); END;
CREATE TABLE IF NOT EXISTS head(name TEXT PRIMARY KEY, bundle_id INTEGER NOT NULL REFERENCES bundle);
CREATE INDEX IF NOT EXISTS head_bundle ON head(bundle_id);
CREATE TABLE IF NOT EXISTS pin(token TEXT PRIMARY KEY, bundle_id INTEGER NOT NULL REFERENCES bundle,
 owner TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pin_bundle ON pin(bundle_id);
CREATE TABLE IF NOT EXISTS dependency(bundle_id INTEGER REFERENCES bundle ON DELETE CASCADE,
 required_bundle INTEGER REFERENCES bundle, CHECK(required_bundle<bundle_id),
 PRIMARY KEY(bundle_id,required_bundle));
CREATE INDEX IF NOT EXISTS dependency_required ON dependency(required_bundle);
CREATE TABLE IF NOT EXISTS cursor_state(product TEXT, source TEXT, revision TEXT,
 offset INTEGER NOT NULL, generation INTEGER NOT NULL REFERENCES generation,
 PRIMARY KEY(product,source,revision), FOREIGN KEY(source,revision) REFERENCES source_revision);
CREATE TABLE IF NOT EXISTS draft(id TEXT PRIMARY KEY, head_name TEXT NOT NULL,
 expected_bundle INTEGER REFERENCES bundle);
CREATE INDEX IF NOT EXISTS draft_base ON draft(expected_bundle);
CREATE TABLE IF NOT EXISTS draft_partition(draft_id TEXT REFERENCES draft ON DELETE CASCADE,
 product TEXT, partition_key TEXT, metadata TEXT NOT NULL,
 PRIMARY KEY(draft_id,product,partition_key));
CREATE TABLE IF NOT EXISTS draft_value(draft_id TEXT REFERENCES draft ON DELETE CASCADE,
 product TEXT, partition_key TEXT, row_key TEXT, payload TEXT,
 PRIMARY KEY(draft_id,product,partition_key,row_key));
CREATE TABLE IF NOT EXISTS draft_cursor(draft_id TEXT REFERENCES draft ON DELETE CASCADE,
 product TEXT, source TEXT, revision TEXT, expected_offset INTEGER, expected_generation INTEGER,
 next_offset INTEGER NOT NULL CHECK(next_offset>=0), PRIMARY KEY(draft_id,product,source,revision));
CREATE TABLE IF NOT EXISTS replacement_work(
 draft_id TEXT REFERENCES draft ON DELETE CASCADE, product TEXT, partition_key TEXT,
 lineage INTEGER NOT NULL UNIQUE REFERENCES generation,
 row_count INTEGER NOT NULL DEFAULT 0 CHECK(row_count>=0), last_key TEXT NOT NULL DEFAULT '',
 sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1)),
 PRIMARY KEY(draft_id,product,partition_key));
CREATE TABLE IF NOT EXISTS gc_progress(kind TEXT PRIMARY KEY, after_id INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_value BEFORE UPDATE OF product,partition_key,lineage,row_key,born,payload,digest
 ON value_version BEGIN SELECT RAISE(ABORT,'published payload immutable'); END;
CREATE TRIGGER IF NOT EXISTS retire_once BEFORE UPDATE OF retired ON value_version
 WHEN OLD.retired IS NOT NULL OR NEW.retired IS NULL
 BEGIN SELECT RAISE(ABORT,'retirement immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_publication BEFORE UPDATE ON publication
 BEGIN SELECT RAISE(ABORT,'publication immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_member BEFORE UPDATE ON member
 BEGIN SELECT RAISE(ABORT,'bundle membership immutable'); END;
"""


class ProjectionRepository:
    def __init__(self, path: Path, *, timeout=1.0):
        self.path = Path(path)
        self.timeout = timeout
        # Parent creation and any runtime path choice belong to the caller.
        with self._connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and ('schema_info' not in tables or
                           [r[0] for r in db.execute("SELECT version FROM schema_info")] != [2]):
                raise ValueError("unsupported repository schema; no migration attempted")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self):
        from work_scheduler import atomic
        with atomic(), self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.IntegrityError) and str(exc).startswith('lifecycle '):
                    raise Conflict(str(exc)) from exc
                raise

    def register_source(self, source, revision, *, metadata, durable_end=0):
        encoded = _json(metadata)
        if not isinstance(durable_end, int) or isinstance(durable_end, bool) or durable_end < 0:
            raise ValueError("invalid source frontier")
        with self._write() as db:
            old = db.execute("SELECT * FROM source_revision WHERE source=? AND revision=?",
                             (source, revision)).fetchone()
            if old:
                if old['metadata'] != encoded or old['durable_end'] != durable_end:
                    raise Conflict("source revision already registered differently")
            else:
                db.execute("INSERT INTO source_revision VALUES(?,?,?,?)",
                           (source, revision, encoded, durable_end))

    def confirm_source(self, source, revision, *, expected_end, durable_end):
        if (not isinstance(durable_end, int) or isinstance(durable_end, bool)
                or durable_end < expected_end):
            raise ValueError("source frontier cannot move backwards")
        with self._write() as db:
            changed = db.execute("UPDATE source_revision SET durable_end=? WHERE source=? AND revision=? AND durable_end=?",
                                 (durable_end, source, revision, expected_end)).rowcount
            if changed != 1:
                raise Conflict("source frontier changed")

    def head(self, name):
        with self._connection() as db:
            row = db.execute("SELECT bundle_id FROM head WHERE name=?", (name,)).fetchone()
            return None if row is None else row[0]

    def cursor(self, product, source, revision):
        with self._connection() as db:
            row = db.execute("SELECT offset,generation FROM cursor_state WHERE product=? AND source=? AND revision=?",
                             (product, source, revision)).fetchone()
            return None if row is None else Cursor(*row)

    def begin(self, head_name, *, expected_bundle):
        work = uuid.uuid4().hex
        with self._write() as db:
            row = db.execute("SELECT bundle_id FROM head WHERE name=?", (head_name,)).fetchone()
            if (None if row is None else row[0]) != expected_bundle:
                raise Conflict("head changed before draft")
            db.execute("INSERT INTO draft VALUES(?,?,?)", (work, head_name, expected_bundle))
        return work

    def stage(self, work, product, partition, changes, *, metadata):
        """Stage only changed row keys; None explicitly retires a row."""
        if len(changes) > MAX_ITEMS or any(not isinstance(k, str) or not k or len(k) > 512 for k in changes):
            raise ValueError("invalid or oversized row batch")
        encoded = [(key, None if value is None else _json(value)) for key, value in changes.items()]
        with self._write() as db:
            if db.execute("SELECT 1 FROM replacement_work WHERE draft_id=? AND product=? AND partition_key=?",
                          (work, product, partition)).fetchone():
                raise Conflict("replacement partition cannot accept small deltas")
            db.execute("INSERT INTO draft_partition VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET metadata=excluded.metadata",
                       (work, product, partition, _json(metadata)))
            db.executemany("INSERT INTO draft_value VALUES(?,?,?,?,?) ON CONFLICT DO UPDATE SET payload=excluded.payload",
                           [(work, product, partition, key, value) for key, value in encoded])
            _bounded(db, 'draft_value', 'draft_id=?', (work,))
            _bounded(db, 'draft_partition', 'draft_id=?', (work,))

    def begin_replacement(self, work, product, partition, *, metadata):
        """Declare a complete replacement, not an overlay of the prior partition.

        Feed its full desired key-sorted contents in bounded pages. Omitted keys
        disappear only at publish. This explicit correction path may duplicate
        a full partition on disk; ordinary delta commits never do so.
        """
        with self._write() as db:
            if db.execute("SELECT 1 FROM draft_partition WHERE draft_id=? AND product=? AND partition_key=?",
                          (work, product, partition)).fetchone():
                raise Conflict("partition already staged")
            lineage = db.execute("INSERT INTO generation DEFAULT VALUES").lastrowid
            db.execute("INSERT INTO draft_partition VALUES(?,?,?,?)", (work, product, partition, _json(metadata)))
            _bounded(db, 'draft_partition', 'draft_id=?', (work,))
            db.execute("INSERT INTO replacement_work(draft_id,product,partition_key,lineage) VALUES(?,?,?,?)",
                       (work, product, partition, lineage))
        return ReplacementProgress(0, '', False)

    @staticmethod
    def _replacement(db, work, product, partition):
        row = db.execute("SELECT * FROM replacement_work WHERE draft_id=? AND product=? AND partition_key=?",
                         (work, product, partition)).fetchone()
        if row is None:
            raise Unavailable("replacement absent")
        return row

    @staticmethod
    def _progress(row):
        return ReplacementProgress(row['row_count'], row['last_key'], bool(row['sealed']))

    def replacement_progress(self, work, product, partition):
        """Durable page checkpoint; no aggregate count or payload scan."""
        with self._connection() as db:
            return self._progress(self._replacement(db, work, product, partition))

    def stage_replacement(self, work, product, partition, rows, *, expected, fault=lambda _stage: None):
        """Append one page and its resume checkpoint in the same transaction.

        Repeated/overlapping pages fail CAS; after an uncertain acknowledgement,
        inspect replacement_progress before retrying. Prepared payloads are not
        reachable from a publication. No confirmed input cursor is advanced.
        """
        if not 1 <= len(rows) <= MAX_ITEMS or any(
                not isinstance(k, str) or not k or len(k) > 512 for k in rows):
            raise ValueError("invalid or oversized replacement page")
        encoded = [(key, _json(value)) for key, value in sorted(rows.items())]
        with self._write() as db:
            row = self._replacement(db, work, product, partition)
            if self._progress(row) != expected or row['sealed']:
                raise Conflict("replacement checkpoint changed or sealed")
            if encoded[0][0] <= row['last_key']:
                raise Conflict("replacement pages must strictly advance keys")
            db.executemany("INSERT INTO value_version(product,partition_key,lineage,row_key,born,payload,digest) "
                           "VALUES(?,?,?,?,?,?,?)", [(product, partition, row['lineage'], key,
                           row['lineage'], value, _hash(value)) for key, value in encoded])
            fault('after_replacement_rows')
            progress = ReplacementProgress(row['row_count'] + len(encoded), encoded[-1][0], False)
            db.execute("UPDATE replacement_work SET row_count=?,last_key=? WHERE draft_id=? AND product=? AND partition_key=?",
                       (progress.rows, progress.last_key, work, product, partition))
            fault('after_replacement_checkpoint')
        fault('after_replacement_commit')
        return progress

    def seal_replacement(self, work, product, partition, *, expected):
        """Caller declares the complete output stream; seal does not scan it."""
        with self._write() as db:
            row = self._replacement(db, work, product, partition)
            if self._progress(row) != expected or row['sealed']:
                raise Conflict("replacement checkpoint changed or sealed")
            db.execute("UPDATE replacement_work SET sealed=1 WHERE draft_id=? AND product=? AND partition_key=?",
                       (work, product, partition))
        return ReplacementProgress(expected.rows, expected.last_key, True)

    def stage_cursor(self, work, product, source, revision, *, expected, next_offset):
        if (not isinstance(next_offset, int) or isinstance(next_offset, bool)
                or next_offset < (expected.offset if expected else 0)):
            raise ValueError("cursor cannot move backwards")
        with self._write() as db:
            db.execute("INSERT INTO draft_cursor VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                       "expected_offset=excluded.expected_offset,expected_generation=excluded.expected_generation,next_offset=excluded.next_offset",
                       (work, product, source, revision, None if expected is None else expected.offset,
                        None if expected is None else expected.generation, next_offset))
            _bounded(db, 'draft_cursor', 'draft_id=?', (work,))

    def discard(self, work):
        with self._write() as db:
            db.execute("DELETE FROM draft WHERE id=?", (work,))

    def publish(self, work, *, requires=(), fault=lambda _stage: None):
        """Atomically publish staged deltas, cursor advances and one product bundle.

        fault is an explicit test hook; no installed/runtime call site exists.
        """
        if len(requires) > MAX_ITEMS:
            raise ValueError("too many dependencies")
        with self._write() as db:
            gen = self._publish(db,work,requires=requires,fault=fault)
        fault('after_commit')
        return gen

    def _publish(self, db, work, *, requires=(), fault=lambda _stage: None):
        """Same commit body for an explicit adapter's additional atomic job cursor."""
        if len(requires) > MAX_ITEMS:
            raise ValueError("too many dependencies")
        draft = db.execute("SELECT * FROM draft WHERE id=?", (work,)).fetchone()
        if draft is None:
            raise Unavailable("draft absent")
        current = db.execute("SELECT bundle_id FROM head WHERE name=?", (draft['head_name'],)).fetchone()
        if (None if current is None else current[0]) != draft['expected_bundle']:
            raise Conflict("head compare-and-swap failed")
        members = {(r['product'], r['partition_key']): r['publication_id'] for r in _bounded(db, "member", "bundle_id=?", (draft['expected_bundle'],))}
        parts = _bounded(db, "draft_partition", "draft_id=?", (work,))
        cursors = _bounded(db, "draft_cursor", "draft_id=?", (work,))
        if not parts:
            raise ValueError("cursor-only publication prohibited")
        products = {p['product'] for p in parts}
        for c in cursors:
            if c['product'] not in products:
                raise ValueError("cursor product has no output partition")
            old = db.execute("SELECT offset,generation FROM cursor_state WHERE product=? AND source=? AND revision=?",
                             (c['product'], c['source'], c['revision'])).fetchone()
            expected = None if c['expected_offset'] is None else (c['expected_offset'], c['expected_generation'])
            if (None if old is None else tuple(old)) != expected:
                raise Conflict("cursor compare-and-swap failed")
            bound = db.execute("SELECT durable_end FROM source_revision WHERE source=? AND revision=?",
                               (c['source'], c['revision'])).fetchone()
            if bound is None or c['next_offset'] > bound[0]:
                raise Conflict("cursor exceeds confirmed source")
        gen = db.execute("INSERT INTO generation DEFAULT VALUES").lastrowid
        lineages = {}
        for p in parts:
            key = (p['product'], p['partition_key'])
            tip = db.execute("SELECT publication_id FROM partition_tip WHERE product=? AND partition_key=?", key).fetchone()
            if (None if tip is None else tip[0]) != members.get(key):
                raise Conflict("partition changed through another head")
            replacement = db.execute("SELECT lineage,sealed FROM replacement_work WHERE draft_id=? AND product=? AND partition_key=?",
                                     (work, *key)).fetchone()
            if replacement is not None:
                if not replacement['sealed']:
                    raise Conflict("replacement not sealed")
                lineages[key] = replacement['lineage']
                # Prepared rows already durable; finalization never loads/rewrites them.
                fault('after_partition_rows:' + p['product'])
                continue
            prior = None if tip is None else db.execute("SELECT lineage FROM publication WHERE id=?", (tip[0],)).fetchone()
            lineage = lineages[key] = gen if prior is None else prior[0]
            for row in db.execute("SELECT * FROM draft_value WHERE draft_id=? AND product=? AND partition_key=?",
                                  (work, *key)).fetchall():
                old = db.execute("SELECT payload FROM value_version WHERE product=? AND partition_key=? AND lineage=? AND row_key=? AND retired IS NULL",
                                 (*key, lineage, row['row_key'])).fetchone()
                if (old is None and row['payload'] is None) or (old is not None and old[0] == row['payload']):
                    continue  # Unchanged payload is shared; no full partition copy.
                db.execute("UPDATE value_version SET retired=? WHERE product=? AND partition_key=? AND lineage=? AND row_key=? AND retired IS NULL",
                           (gen, *key, lineage, row['row_key']))
                if row['payload'] is not None:
                    db.execute("INSERT INTO value_version(product,partition_key,lineage,row_key,born,payload,digest) VALUES(?,?,?,?,?,?,?)",
                               (*key, lineage, row['row_key'], gen, row['payload'], _hash(row['payload'])))
            fault('after_partition_rows:' + p['product'])
        fault('after_rows')
        for c in cursors:
            db.execute("INSERT INTO cursor_state VALUES(?,?,?,?,?) ON CONFLICT DO UPDATE SET offset=excluded.offset,generation=excluded.generation",
                       (c['product'], c['source'], c['revision'], c['next_offset'], gen))
            fault('after_partition_cursor:' + c['product'])
        fault('after_cursors')
        for p in parts:
            key = (p['product'], p['partition_key'])
            pub = db.execute("INSERT INTO publication(product,partition_key,lineage,read_gen,metadata) VALUES(?,?,?,?,?)",
                             (*key, lineages[key], gen, p['metadata'])).lastrowid
            members[key] = pub
            db.execute("INSERT INTO partition_tip VALUES(?,?,?) ON CONFLICT DO UPDATE SET publication_id=excluded.publication_id", (*key, pub))
            fault('after_partition_publication:' + p['product'])
        if len(members) > MAX_ITEMS:
            raise ValueError("bundle member limit exceeded")
        fault('after_publications')
        db.execute("INSERT INTO bundle VALUES(?)", (gen,))
        db.executemany("INSERT INTO member VALUES(?,?,?,?)", [(gen, *key, pub) for key, pub in members.items()])
        db.executemany("INSERT INTO dependency VALUES(?,?)", [(gen, required) for required in set(requires)])
        inputs = {(r['product'], r['source'], r['revision']): (r['offset'], r['generation'])
                  for r in _bounded(db, 'bundle_cursor', 'bundle_id=?', (draft['expected_bundle'],))}
        inputs.update({(c['product'], c['source'], c['revision']): (c['next_offset'], gen) for c in cursors})
        if len(inputs) > MAX_ITEMS:
            raise ValueError("bundle input limit exceeded")
        db.executemany("INSERT INTO bundle_cursor VALUES(?,?,?,?,?,?)",
                       [(gen, *key, *value) for key, value in inputs.items()])
        fault('after_bundle')
        db.execute("INSERT INTO head VALUES(?,?) ON CONFLICT DO UPDATE SET bundle_id=excluded.bundle_id", (draft['head_name'], gen))
        db.execute("DELETE FROM draft WHERE id=?", (work,))
        fault('after_head')
        return gen

    def inputs(self, bundle):
        """Immutable confirmed cursor vector; never substitute current cursors."""
        with self._connection() as db:
            db.execute("BEGIN")
            if db.execute("SELECT 1 FROM bundle WHERE id=?", (bundle,)).fetchone() is None:
                raise Unavailable("bundle absent or collected")
            return {(r['product'], r['source'], r['revision']): Cursor(r['offset'], r['generation'])
                    for r in _bounded(db, 'bundle_cursor', 'bundle_id=?', (bundle,))}

    def pin(self, bundle, *, owner):
        token = uuid.uuid4().hex
        with self._write() as db:
            if db.execute("SELECT 1 FROM bundle WHERE id=?",(bundle,)).fetchone() is None:
                raise Unavailable('bundle absent or collected')
            db.execute("INSERT INTO pin VALUES(?,?,?)", (token, bundle, owner))
        return token

    def release(self, token):
        with self._write() as db:
            # The holder may always release its exact token, also after revocation.
            managed=db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_delete_permit'").fetchone()
            key=_json([token])
            if managed:db.execute('INSERT OR IGNORE INTO lifecycle_delete_permit VALUES(?,?)',('pin',key))
            db.execute("DELETE FROM pin WHERE token=?", (token,))
            if managed:db.execute('DELETE FROM lifecycle_delete_permit WHERE table_name=? AND object_key=?',('pin',key))

    @contextmanager
    def lease(self, bundle, *, owner):
        token=self.pin(bundle,owner=owner)
        from lifecycle_leases import read_scope
        try:
            with read_scope(self,token):
                yield token
        finally:
            self.release(token)

    def read(self, bundle, product, partition, *, after_key='', limit=256):
        """One bounded page in a new transaction, even after newer publications.

        Callers pin a bundle across pages. Absent/unpinned collected bundles fail
        explicitly; no fallback to a newer head. No network or runtime IO here.
        """
        if not 1 <= limit <= 1024:
            raise ValueError("page limit must be 1..1024")
        with self._connection() as db:
            db.execute("BEGIN")
            pub = db.execute("SELECT p.* FROM member m JOIN publication p ON p.id=m.publication_id "
                             "WHERE m.bundle_id=? AND m.product=? AND m.partition_key=?", (bundle, product, partition)).fetchone()
            if pub is None:
                raise Unavailable("publication absent or collected")
            rows = db.execute("SELECT row_key,payload,digest FROM value_version WHERE product=? AND partition_key=? "
                              "AND lineage=? AND row_key>? AND born<=? AND (retired IS NULL OR retired>?) ORDER BY row_key LIMIT ?",
                              (product, partition, pub['lineage'], after_key, pub['read_gen'], pub['read_gen'], limit)).fetchall()
            for row in rows:
                if _hash(row['payload']) != row['digest']:
                    raise Unavailable("published payload corrupt")
            return {'publication': pub['id'], 'generation': pub['read_gen'], 'metadata': json.loads(pub['metadata']),
                    'rows': [(row['row_key'], json.loads(row['payload'])) for row in rows]}

    def collect(self, *, page_size=64, fault=lambda _stage: None):
        """Bound candidate metadata per call; protect heads, pins, drafts and DAG refs.

        Checkpointed candidate pages avoid repeated full inventory scans. No raw
        files, no VACUUM, no automatic retention policy or lease expiry.
        """
        if not 1 <= page_size <= 1024:
            raise ValueError("invalid GC page")
        counts = {}
        with self._write() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_layout'").fetchone():
                raise Conflict('explicit lifecycle owns retention; legacy collect is disabled')
            for table in ('bundle', 'publication', 'value_version'):
                prev = db.execute("SELECT after_id FROM gc_progress WHERE kind=?", (table,)).fetchone()
                ids = [row[0] for row in db.execute(f"SELECT id FROM {table} WHERE id>? ORDER BY id LIMIT ?", (prev[0] if prev else 0, page_size))]
                removed = 0
                for ident in ids:
                    if table == 'bundle':
                        protected = db.execute("SELECT 1 WHERE EXISTS(SELECT 1 FROM head WHERE bundle_id=?) "
                            "OR EXISTS(SELECT 1 FROM pin WHERE bundle_id=?) OR EXISTS(SELECT 1 FROM draft WHERE expected_bundle=?) "
                            "OR EXISTS(SELECT 1 FROM dependency WHERE required_bundle=?)", (ident,)*4).fetchone()
                    elif table == 'publication':
                        protected = db.execute("SELECT 1 WHERE EXISTS(SELECT 1 FROM member WHERE publication_id=?) "
                            "OR EXISTS(SELECT 1 FROM partition_tip WHERE publication_id=?)", (ident, ident)).fetchone()
                    else:
                        protected = db.execute("SELECT 1 FROM value_version v WHERE v.id=? AND ("
                            "EXISTS(SELECT 1 FROM replacement_work w WHERE w.lineage=v.lineage) OR "
                            "EXISTS(SELECT 1 FROM publication p WHERE p.product=v.product "
                            "AND p.partition_key=v.partition_key AND p.lineage=v.lineage AND p.read_gen>=v.born "
                            "AND (v.retired IS NULL OR p.read_gen<v.retired)))", (ident,)).fetchone()
                    if not protected:
                        removed += db.execute(f"DELETE FROM {table} WHERE id=?", (ident,)).rowcount
                db.execute("INSERT INTO gc_progress VALUES(?,?) ON CONFLICT DO UPDATE SET after_id=excluded.after_id", (table, ids[-1] if ids else 0))
                counts[table] = {'examined': len(ids), 'removed': removed}
            fault("before_gc_commit")
        fault("after_gc_commit")
        return counts
