"""Explicit, restartable adoption of frozen legacy writer files.

Called only by a separate migration operation, never application startup or a
GET. The finite file list comes from an already reviewed inventory. Bytes and
filenames stay unchanged; each step confirms at most one existing Raw block.
"""
import json
from pathlib import Path
from projection_repository import Conflict, Unavailable, _json
from raw_registry import RawRegistry, encoded, hashed, file_signature

SCHEMA = '''
CREATE TABLE IF NOT EXISTS application_adoption(
 id TEXT PRIMARY KEY, contract TEXT NOT NULL, ordinal INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'preparing');
'''


class _AdoptionRegistry(RawRegistry):
    def _recover(self, **kwargs):
        intent = self._intent()
        if intent is not None and intent['mode'] not in ('register', 'adopt'):
            raise Conflict('pending Raw append requires writer recovery outside legacy adoption')
        return super()._recover(**kwargs)


class LegacyWriterAdoption:
    def __init__(self, repository, identifier, bindings):
        self.repository, self.identifier = repository, identifier
        self.bindings = tuple(bindings)
        if not self.bindings or len(self.bindings) > 32:
            raise ValueError('explicit bounded inventory page required')

    def initialize(self):
        with self.repository._connection() as db:
            db.executescript('BEGIN IMMEDIATE;' + SCHEMA + 'COMMIT;')

    def begin(self, *, provenance, rollback):
        if not provenance or not rollback:
            raise ValueError('explicit inventory origin and preserved rollback required')
        files = []
        for writer, path in self.bindings:
            path = Path(path).resolve()
            source = writer.source(path)
            if not path.is_file():
                raise Unavailable('legacy adoption requires an existing regular file')
            files.append({'source':source, 'path':str(path), 'signature':file_signature(path),
                'registry':writer.registry.config['registry_id'], 'writer_epoch':writer.writer_epoch,
                'namespace':'legacy-local'})
        contract = _json({'files':files, 'provenance':provenance, 'rollback':rollback})
        with self.repository._write() as db:
            old = db.execute('SELECT contract FROM application_adoption WHERE id=?', (self.identifier,)).fetchone()
            if old and old[0] != contract:
                raise Conflict('frozen legacy adoption changed')
            db.execute('INSERT OR IGNORE INTO application_adoption(id,contract) VALUES(?,?)',
                       (self.identifier, contract))

    def state(self):
        with self.repository._connection() as db:
            from application_schema import require_schema
            require_schema(self, db, SCHEMA, ('application_adoption',))
            row = db.execute('SELECT * FROM application_adoption WHERE id=?', (self.identifier,)).fetchone()
        if row is None:
            raise Unavailable('explicit legacy adoption not begun')
        return dict(row)

    def step(self, *, fault=lambda stage: None):
        state = self.state()
        if state['status'] == 'complete':
            return {'status':'complete', 'raw_bytes':0}
        files = json.loads(state['contract'])['files']
        index = state['ordinal']
        frozen = files[index]
        writer, path = self.bindings[index]
        path = Path(path).resolve()
        if (writer.registry.config['registry_id'], writer.writer_epoch, writer.source(path), str(path)) != (
                frozen['registry'], frozen['writer_epoch'], frozen['source'], frozen['path']):
            raise Conflict('legacy adoption writer binding changed')
        if file_signature(path) != frozen['signature']:
            raise Conflict('frozen legacy source changed; no automatic repair')
        registry = _AdoptionRegistry(writer.registry.root, fault=writer.registry.fault)
        registry.recover()
        receipt = registry.state(frozen['source'])
        if receipt is None:
            registry.register(frozen['source'], 'initial', path,
                writer_epoch=writer.writer_epoch, namespace='legacy-local', allow_existing=True,
                operation='adopt-register:'+hashed(encoded([self.identifier, frozen['source']])))
            fault('after_adoption_registration')
            return {'status':'registered', 'raw_bytes':0}
        if (receipt['path'], receipt['writer_epoch'], receipt['namespace'], receipt['revision']) != (
                str(path), writer.writer_epoch, 'legacy-local', 'initial'):
            raise Conflict('legacy adoption source state changed')
        if receipt['end'] > frozen['signature'][2]:
            raise Conflict('legacy adoption exceeds frozen bytes')
        if receipt['end'] < frozen['signature'][2]:
            adopted = registry.adopt_tail(frozen['source'], expected_receipt=hashed(encoded(receipt)),
                operation='adopt:'+hashed(encoded([self.identifier, frozen['source'], receipt['end']])))
            fault('after_adoption_block')
            return {'status':'adopted', 'raw_bytes':adopted['end']-adopted['start']}
        if file_signature(path) != frozen['signature']:
            raise Conflict('legacy source changed before ACK')
        with self.repository._write() as db:
            if db.execute('UPDATE application_adoption SET ordinal=ordinal+1,status=? WHERE id=? AND ordinal=?',
                          ('complete' if index+1 == len(files) else 'preparing', self.identifier, index)).rowcount != 1:
                raise Conflict('legacy adoption ACK changed')
        fault('after_adoption_ack')
        return {'status':'complete' if index+1 == len(files) else 'acknowledged', 'raw_bytes':0}
