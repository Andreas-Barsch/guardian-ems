"""Bounded append-to-metadata publication and request-scoped head selection."""
from contextlib import contextmanager
from contextvars import ContextVar
import uuid
from metadata_log_consumer import MetadataLogConsumer
from metadata_revision import MetadataRevision, RevisionMetadataReader
from projection_repository import Conflict, Unavailable, _json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS application_metadata(
 name TEXT PRIMARY KEY, contract TEXT NOT NULL, revision TEXT REFERENCES metadata_revision(id),
 parent TEXT REFERENCES metadata_revision(id), frontier INTEGER);
CREATE INDEX IF NOT EXISTS application_metadata_revision ON application_metadata(revision);
CREATE INDEX IF NOT EXISTS application_metadata_parent ON application_metadata(parent);
'''


class MetadataTail:
    def __init__(self, repository, registry, *, head, mapping, origin_proof):
        self.repository, self.registry = repository, registry
        self.head, self.mapping, self.origin_proof = head, dict(mapping), origin_proof

    def initialize(self):
        contract = _json({'registry': self.registry.config['registry_id'],
                          'mapping': self.mapping, 'origin': self.origin_proof})
        with self.repository._connection() as db:
            db.executescript('BEGIN IMMEDIATE;' + SCHEMA + 'COMMIT;')
        with self.repository._write() as db:
            old = db.execute('SELECT contract FROM application_metadata WHERE name=?', (self.head,)).fetchone()
            if old and old[0] != contract:
                raise Conflict('metadata application binding changed')
            db.execute('INSERT OR IGNORE INTO application_metadata(name,contract) VALUES(?,?)', (self.head, contract))

    def current(self):
        with self.repository._connection() as db:
            from application_schema import require_schema
            require_schema(self, db, SCHEMA, ('application_metadata',))
            row = db.execute('SELECT revision FROM metadata_head WHERE name=?', (self.head,)).fetchone()
            if row is None:
                raise Unavailable('explicit initial metadata migration required')
            from metadata_revision import ROOTS
            roots = {r[0] for r in db.execute('SELECT name FROM metadata_root WHERE revision=?', (row[0],))}
            if roots != set(ROOTS):
                raise Unavailable('metadata service roots require explicit migration')
            binding = db.execute('SELECT mapping FROM metadata_registry_cursor WHERE revision=? AND registry=?',
                                 (row[0], self.registry.config['registry_id'])).fetchone()
            if binding is None or binding[0] != _json({'mapping':self.mapping, 'origin':self.origin_proof}):
                raise Unavailable('published metadata registry binding requires explicit migration')
        return row[0]

    def _consumer(self, revision):
        return MetadataLogConsumer(self.registry, MetadataRevision(self.repository, revision))

    def step(self, *, fault=lambda stage: None):
        with self.repository._connection() as db:
            from application_schema import require_schema
            require_schema(self, db, SCHEMA, ('application_metadata',))
            row = db.execute('SELECT * FROM application_metadata WHERE name=?', (self.head,)).fetchone()
        if row is None:
            raise Unavailable('metadata application binding not prepared')
        state = dict(row)
        if state['revision'] is None:
            parent = self.current()
            cursor = self._consumer(parent).cursor()
            page = self.registry.read_page(next_seq=cursor[0], previous_hash=cursor[2], max_records=1)
            if not page['records']:
                return {'status': 'caught_up', 'raw_bytes': 0, 'decodes': 0}
            identifier = uuid.uuid4().hex
            MetadataRevision(self.repository, identifier).begin(parent=parent,
                provenance={'application_head': self.head, 'registry': self.registry.config['registry_id'],
                            'frontier': page['head_seq']})
            fault('after_metadata_candidate_begin')
            with self.repository._write() as db:
                if db.execute('UPDATE application_metadata SET revision=?,parent=?,frontier=? WHERE name=? AND revision IS NULL',
                              (identifier, parent, page['head_seq'], self.head)).rowcount != 1:
                    raise Conflict('metadata application dispatch changed')
            fault('after_metadata_dispatch')
            return {'status': 'dispatched', 'raw_bytes': 0, 'decodes': 0}
        revision = MetadataRevision(self.repository, state['revision'])
        revision.begin(parent=state['parent'], provenance={'application_head': self.head,
                       'registry': self.registry.config['registry_id'], 'frontier': state['frontier']})
        consumer = self._consumer(state['revision'])
        consumer.initialize(self.mapping, origin_proof=self.origin_proof)
        if revision.state()['status'] == 'ready':
            if self.current() != state['revision']:
                raise Conflict('metadata head changed before application ACK')
            with self.repository._write() as db:
                if db.execute('UPDATE application_metadata SET revision=NULL,parent=NULL,frontier=NULL WHERE name=? AND revision=?',
                              (self.head, state['revision'])).rowcount != 1:
                    raise Conflict('metadata application ACK changed')
            return {'status': 'published', 'raw_bytes': 0, 'decodes': 0}
        cursor = consumer.cursor()
        if cursor[0] <= state['frontier']:
            result = consumer.step(expected_cursor=cursor, max_records=8, fault=fault)
            after = consumer.cursor()
            if result['caught_up'] and (after[0] != state['frontier'] + 1 or after[1]):
                raise Conflict('frozen metadata receipt frontier unavailable')
            return result
        if cursor[0] != state['frontier'] + 1 or cursor[1]:
            raise Conflict('metadata exceeded frozen receipt frontier')
        result = revision.position_step(expected_version=revision.state()['version'], limit=8, fault=fault)
        if result['complete']:
            revision.publish(expected_version=revision.state()['version'], head=self.head,
                             expected_head=state['parent'], scope_proof={'registry': consumer.identity,
                             'frontier': state['frontier'], 'application_head': self.head}, fault=fault)
        return result


class HeadMetadataReader:
    """One immutable metadata revision for the whole API call, including overlays."""
    def __init__(self, repository, head):
        self.repository, self.head = repository, head
        self._reader = ContextVar('application_metadata_reader', default=None)

    @contextmanager
    def capture(self):
        previous = self._reader.get()
        if previous is not None:
            yield previous
            return
        # Hold a reader lease before releasing the head transaction. This also
        # protects the captured revision between multiple bounded reader calls.
        token = uuid.uuid4().hex
        with self.repository._write() as db:
            row = db.execute('SELECT revision FROM metadata_head WHERE name=?', (self.head,)).fetchone()
            if row is None:
                from research_budget import ResearchQueryError
                raise ResearchQueryError('history_preparation_required', 'metadata head not prepared', 503)
            revision = row[0]
            db.execute('INSERT INTO metadata_reader VALUES(?,?)', (token, revision))
        from lifecycle_leases import read_scope, release_reader
        marker = self._reader.set(RevisionMetadataReader(self.repository, revision))
        try:
            with read_scope(self.repository, token, 'metadata_reader'):
                yield self._reader.get()
        finally:
            self._reader.reset(marker)
            release_reader(self.repository, token)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        def call(*args, **kwargs):
            with self.capture() as reader:
                return getattr(reader, name)(*args, **kwargs)
        return call


class ConfirmedIdentity:
    """Acquisition never assigns an obsolete identity across an unconsumed edit."""
    def __init__(self, tail, reader):
        self.tail, self.reader = tail, reader

    def identity_at(self, position, timestamp):
        with self.reader.capture() as selected:
            from raw_registry import encoded, hashed
            # Only a documentary position change can invalidate this identity.
            # Pending technical/config records in the same registry must not
            # erase a still-confirmed position on every PWR cycle.
            with self.tail.repository._connection() as db:
                sources = {row['source']:row['receipt'] for row in db.execute(
                    'SELECT source,receipt FROM metadata_registry_source WHERE revision=? AND registry=?',
                    (selected.revision,self.tail.registry.config['registry_id']))}
            for source, kind in self.tail.mapping.items():
                if kind != 'position':
                    continue
                receipt = self.tail.registry.state(source)
                if (None if receipt is None else hashed(encoded(receipt))) != sources.get(source):
                    raise Unavailable('confirmed position metadata tail pending; live identity unresolved')
            topology = selected.topology_at(timestamp)
            item = next((value for value in topology['positions'] if value['position'] == position), None)
            return (item['physical_serial'] if item else None, topology['position_history_id'])


class MetadataPending(Unavailable):
    """The selected source has confirmed bytes newer than its metadata view."""


class ConfirmedMetadataSource:
    """Service reads must match the confirmed source before a subsequent write."""
    KIND = None
    def __init__(self, tail, reader, writer, path):
        from pathlib import Path
        self.tail, self.reader, self.writer, self.path = tail, reader, writer, Path(path)
        self.source = writer.source(path)
        if tail.registry.config['registry_id'] != writer.registry.config['registry_id']:
            raise ValueError('position writer and metadata registry differ')
        if self.KIND is None or tail.mapping.get(self.source) != self.KIND:
            raise ValueError('explicit '+str(self.KIND)+' source mapping required')

    @contextmanager
    def current(self):
        from raw_registry import encoded, hashed
        with self.reader.capture() as selected:
            receipt = self.writer.registry.state(self.source)
            with self.tail.repository._connection() as db:
                row = db.execute('SELECT receipt FROM metadata_registry_source WHERE revision=? AND registry=? AND source=?',
                    (selected.revision,self.tail.registry.config['registry_id'],self.source)).fetchone()
            if receipt is None:
                if self.path.exists() and self.path.stat().st_size:
                    raise Unavailable(str(self.KIND)+' source requires explicit adoption')
            elif row is None or row[0] != hashed(encoded(receipt)):
                raise MetadataPending('confirmed '+str(self.KIND)+' metadata pending; no stale service write')
            yield selected


class ConfirmedPositionReader(ConfirmedMetadataSource):
    KIND = 'position'

    def position_snapshot_at(self, timestamp=None):
        with self.current() as reader:
            return reader.position_snapshot_at(timestamp)

    def position_validation_records(self, timestamp):
        with self.current() as reader:
            return reader.position_validation_records(timestamp)

    def position_snapshots(self):
        with self.current() as reader:
            return reader.position_snapshots()

    def position_known_serials(self,position):
        with self.current() as reader:
            return reader.position_known_serials(position)

    def position_last_documented(self):
        with self.current() as reader:
            return reader.position_last_documented()

    def position_serial_options(self,position,timestamp):
        with self.current() as reader:
            return reader.position_serial_options(position,timestamp)


class ConfirmedMaintenanceReader(ConfirmedMetadataSource):
    KIND = 'maintenance'

    def maintenance_get(self, event_id):
        try:
            with self.current() as reader:
                return reader.maintenance_get(event_id)
        except MetadataPending:
            # The existing topology transaction creates a maintenance event and
            # immediately references it from a position snapshot. Read that exact
            # confirmed tip without recovery/replay or waiting for a consumer.
            from raw_registry import encoded, hashed, file_signature
            from maintenance import MaintenanceEvent
            registry = self.writer.registry
            receipt = registry.state(self.source)
            if receipt is None or receipt['mode'] not in ('append','adopt') or receipt['path'] != str(self.path.resolve()):
                raise
            signature = file_signature(self.path)
            if signature != receipt['signature']:
                raise MetadataPending('maintenance tip differs from confirmed source')
            raw = registry._raw_range(receipt['path'],receipt['start'],receipt['end'])
            if hashed(raw) != receipt['block_hash']:
                raise MetadataPending('maintenance tip hash changed')
            import json
            lines = [line for line in raw.splitlines() if line.strip()]
            event = MaintenanceEvent.from_dict(json.loads(lines[-1])) if lines else None
            if (file_signature(self.path) != signature or registry.state(self.source) != receipt
                    or event is None or event.maintenance_event_id != event_id):
                raise MetadataPending('requested maintenance event is not the confirmed tip; metadata pending')
            return event

    def maintenance_history(self, event_id):
        with self.current() as reader:
            return reader.maintenance_history(event_id)

    def maintenance_request(self, request_id):
        with self.current() as reader:
            return reader.maintenance_request(request_id)

    def maintenance_list(self, **selection):
        with self.current() as reader:
            return reader.maintenance_list(**selection)


class PreparedServiceApi:
    """Use the existing service API inside the application's HTTP read budget."""
    def __init__(self, api, scheduler):
        self.api, self.scheduler = api, scheduler
        self.service = api.service

    def handle(self, method, target, headers=None, body=b'', *, cancelled=None, deadline=None):
        from research_budget import cancellation_scope, query_budget
        with cancellation_scope(cancelled), query_budget(deadline):
            return self.api.handle(method, target, headers, body)
