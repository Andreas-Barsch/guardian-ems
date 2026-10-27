"""Explicit local persistent composition of the real diagnostic stores.

No runtime paths, discovery, thread starts or production factory selection.
A private generation holds changed rows; only a kernel bundle exposes the pair
and its source cursor. Retention work is resumable before that publication.
"""
from collections.abc import MutableMapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
import time
import uuid

from cell_diagnostics import CellDiagnosticStore, CellSample
from diagnostic_aggregates import DiagnosticAggregateStore
from current_condition_backfill import CurrentConditionBackfill
from projection_repository import Conflict, Unavailable, _json
from cell_source_resolver import CellSourceResolver, SourceRef
from research_budget import check_budget
from diagnostic_work_budget import work_budget

PAGE = 32
SCHEMA = '''
CREATE TABLE IF NOT EXISTS diagnostic_composition(
 name TEXT PRIMARY KEY, ingestion TEXT NOT NULL, options TEXT NOT NULL,
 max_samples INTEGER NOT NULL, retention INTEGER NOT NULL, bundle INTEGER,
 private_gen INTEGER, work TEXT, pending TEXT, cutoff TEXT, version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS diagnostic_row(
 name TEXT, kind TEXT, key TEXT, born INTEGER, retired INTEGER,
 group_key TEXT, ordering REAL, module INTEGER, stamp REAL, day TEXT,
 payload TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(name,kind,key,born));
CREATE INDEX IF NOT EXISTS diagnostic_visible ON diagnostic_row(name,kind,key,born,retired);
CREATE UNIQUE INDEX IF NOT EXISTS diagnostic_current ON diagnostic_row(name,kind,key) WHERE retired IS NULL;
CREATE INDEX IF NOT EXISTS diagnostic_ring ON diagnostic_row(name,kind,group_key,ordering,key,born,retired);
CREATE INDEX IF NOT EXISTS diagnostic_group ON diagnostic_row(name,kind,group_key,key,born,retired);
CREATE INDEX IF NOT EXISTS diagnostic_active_ring ON diagnostic_row(name,kind,group_key,ordering,key) WHERE retired IS NULL;
CREATE INDEX IF NOT EXISTS diagnostic_slot ON diagnostic_row(name,kind,module,stamp,born,retired);
CREATE INDEX IF NOT EXISTS diagnostic_prune ON diagnostic_row(name,kind,day,key) WHERE retired IS NULL;
CREATE TABLE IF NOT EXISTS diagnostic_analysis_job(
 id TEXT PRIMARY KEY,name TEXT,bundle INTEGER,options TEXT,modules TEXT,request_key TEXT,
 maintenance TEXT,position INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,
 after_sample INTEGER NOT NULL DEFAULT 0,after_key TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS diagnostic_analysis_request ON diagnostic_analysis_job(name,request_key);
CREATE TABLE IF NOT EXISTS diagnostic_analysis_result(
 job TEXT,module INTEGER,part TEXT,payload TEXT,PRIMARY KEY(job,module,part));
CREATE TABLE IF NOT EXISTS diagnostic_condition_cache(
 sample_hash TEXT,options_hash TEXT,payload TEXT NOT NULL,digest TEXT NOT NULL,
 PRIMARY KEY(sample_hash,options_hash));
'''


def decode(row):
    if hashlib.sha256(row['payload'].encode()).hexdigest() != row['digest']:
        raise Unavailable('diagnostic row checksum mismatch')
    return json.loads(row['payload'])


class Rows(MutableMapping):
    """An MVCC mapping with a bounded per-step write set, not a whole-cache copy."""
    def __init__(self, view, kind):
        self.view, self.kind, self.dirty = view, kind, {}

    def __getitem__(self, key):
        key = str(key)
        if key in self.dirty:
            if self.dirty[key] is None: raise KeyError(key)
            return self.dirty[key]
        row = self.view.db.execute(
            'SELECT * FROM diagnostic_row WHERE name=? AND kind=? AND key=? '
            'AND born<=? ORDER BY born DESC LIMIT 1',
            (self.view.name, self.kind, key, self.view.gen)).fetchone()
        if row is None or (row['retired'] is not None and row['retired'] <= self.view.gen):
            raise KeyError(key)
        return decode(row)

    def __setitem__(self, key, value):
        if not self.view.writable: raise TypeError('immutable checkpoint view')
        key = str(key)
        if key not in self.dirty and len(self.dirty) >= 128:
            raise ValueError('diagnostic delta write-set budget')
        self.dirty[key] = value

    def __delitem__(self, key):
        self[key]
        self[key] = None

    def __iter__(self):
        if self.dirty: raise RuntimeError('flush delta before iteration')
        after = ''
        while True:
            rows = self.view.db.execute(
                'SELECT key FROM diagnostic_row WHERE name=? AND kind=? AND key>? '
                'AND born<=? AND (retired IS NULL OR retired>?) ORDER BY key LIMIT ?',
                (self.view.name, self.kind, after, self.view.gen, self.view.gen, PAGE)).fetchall()
            for row in rows: yield int(row[0]) if self.kind == 'latest' else row[0]
            if len(rows) < PAGE: return
            after = rows[-1][0]

    def __len__(self):
        # Used by explicit diagnostic analysis, never by delta acquisition.
        return sum(1 for _ in self)

    def records_for_identity(self, module, serial):
        identity = serial or f"UNKNOWN-POSITION-{int(module)}"
        after = ''
        while True:
            rows = self.view.db.execute(
                'SELECT * FROM diagnostic_row WHERE name=? AND kind=? AND group_key=? AND key>? '
                'AND born<=? AND (retired IS NULL OR retired>?) ORDER BY key LIMIT ?',
                (self.view.name,self.kind,identity,after,self.view.gen,self.view.gen,PAGE)).fetchall()
            for row in rows: yield decode(row)
            if len(rows)<PAGE: return
            after=rows[-1]['key']

    def prune_records(self, latest_day, retention_days):
        self.view.cutoff = (datetime.fromisoformat(latest_day).date()
                            - timedelta(days=retention_days - 1)).isoformat()

    def flush(self):
        for key, value in self.dirty.items(): self.view.put(self.kind, key, value)
        self.dirty.clear()


class Ring(Sequence):
    def __init__(self, view, serial, unknown=False):
        self.view, self.serial, self.unknown = view, serial, unknown
        self.kind = 'unknown_ring' if unknown else 'identity_ring'
        self.group = hashlib.sha256(_json([self.kind, serial]).encode()).hexdigest()

    def _meta(self):
        return self.view.maps[self.kind].get(self.serial, {'count': 0, 'next': 0, 'created': self.view.gen})

    def __len__(self): return self._meta()['count']

    def __iter__(self):
        first = self._meta().get('first')
        if first is None: return
        after = (first[0], '')
        while True:
            rows = self.view.db.execute(
                'SELECT * FROM diagnostic_row INDEXED BY diagnostic_ring WHERE name=? AND kind=\'sample\' AND group_key=? '
                'AND (ordering,key)>(?,?) AND born<=? AND (retired IS NULL OR retired>?) '
                'ORDER BY ordering,key LIMIT ?',
                (self.view.name, self.group, *after, self.view.gen, self.view.gen, PAGE)).fetchall()
            for row in rows: yield decode(row)
            if len(rows) < PAGE: return
            after = (rows[-1]['ordering'], rows[-1]['key'])

    def __getitem__(self, index):
        if isinstance(index, slice): return list(self)[index]
        count = len(self)
        if index < 0: index += count
        if not 0 <= index < count: raise IndexError(index)
        row = self.view.db.execute(
            'SELECT * FROM diagnostic_row INDEXED BY diagnostic_ring WHERE name=? AND kind=\'sample\' AND group_key=? '
            'AND (ordering,key)>= (?,?) AND born<=? AND (retired IS NULL OR retired>?) ORDER BY ordering,key LIMIT 1 OFFSET ?',
            (self.view.name, self.group, *self._meta()['first'], self.view.gen, self.view.gen, index)).fetchone()
        if row is None: raise Unavailable('incomplete diagnostic ring')
        return decode(row)

    def append(self, value):
        meta = dict(self._meta()); seq = meta['next']; meta['next'] += 1
        key = self.group + ':' + f'{seq:020d}'
        order = value['timestamp'] if self.view.ingestion == 'history' else seq
        self.view.last_sample_ref = (key,self.view.gen)
        self.view.put('sample', key, value, group=self.group, ordering=order)
        meta['count'] += 1
        if meta['count'] > self.view.maximum:
            row = self.view.db.execute(
                'SELECT key FROM diagnostic_row INDEXED BY diagnostic_active_ring WHERE name=? AND kind=\'sample\' AND group_key=? '
                'AND retired IS NULL ORDER BY ordering,key LIMIT 1',
                (self.view.name, self.group)).fetchone()
            self.view.put('sample', row[0], None); meta['count'] -= 1
        first = self.view.db.execute(
            "SELECT ordering,key FROM diagnostic_row INDEXED BY diagnostic_active_ring WHERE name=? AND kind='sample' AND group_key=? AND retired IS NULL ORDER BY ordering,key LIMIT 1",
            (self.view.name, self.group)).fetchone()
        meta['first'] = list(first) if first else None
        self.view.maps[self.kind][self.serial] = meta


class Rings(MutableMapping):
    def __init__(self, view, unknown=False):
        self.view, self.unknown = view, unknown
        self.kind = 'unknown_ring' if unknown else 'identity_ring'
    def __getitem__(self, key): return Ring(self.view, key, self.unknown)
    def get(self, key, default=None):
        return self[key] if str(key) in self.view.maps[self.kind] else default
    def __iter__(self):
        after = (-1, '')
        while True:
            rows = self.view.db.execute(
                'SELECT key,ordering FROM diagnostic_row WHERE name=? AND kind=? '
                'AND (ordering,key)>(?,?) AND born<=? AND (retired IS NULL OR retired>?) '
                'ORDER BY ordering,key LIMIT ?',
                (self.view.name, self.kind, *after, self.view.gen, self.view.gen, PAGE)).fetchall()
            for row in rows: yield int(row['key']) if self.unknown else row['key']
            if len(rows) < PAGE: return
            after = (rows[-1]['ordering'], rows[-1]['key'])
    def __len__(self): return len(self.view.maps[self.kind])
    def __setitem__(self, key, value): raise TypeError('append through persistent ring')
    def __delitem__(self, key): raise TypeError('ring removal needs explicit migration')


class View:
    def __init__(self, db, name, gen, maximum, retention, ingestion, *, writable=False):
        self.db, self.name, self.gen = db, name, gen
        self.maximum, self.retention, self.ingestion = maximum, retention, ingestion
        self.writable, self.cutoff = writable, None
        self.writes = 0
        self.maps = {kind: Rows(self, kind) for kind in
                     ('identity_ring', 'unknown_ring', 'latest', 'aggregate', 'sources', 'coverage', 'positions', 'quality')}
        self.cells = CellDiagnosticStore.in_memory(maximum)
        self.cells.identity_samples = Rings(self)
        self.cells.unknown_samples = Rings(self, True)
        self.cells._latest_by_position = self.maps['latest']
        self.cells.set_current_identities(self.maps['positions'].get('current', {}))
        self.cells.rebuild_sources = self.maps['sources']
        self.cells.expected_materialized_coverage = self.maps['coverage']
        self.aggregates = DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases, retention)
        self.aggregates.records = self.maps['aggregate']

    def put(self, kind, key, value, *, group=None, ordering=None):
        if not self.writable: raise TypeError('immutable checkpoint view')
        self.writes += 1
        if self.writes > 256: raise ValueError('diagnostic step mutation budget')
        # A private generation may be resumed and edit its own unpublished rows.
        if value is not None:
            self.db.execute('DELETE FROM diagnostic_row WHERE name=? AND kind=? AND key=? AND born=?',
                            (self.name, kind, str(key), self.gen))
        self.db.execute('UPDATE diagnostic_row SET retired=? WHERE name=? AND kind=? AND key=? AND retired IS NULL',
                        (self.gen, self.name, kind, str(key)))
        if value is None: return
        if kind in ('identity_ring', 'unknown_ring'): ordering = value['created']
        if kind == 'history_slot': group,ordering = value['serial'],value['timestamp']
        if kind == 'aggregate': group = value.get('physical_module_serial') or f"UNKNOWN-POSITION-{value['module_position']}"
        payload = _json(value)
        self.db.execute('INSERT INTO diagnostic_row VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (self.name, kind, str(key), self.gen, None, group, ordering,
                         value.get('module'), value.get('timestamp'), value.get('day'),
                         payload, hashlib.sha256(payload.encode()).hexdigest()))

    def sample_page(self, module, after=None, *, missing_options=None):
        serial = self.cells.current_serial(module)
        if not serial and self.cells._latest_by_position.get(module) is None:
            return [], None, True
        ring = Ring(self, serial if serial else module, not bool(serial))
        first = ring._meta().get('first')
        if first is None: return [], None, True
        # Same unknown-after-last-documented selection as values_for_module.
        threshold = -float('inf')
        if not serial:
            row = self.db.execute(
                "SELECT MAX(stamp) FROM diagnostic_row WHERE name=? AND kind='sample' AND module=? AND born<=? AND (retired IS NULL OR retired>?) AND json_extract(payload,'$.module_serial') IS NOT NULL",
                (self.name,module,self.gen,self.gen)).fetchone()
            if row[0] is not None: threshold = row[0]
        after = tuple(after) if after else (first[0], '')
        query = "SELECT * FROM diagnostic_row INDEXED BY diagnostic_ring WHERE name=? AND kind='sample' AND group_key=? AND (ordering,key)>(?,?) AND stamp>? AND born<=? AND (retired IS NULL OR retired>?)"
        params = [self.name,ring.group,*after,threshold,self.gen,self.gen]
        if missing_options is not None:
            query += ' AND NOT EXISTS(SELECT 1 FROM diagnostic_condition_cache c WHERE c.sample_hash=diagnostic_row.digest AND c.options_hash=?)'
            params.append(missing_options)
        query += ' ORDER BY ordering,key LIMIT ?';params.append(PAGE)
        rows = self.db.execute(query,params).fetchall()
        return ([decode(row) for row in rows],
                None if not rows else [rows[-1]['ordering'],rows[-1]['key']],len(rows)<PAGE)

    def flush(self):
        for mapping in self.maps.values(): mapping.flush()


class DiagnosticRepository:
    def __init__(self, repository, catalog, epochs=None):
        self.repository, self.catalog, self.epochs = repository, catalog, epochs
        self.reader = CellSourceResolver(catalog)
        self.last_transaction = None

    @contextmanager
    def _write(self):
        waiting = time.monotonic(); entered = None; instructions = 0; failure = []
        with self.repository._write() as db:
            entered = time.monotonic()
            def budget():
                nonlocal instructions
                instructions += 100
                if instructions > 200000:
                    failure.append(Unavailable('diagnostic transaction SQL budget (200000)'))
                    return 1
                if instructions % 1000 == 0:
                    try: check_budget()
                    except BaseException as exc:
                        failure.append(exc);return 1
                return 0
            db.set_progress_handler(budget, 100)
            try:
                yield db
            except sqlite3.OperationalError:
                if failure: raise failure[0]
                raise
            finally:
                db.set_progress_handler(None, 0)
        from work_scheduler import account
        account(sql_vm_steps_approx=instructions, sql_lock_wait_seconds=entered-waiting)
        self.last_transaction = {'lock_wait_seconds': entered-waiting,
                                 'transaction_seconds': time.monotonic()-entered,
                                 'sql_vm_steps_approx': instructions}


    def initialize(self):
        with self.repository._connection() as db: db.executescript('BEGIN IMMEDIATE;' + SCHEMA + 'COMMIT;')

    @staticmethod
    def head_name(name): return 'diagnostic:' + name
    @staticmethod
    def product(name): return 'diagnostic:' + name

    def state(self, name):
        with self.repository._connection() as db:
            row = db.execute('SELECT * FROM diagnostic_composition WHERE name=?', (name,)).fetchone()
            if row is None: raise Unavailable('diagnostic composition absent')
            return dict(row)

    def create(self, name, options, *, ingestion):
        if ingestion not in ('live', 'history'): raise ValueError('explicit live/history sample semantics required')
        maximum = int(options['cell_diag_history_max_samples'])
        if maximum < 1: raise ValueError('positive retention required')
        retention = max(30, int(options.get('cell_diag_aggregate_retention_days', 730)))
        with self._write() as db:
            db.execute('INSERT INTO diagnostic_composition(name,ingestion,options,max_samples,retention) VALUES(?,?,?,?,?)',
                       (name, ingestion, _json(options), maximum, retention))
        self._new_work(name)
        return self.publish(name)

    def _new_work(self, name):
        row = self.state(name)
        if row['work']: return row
        with self._write() as db:
            current = db.execute('SELECT version FROM diagnostic_composition WHERE name=?', (name,)).fetchone()
            if current[0] != row['version']: raise Conflict('diagnostic state changed')
            gen = db.execute('INSERT INTO generation DEFAULT VALUES').lastrowid
            work = uuid.uuid4().hex
            db.execute('INSERT INTO draft VALUES(?,?,?)', (work, self.head_name(name), row['bundle']))
            db.execute('UPDATE diagnostic_composition SET private_gen=?,work=?,version=version+1 WHERE name=?',
                       (gen, work, name))
        return self.state(name)

    @contextmanager
    def view(self, name, *, bundle=None):
        row = self.state(name)
        bundle = row['bundle'] if bundle is None else bundle
        if bundle is None: raise Unavailable('no complete diagnostic pair')
        with self.repository.lease(bundle,owner='diagnostic-reader'):
            a = self.repository.read(bundle, self.product(name), 'cells', limit=1)
            b = self.repository.read(bundle, self.product(name), 'aggregates', limit=1)
            if a['metadata'] != b['metadata']: raise Unavailable('diagnostic pair mismatch')
            meta = a['metadata']
            with self.repository._connection() as db:
                db.execute('BEGIN')
                failure=[]
                def budget():
                    try: check_budget()
                    except BaseException as exc: failure.append(exc);return 1
                    return 0
                db.set_progress_handler(budget,1000)
                try:
                    yield View(db, name, meta['row_generation'], meta['max_samples'], meta['retention'], meta['ingestion']), meta
                except sqlite3.OperationalError:
                    if failure: raise failure[0]
                    raise
                finally: db.set_progress_handler(None,0)

    def _source_record(self, source, revision, offset):
        with self.catalog.connection() as db:
            row = self.catalog._source(db, source, revision)
            active = db.execute('SELECT revision FROM active WHERE source=?', (source,)).fetchone()
            if active is None or active[0] != revision: raise Conflict('diagnostic source revision changed')
            self.catalog._verify(row)
            if offset == row['cursor']: return None, dict(row)
            item = db.execute('SELECT * FROM entry WHERE source=? AND revision=? AND offset=?',
                              (source, revision, offset)).fetchone()
            if item is None:
                issue = db.execute('SELECT * FROM issue WHERE source=? AND revision=? AND offset=?',(source,revision,offset)).fetchone()
                if issue is None: raise Unavailable('unindexed source slot; explicit preparation required')
                if issue['end_offset'] > row['cursor']: raise Unavailable('unconfirmed issue slot')
                # No invented value. Preserve catalog's confirmed invalid-slot
                # provenance and advance only its exact already-indexed bytes.
                return ({'_catalog_issue':issue['reason'],'_record_hash':issue['hash']},issue['end_offset'],
                        {'raw_bytes':0,'decodes':0}),dict(row)
            ref = SourceRef(source, revision, row['path'], row['namespace'], item['offset'], item['end_offset'],
                            item['stamp'], item['module'], item['identity'], row['context'], item['hash'],
                            (row['dev'], row['ino'], row['size'], row['mtime']),
                            (item['day'], item['stamp'], source, revision, offset))
        read = self.reader.read_refs([ref])
        return (read['records'][0]['record'], item['end_offset'], read), dict(row)

    def consume(self, name, source, revision, *, target_offset=None, fault=lambda stage: None):
        """One confirmed record, then bounded pruning/publication steps; no discovery."""
        check_budget(); started = time.monotonic(); row = self.state(name)
        if row['pending']:
            return self.finish(name, fault=fault)
        cursor = self.repository.cursor(self.product(name), source, revision)
        offset = cursor.offset if cursor else 0
        if target_offset is not None and offset >= target_offset:
            return {'status': 'caught_up', 'raw_bytes': 0, 'decodes': 0}
        item, origin = self._source_record(source, revision, offset)
        if item is None: return {'status': 'caught_up', 'raw_bytes': 0, 'decodes': 0}
        raw, end, io = item
        if target_offset is not None and end > target_offset: raise ValueError('target is not a confirmed record boundary')
        dependency = None; skip_reason = None
        if raw.get('_catalog_issue'):
            sample = None; skip_reason = 'invalid_record'
        elif row['ingestion'] == 'history':
            identity_index = ([], [])
            if self.epochs is not None and not raw.get('module_serial'):
                from position_history import PositionSnapshot
                stamp = float(raw['timestamp']); end_stamp = math.nextafter(stamp, math.inf)
                captured = self.epochs.capture_window('position', stamp, end_stamp)
                snapshots = [PositionSnapshot.from_dict(value) for value in captured['records']]
                identity_index = ([value.effective_at for value in snapshots], snapshots)
                dependency = self.epochs.register_dependency(
                    'diagnostic:'+name+':'+uuid.uuid4().hex, 'position', stamp, end_stamp,
                    context={'source':source,'revision':revision,'offset':offset},
                    expected_fingerprint=captured['fingerprint'])
            try:
                sample = CurrentConditionBackfill._sample(raw, identity_index)
                if sample is None: skip_reason = 'identity_unknown'
            except (ValueError,TypeError,KeyError,OverflowError):
                sample = None; skip_reason = 'invalid_sample'
        else:
            if raw.get('schema_version') != 1: raise ValueError('unsupported cell schema')
            sample = CellSample(**{k: raw.get(k) for k in CellSample.__dataclass_fields__})
        row = self._new_work(name)
        with self._write() as db:
            current = db.execute('SELECT version,pending FROM diagnostic_composition WHERE name=?', (name,)).fetchone()
            if current['version'] != row['version'] or current['pending']: raise Conflict('diagnostic delta changed')
            view = View(db, name, row['private_gen'], row['max_samples'], row['retention'], row['ingestion'], writable=True)
            displaced = set()
            if sample is not None and row['ingestion'] == 'history':
                old = db.execute("SELECT * FROM diagnostic_row WHERE name=? AND kind='sample' AND module=? AND stamp=? AND retired IS NULL LIMIT 33",
                                 (name, sample.module, sample.timestamp)).fetchall()
                if len(old) > 32: raise ValueError('slot reconciliation budget')
                for prior in old:
                    value = decode(prior); displaced.add(value['module_serial']); ring = Ring(view, value['module_serial'])
                    meta = dict(ring._meta()); meta['count'] -= 1
                    if meta['count']: view.maps['identity_ring'][value['module_serial']] = meta
                    else: del view.maps['identity_ring'][value['module_serial']]
                    view.put('sample', prior['key'], None)
            if sample is not None:
                view.cells.add(sample)
                view.aggregates.add(sample, json.loads(row['options']))
            if skip_reason:
                counter = view.maps['quality'].get(skip_reason, {'count':0})
                view.maps['quality'][skip_reason] = {'count':counter['count']+1}
            view.flush()
            if sample is not None and row['ingestion'] == 'history':
                # Index the authoritative slot's immutable sample version, even
                # if it was immediately outside the current retained tail.
                sample_key,sample_born = view.last_sample_ref
                view.put('history_slot',_json([sample.module,sample.timestamp]),
                         {'sample_key':sample_key,'sample_born':sample_born,
                          'serial':sample.module_serial,'module':sample.module,
                          'timestamp':sample.timestamp,'source':source,'revision':revision,'offset':offset})
                for serial in displaced:
                    self._refill_history_identity(view,serial)
                view.flush()
            if row['ingestion'] == 'history':
                # replace_samples historically rebuilds latest positions from the
                # retained rings; preserve that distinction from live add().
                for position in range(1, 7):
                    latest = db.execute("SELECT * FROM diagnostic_row WHERE name=? AND kind='sample' AND module=? AND retired IS NULL ORDER BY stamp DESC,key DESC LIMIT 1",
                                        (name, position)).fetchone()
                    if latest is not None: view.maps['latest'][position] = decode(latest)
                    elif position in view.maps['latest']: del view.maps['latest'][position]
                view.flush()
            pending = {'source': source, 'revision': revision, 'offset': end,
                       'skipped':skip_reason,'issue_hash':raw.get('_record_hash'), 'receipt': origin['receipt'], 'frontier': origin['frontier'],
                       'epoch_dependency': dependency, 'namespace': origin['namespace'], 'raw_bytes': io['raw_bytes'], 'decodes': io['decodes']}
            db.execute('UPDATE diagnostic_composition SET pending=?,cutoff=?,version=version+1 WHERE name=?',
                       (_json(pending), view.cutoff, name))
            fault('after_private_delta')
        fault('after_delta_commit')
        return {'status': 'private_delta', 'raw_bytes': io['raw_bytes'], 'decodes': io['decodes'],
                'changed_rows': view.writes, 'transaction': self.last_transaction, 'step_seconds': time.monotonic() - started}

    @staticmethod
    def _refill_history_identity(view, serial):
        ring=Ring(view,serial)
        if len(ring)>=view.maximum:return
        # One incoming slot can displace at most one retained slot per identity.
        # Refill exactly that loss from the next canonical slot, never replay Raw.
        first=ring._meta().get('first')
        upper=first[0] if first else float('inf')
        candidate=view.db.execute(
            "SELECT * FROM diagnostic_row h INDEXED BY diagnostic_active_ring WHERE h.name=? AND h.kind='history_slot' AND h.group_key=? AND h.ordering<=? AND h.retired IS NULL "
            "AND NOT EXISTS(SELECT 1 FROM diagnostic_row s WHERE s.name=h.name AND s.kind='sample' AND s.key=json_extract(h.payload,'$.sample_key') AND s.born=json_extract(h.payload,'$.sample_born') AND s.retired IS NULL) "
            "ORDER BY h.ordering DESC,h.key DESC LIMIT 1",(view.name,serial,upper)).fetchone()
        if candidate is None:return
        pointer=decode(candidate)
        source=view.db.execute("SELECT * FROM diagnostic_row WHERE name=? AND kind='sample' AND key=? AND born=?",
                               (view.name,pointer['sample_key'],pointer['sample_born'])).fetchone()
        if source is None:raise Unavailable('canonical slot sample version missing')
        ring.append(decode(source));view.flush()
        key,born=view.last_sample_ref
        view.put('history_slot',candidate['key'],dict(pointer,sample_key=key,sample_born=born))

    def begin_live_handover(self,name):
        """Explicit offline migration: preserve the historical result, then use
        original live arrival-order semantics for subsequent samples. Only the
        retained rings are re-keyed, in private bounded pages; no Raw replay.
        """
        row=self.state(name)
        pending=json.loads(row['pending']) if row['pending'] else None
        if pending and (pending.get('handover') or pending.get('handover_ready')):return {'status':'preparing','raw_bytes':0,'decodes':0}
        if row['ingestion']=='live':return {'status':'live','raw_bytes':0,'decodes':0}
        if row['work'] or pending or row['cutoff']:raise Conflict('historical diagnostic publication must finish before handover')
        meta=self.repository.read(row['bundle'],self.product(name),'cells',limit=1)['metadata']
        with self._write() as db:
            if db.execute('SELECT version FROM diagnostic_composition WHERE name=?',(name,)).fetchone()[0]!=row['version']:
                raise Conflict('diagnostic changed before handover')
            gen=db.execute('INSERT INTO generation DEFAULT VALUES').lastrowid;work=uuid.uuid4().hex
            db.execute('INSERT INTO draft VALUES(?,?,?)',(work,self.head_name(name),row['bundle']))
            pending={'handover':True,'origin_bundle':row['bundle'],'origin_generation':meta['row_generation'],
                'phase':'rings','after_group':['',''],'group':None}
            db.execute('UPDATE diagnostic_composition SET private_gen=?,work=?,pending=?,version=version+1 WHERE name=?',
                (gen,work,_json(pending),name))
        return {'status':'preparing','raw_bytes':0,'decodes':0}

    def _handover_step(self,name,row,pending,*,fault):
        with self._write() as db:
            if db.execute('SELECT version FROM diagnostic_composition WHERE name=?',(name,)).fetchone()[0]!=row['version']:
                raise Conflict('diagnostic handover advanced concurrently')
            view=View(db,name,row['private_gen'],row['max_samples'],row['retention'],'history',writable=True)
            oldgen=pending['origin_generation']
            if pending['phase']=='rings':
                group=pending['group']
                if group is None:
                    item=db.execute("SELECT * FROM diagnostic_row WHERE name=? AND kind IN ('identity_ring','unknown_ring') "
                        "AND (kind,key)>(?,?) AND born<=? AND (retired IS NULL OR retired>?) ORDER BY kind,key LIMIT 1",
                        (name,*pending['after_group'],oldgen,oldgen)).fetchone()
                    if item is None:pending['phase']='slots'
                    else:
                        meta=decode(item);serial=int(item['key']) if item['kind']=='unknown_ring' else item['key']
                        ring=Ring(view,serial,item['kind']=='unknown_ring')
                        pending['group']={'kind':item['kind'],'key':item['key'],'group_key':ring.group,
                            'meta':meta,'base':meta['next'],'copied':0,'after':None,'first':None}
                else:
                    query="SELECT * FROM diagnostic_row INDEXED BY diagnostic_ring WHERE name=? AND kind='sample' AND group_key=? AND born<=? AND (retired IS NULL OR retired>?)"
                    args=[name,group['group_key'],oldgen,oldgen]
                    if group['after'] is not None:
                        query+=' AND (ordering,key)>(?,?)';args.extend(group['after'])
                    samples=db.execute(query+' ORDER BY ordering,key LIMIT ?',(*args,PAGE)).fetchall()
                    for sample in samples:
                        order=group['base']+group['copied']
                        view.put('sample',sample['key'],decode(sample),group=group['group_key'],ordering=order)
                        if group['first'] is None:group['first']=[order,sample['key']]
                        group['copied']+=1;group['after']=[sample['ordering'],sample['key']]
                    if len(samples)<PAGE:
                        if group['copied']!=group['meta']['count']:raise Conflict('historical ring coverage changed during handover')
                        meta=dict(group['meta'],first=group['first'],next=group['base']+group['copied'])
                        view.maps[group['kind']][group['key']]=meta;view.flush()
                        pending['after_group']=[group['kind'],group['key']];pending['group']=None
            elif pending['phase']=='slots':
                slots=db.execute("SELECT key FROM diagnostic_row WHERE name=? AND kind='history_slot' AND retired IS NULL ORDER BY key LIMIT ?",(name,PAGE)).fetchall()
                for slot in slots:view.put('history_slot',slot['key'],None)
                if not slots:
                    pending={'handover_ready':True,'origin_bundle':pending['origin_bundle']}
                    db.execute("UPDATE diagnostic_composition SET ingestion='live' WHERE name=?",(name,))
            else:raise Conflict('unknown diagnostic handover phase')
            db.execute('UPDATE diagnostic_composition SET pending=?,version=version+1 WHERE name=?',(_json(pending),name))
            fault('before_handover_page_commit')
        fault('after_handover_page_commit')
        return {'status':'handover','changed_rows':view.writes,'raw_bytes':0,'decodes':0}

    def finish(self, name, *, fault=lambda stage: None):
        check_budget(); row = self.state(name)
        pending = json.loads(row['pending']) if row['pending'] else None
        if pending and pending.get('handover'):
            return self._handover_step(name,row,pending,fault=fault)
        if pending and pending.get('configuration'):
            return self._resize_step(name,row,pending,fault=fault)
        if row['cutoff']:
            started = time.monotonic()
            with self._write() as db:
                if db.execute('SELECT version FROM diagnostic_composition WHERE name=?', (name,)).fetchone()[0] != row['version']:
                    raise Conflict('diagnostic retention state changed')
                keys = db.execute("SELECT key FROM diagnostic_row WHERE name=? AND kind='aggregate' AND retired IS NULL AND day<? ORDER BY day,key LIMIT ?",
                                  (name, row['cutoff'], PAGE)).fetchall()
                view = View(db, name, row['private_gen'], row['max_samples'], row['retention'], row['ingestion'], writable=True)
                for key in keys: view.put('aggregate', key[0], None)
                db.execute('UPDATE diagnostic_composition SET cutoff=?,version=version+1 WHERE name=?',
                           (row['cutoff'] if len(keys) == PAGE else None, name))
                fault('after_prune_page')
            return {'status': 'pruning', 'changed_rows': len(keys), 'raw_bytes': 0, 'decodes': 0,
                    'transaction': self.last_transaction, 'step_seconds': time.monotonic() - started}
        return self.publish(name, fault=fault)

    def _resize_step(self, name, row, pending, *, fault):
        """Changing capacity preserves available evidence; it invents no history.

        As in loading an existing cache with a new maxlen, smaller rings retain
        their newest members; enlarging capacity cannot recover evicted samples.
        No wall-clock-based aggregate pruning: the next real add supplies its day.
        """
        with self._write() as db:
            current=db.execute('SELECT version FROM diagnostic_composition WHERE name=?',(name,)).fetchone()
            if current[0]!=row['version']:raise Conflict('resize advanced concurrently')
            record=db.execute("SELECT * FROM diagnostic_row WHERE name=? AND kind IN ('identity_ring','unknown_ring') AND (kind,key)>(?,?) AND retired IS NULL ORDER BY kind,key LIMIT 1",
                              (name,*pending['after'])).fetchone()
            if record is None:
                db.execute('UPDATE diagnostic_composition SET pending=?,version=version+1 WHERE name=?',(_json({'configuration_ready':True}),name))
                return {'status':'resize_prepared','changed_rows':0,'raw_bytes':0,'decodes':0}
            view=View(db,name,row['private_gen'],row['max_samples'],row['retention'],row['ingestion'],writable=True)
            meta=decode(record)
            serial=int(record['key']) if record['kind']=='unknown_ring' else record['key']
            ring=Ring(view,serial,record['kind']=='unknown_ring')
            excess=max(0,meta['count']-row['max_samples'])
            remove=db.execute("SELECT key FROM diagnostic_row INDEXED BY diagnostic_active_ring WHERE name=? AND kind='sample' AND group_key=? AND retired IS NULL ORDER BY ordering,key LIMIT ?",
                              (name,ring.group,min(PAGE,excess))).fetchall()
            for item in remove:view.put('sample',item[0],None)
            meta['count']-=len(remove)
            first=db.execute("SELECT ordering,key FROM diagnostic_row INDEXED BY diagnostic_active_ring WHERE name=? AND kind='sample' AND group_key=? AND retired IS NULL ORDER BY ordering,key LIMIT 1",(name,ring.group)).fetchone()
            meta['first']=list(first) if first else None
            view.maps[record['kind']][serial]=meta;view.flush()
            if meta['count']<=row['max_samples']:pending['after']=[record['kind'],record['key']]
            db.execute('UPDATE diagnostic_composition SET pending=?,version=version+1 WHERE name=?',(_json(pending),name))
            fault('after_resize_page')
        return {'status':'resizing','changed_rows':view.writes,'raw_bytes':0,'decodes':0,'transaction':self.last_transaction}

    def publish(self, name, *, fault=lambda stage: None):
        row = self.state(name)
        if row['cutoff']: raise Conflict('retention not complete')
        if row['pending'] and json.loads(row['pending']).get('handover'):raise Conflict('diagnostic handover not complete')
        if not row['work']: return {'status': 'published', 'bundle': row['bundle'], 'raw_bytes': 0, 'decodes': 0}
        metadata = {'row_generation': row['private_gen'], 'max_samples': row['max_samples'],
                    'coverage': 'confirmed_selected_source_prefixes_only', 'retention': row['retention'], 'ingestion': row['ingestion'], 'options': json.loads(row['options'])}
        metadata['delta_provenance'] = json.loads(row['pending']) if row['pending'] else None
        for part in ('cells', 'aggregates'):
            self.repository.stage(row['work'], self.product(name), part, {}, metadata=metadata)
        pending = json.loads(row['pending']) if row['pending'] else None
        if pending and pending.get('handover'):raise Conflict('diagnostic handover not complete')
        if pending and (pending.get('configuration_ready') or pending.get('handover_ready')): pending = None
        if pending and pending.get('configuration'): raise Conflict('retention resize not complete')
        if pending:
            with self.repository._connection() as db:
                old = db.execute('SELECT durable_end FROM source_revision WHERE source=? AND revision=?',
                                 (pending['source'], pending['revision'])).fetchone()
            if old is None:
                self.repository.register_source(pending['source'], pending['revision'], metadata={'namespace': pending['namespace']}, durable_end=pending['frontier'])
            elif old[0] < pending['frontier']:
                self.repository.confirm_source(pending['source'], pending['revision'], expected_end=old[0], durable_end=pending['frontier'])
            expected = self.repository.cursor(self.product(name), pending['source'], pending['revision'])
            self.repository.stage_cursor(row['work'], self.product(name), pending['source'], pending['revision'], expected=expected, next_offset=pending['offset'])
        dependency = pending.get('epoch_dependency') if pending else None
        if dependency and self.epochs is None: raise Unavailable('restore requires original epoch store')
        guard = self.epochs.publication_guard([dependency]) if dependency else nullcontext()
        with guard, self.catalog.write() as source_db:
            if pending:
                active = source_db.execute('SELECT revision FROM active WHERE source=?', (pending['source'],)).fetchone()
                if active is None or active[0] != pending['revision']: raise Conflict('source changed before diagnostic publication')
                self.catalog._verify(self.catalog._source(source_db,pending['source'],pending['revision']))
                receipt = source_db.execute('SELECT frontier FROM receipt WHERE source=? AND revision=? AND receipt=?',
                                            (pending['source'], pending['revision'], pending['receipt'])).fetchone()
                if receipt is None or receipt[0] < pending['offset']: raise Conflict('unconfirmed diagnostic prefix')
            with self._write() as db:
                if db.execute('SELECT version FROM diagnostic_composition WHERE name=?', (name,)).fetchone()[0] != row['version']:
                    raise Conflict('diagnostic publication changed')
                bundle = self.repository._publish(db, row['work'], requires=(), fault=fault)
                db.execute('UPDATE diagnostic_composition SET bundle=?,private_gen=NULL,work=NULL,pending=NULL,version=version+1 WHERE name=?', (bundle, name))
                fault('after_pair_cursor')
        fault('after_pair_commit')
        return {'status': 'published', 'bundle': bundle, 'raw_bytes': 0, 'decodes': 0, 'transaction': self.last_transaction}


    def configure(self, name, options):
        """Explicit current options only; historical aggregate config IDs remain."""
        row = self.state(name)
        if row['work']: raise Conflict('finish current delta before options change')
        maximum = int(options['cell_diag_history_max_samples'])
        if maximum < 1: raise ValueError('positive retention required')
        retention = max(30,int(options.get('cell_diag_aggregate_retention_days',730)))
        resize = maximum != row['max_samples']
        row = self._new_work(name)
        with self._write() as db:
            if db.execute('SELECT version FROM diagnostic_composition WHERE name=?', (name,)).fetchone()[0] != row['version']:
                raise Conflict('configuration changed concurrently')
            db.execute('UPDATE diagnostic_composition SET options=?,max_samples=?,retention=?,pending=?,version=version+1 WHERE name=?',
                       (_json(options),maximum,retention,_json({'configuration':True,'after':['','']}) if resize else None,name))
        return {'status':'resizing'} if resize else self.publish(name)

    def set_current_identities(self, name, positions):
        """Explicit already-observed identities, using the existing store method."""
        row = self.state(name)
        if row['work']: raise Conflict('finish current delta before identity handover')
        row = self._new_work(name)
        with self._write() as db:
            if db.execute('SELECT version FROM diagnostic_composition WHERE name=?',(name,)).fetchone()[0] != row['version']:
                raise Conflict('identity handover changed concurrently')
            view = View(db,name,row['private_gen'],row['max_samples'],row['retention'],row['ingestion'],writable=True)
            view.cells.set_current_identities(positions)
            view.maps['positions']['current'] = view.cells._documented_current_identity
            view.flush()
            db.execute('UPDATE diagnostic_composition SET version=version+1 WHERE name=?',(name,))
        return self.publish(name)

    def begin_analysis(self, name, modules, *, maintenance_events=(), bundle=None):
        if not 1 <= len(modules) <= 6 or len(set(modules)) != len(modules) or any(m not in range(1, 7) for m in modules):
            raise ValueError('explicit distinct module positions 1..6 required')
        state = self.state(name); bundle = state['bundle'] if bundle is None else bundle
        with self.view(name, bundle=bundle) as (_, meta): options = meta['options']
        identifier = uuid.uuid4().hex
        request_key = hashlib.sha256(_json([bundle,options,list(modules),list(maintenance_events)]).encode()).hexdigest()
        with self._write() as db:
            existing = db.execute('SELECT id FROM diagnostic_analysis_job WHERE name=? AND request_key=?',(name,request_key)).fetchone()
            if existing: return existing[0]
            # A durable pin protects all kernel metadata until explicit job disposal.
            db.execute('INSERT INTO pin VALUES(?,?,?)', ('diagnostic-analysis:'+identifier, bundle, identifier))
            db.execute("INSERT INTO diagnostic_analysis_job(id,name,bundle,options,modules,maintenance,request_key,status) VALUES(?,?,?,?,?,?,?,'statistics')",
                       (identifier, name, bundle, _json(options), _json(list(modules)), _json(list(maintenance_events)),request_key))
        return identifier

    def analysis_state(self, job):
        with self.repository._connection() as db:
            row = db.execute('SELECT * FROM diagnostic_analysis_job WHERE id=?', (job,)).fetchone()
            if row is None: raise Unavailable('analysis job absent')
            return dict(row)

    def analysis_step(self, job, *, fault=lambda stage: None):
        """32 retained samples per statistics step, then exact bounded reduction.

        The final existing evidence methods require the selected retention window;
        they are not replaced by an approximate incremental scientific algorithm.
        No Raw read, cache copy or retention-ring reconstruction occurs.
        """
        check_budget(); started = time.monotonic(); state = self.analysis_state(job)
        if state['status'] == 'complete': return {'status': 'complete', 'samples_processed': 0}
        modules = json.loads(state['modules']); module = modules[state['position']]
        options = json.loads(state['options'])
        options_hash = hashlib.sha256(_json(options).encode()).hexdigest()
        with work_budget(check_budget), self.view(state['name'], bundle=state['bundle']) as (view, _):
            if state['status'] == 'statistics':
                selected, next_key, complete = view.sample_page(module,
                    json.loads(state['after_key']) if state['after_key'] else None, missing_options=options_hash)
                contributions = []
                for sample in selected:
                    check_budget()
                    valid = view.cells.valid_cell_voltages(sample)
                    stats = view.cells.empty_current_statistics()
                    if valid: view.cells.accumulate_current_statistics(stats, sample, options)
                    payload=_json({'valid':valid,'statistics':stats})
                    contributions.append((hashlib.sha256(_json(sample).encode()).hexdigest(),
                                          options_hash,payload,hashlib.sha256(payload.encode()).hexdigest()))
                after = state['after_sample'] + len(selected)
                with self._write() as db:
                    current = db.execute('SELECT status,after_sample,position FROM diagnostic_analysis_job WHERE id=?', (job,)).fetchone()
                    if tuple(current) != (state['status'], state['after_sample'], state['position']): raise Conflict('analysis advanced concurrently')
                    db.executemany('INSERT OR IGNORE INTO diagnostic_condition_cache VALUES(?,?,?,?)',contributions)
                    db.execute('UPDATE diagnostic_analysis_job SET after_sample=?,after_key=?,status=? WHERE id=?',
                               (after, _json(next_key), 'reduce' if complete else 'statistics', job))
                    fault('after_statistics_page')
                return {'status': 'statistics', 'samples_processed': len(selected),
                        'retention_samples_processed': after, 'raw_bytes': 0, 'step_seconds': time.monotonic()-started}
            def cached_statistics(values, parameters):
                stats = view.cells.empty_current_statistics()
                for sample in values:
                    check_budget()
                    digest = hashlib.sha256(_json(sample).encode()).hexdigest()
                    cached = view.db.execute('SELECT payload,digest FROM diagnostic_condition_cache WHERE sample_hash=? AND options_hash=?',
                                             (digest,options_hash)).fetchone()
                    if cached is None: raise Unavailable('incomplete CurrentCondition statistic checkpoint')
                    part = decode(cached)['statistics']
                    for phase,cells in part.items():
                        for index,contribution in enumerate(cells):
                            cell=stats[phase][index]
                            for field in ('n','low','high'):cell[field]+=contribution[field]
                            for field in ('dev','ranks'):cell[field].extend(contribution[field])
                return stats
            view.cells.current_condition_statistics = cached_statistics
            serial = view.cells.current_serial(module)
            aggregates = view.aggregates.for_identity(module, serial)
            result = view.cells.analyse(module, options, json.loads(state['maintenance']), aggregates)
            count = result.get('sample_count', 0)
            result = dict(result, physical_module_serial=serial)
        parts = self._result_parts(result)
        with self._write() as db:
            current = db.execute('SELECT status,position FROM diagnostic_analysis_job WHERE id=?', (job,)).fetchone()
            if tuple(current) != ('reduce', state['position']): raise Conflict('analysis advanced concurrently')
            db.executemany('INSERT INTO diagnostic_analysis_result VALUES(?,?,?,?)',
                           [(job,module,key,_json(value)) for key,value in parts.items()])
            position = state['position']+1
            db.execute('UPDATE diagnostic_analysis_job SET position=?,after_sample=0,after_key=NULL,status=? WHERE id=?',
                       (position,'complete' if position == len(modules) else 'statistics',job))
            fault('after_analysis_module')
        return {'status': 'complete' if position == len(modules) else 'statistics',
                'samples_processed': 0, 'exact_reduction_samples':count,
                'raw_bytes':0,'step_seconds':time.monotonic()-started}

    @staticmethod
    def _result_parts(result):
        result = dict(result); parts={}
        for index, cell in enumerate(result.pop('cells', [])): parts[f'cell:{index:02d}']=cell
        advanced = dict(result.pop('advanced_diagnostics', {}))
        for index, cell in enumerate(advanced.pop('cells', [])): parts[f'advanced:{index:02d}']=cell
        parts['header']=result;parts['advanced_header']=advanced
        return parts

    def analysis_result(self, job):
        state=self.analysis_state(job)
        if state['status']!='complete': raise Unavailable('analysis not complete')
        with self.repository.lease(state['bundle'],owner='diagnostic-analysis-reader'):
            output={}
            with self.repository._connection() as db:
                for module in json.loads(state['modules']):
                    parts={r['part']:json.loads(r['payload']) for r in db.execute('SELECT part,payload FROM diagnostic_analysis_result WHERE job=? AND module=? ORDER BY part',(job,module))}
                    result=parts['header'];result['cells']=[v for k,v in parts.items() if k.startswith('cell:')]
                    if parts['advanced_header']:
                        result['advanced_diagnostics']={**parts['advanced_header'],'cells':[v for k,v in parts.items() if k.startswith('advanced:')]}
                    output[module]=result
            return output

    def analysis_reference(self, job):
        from config_history import config_id, diagnostic_parameters
        state = self.analysis_state(job)
        with self.view(state['name'],bundle=state['bundle']) as (view,meta):
            identities = {m:view.cells.current_serial(m) for m in json.loads(state['modules'])}
        return {'job':job,'current_identities':identities,
                'config_id':config_id(diagnostic_parameters(json.loads(state['options']))),
                'persistent_pair':state['bundle']}

    def analyse_reference(self, reference):
        job=reference['job']
        # Current worker executes bounded resumable steps against immutable pair.
        # No lock spans diagnostic calculations; the caller controls scheduling.
        while self.analysis_state(job)['status']!='complete':
            check_budget();self.analysis_step(job)
        return self.analysis_result(job), [], {'persistent_pair':self.analysis_state(job)['bundle']}


class RepositoryPairWriter:
    def __init__(self, diagnostics, *, max_steps=64):
        if not 1 <= max_steps <= 256: raise ValueError('persistence step budget must be 1..256')
        self.diagnostics,self.max_steps=diagnostics,max_steps
        self.last_steps=[]

    def persist_payload(self, reference):
        self.last_steps=[]
        from work_scheduler import active, account
        scheduled = active()
        while True:
            check_budget()
            step=self.diagnostics.consume(reference['name'],reference['source'],reference['revision'],target_offset=reference['target_offset'])
            self.last_steps.append(step)
            account(raw_read_bytes=step.get('raw_bytes', 0), decodes=step.get('decodes', 0),
                    changed_rows=step.get('changed_rows', 0))
            if step['status']=='caught_up':return True
            if len(self.last_steps) >= self.max_steps:
                if not scheduled:
                    raise Unavailable('diagnostic step budget; confirmed checkpoints retained for resume')
                # Same finite captured source prefix, same job/stack. The shared
                # scheduler yields instead of restarting; retain bounded metrics.
                self.last_steps.clear()


def local_diagnostic_workers(diagnostics, *, max_persistence_steps=64):
    """Explicit local factory. Constructors do not start threads or select paths."""
    from derived_persistence import DerivedPersistenceWorker
    from cell_analysis_worker import CellAnalysisWorker
    writer=RepositoryPairWriter(diagnostics,max_steps=max_persistence_steps)
    return (DerivedPersistenceWorker(writer,writer),
            CellAnalysisWorker(diagnostics.analyse_reference),writer)
