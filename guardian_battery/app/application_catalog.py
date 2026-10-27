"""One receipt and one index page per background step; no directory discovery."""
import json
from projection_repository import Conflict, Unavailable, _json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS application_catalog(
 name TEXT PRIMARY KEY, binding TEXT NOT NULL, receipt TEXT, cursor TEXT);
'''


class CatalogTail:
    def __init__(self, consumer, *, name):
        self.consumer, self.name = consumer, name
        self.catalog = consumer.catalog
        from confirmed_prefix_reader import ConfirmedPrefixReader
        self.prefix_reader = ConfirmedPrefixReader(consumer.registry)

    def initialize(self):
        self.consumer.initialize()
        binding = _json({'registry': self.consumer.registry_id,
                         'formats': self.consumer.format_contract})
        with self.catalog.connection() as db:
            db.executescript('BEGIN IMMEDIATE;' + SCHEMA + 'COMMIT;')
        with self.catalog.write() as db:
            row = db.execute('SELECT binding FROM application_catalog WHERE name=?', (self.name,)).fetchone()
            if row and row[0] != binding:
                raise Conflict('catalog application binding changed')
            db.execute('INSERT OR IGNORE INTO application_catalog(name,binding) VALUES(?,?)', (self.name, binding))

    def state(self):
        with self.catalog.connection() as db:
            from application_schema import require_schema
            require_schema(self, db, SCHEMA, ('application_catalog',))
            row = db.execute('SELECT * FROM application_catalog WHERE name=?', (self.name,)).fetchone()
        if row is None:
            raise Unavailable('catalog application not prepared')
        return dict(row)

    def step(self, *, fault=lambda stage: None):
        row = self.state()
        if row['receipt'] is None:
            cursor = self.consumer.cursor()
            page = self.consumer.registry.read_page(next_seq=cursor[0], previous_hash=cursor[1], max_records=1)
            if not page['records']:
                return {'status':'caught_up', 'raw_bytes':0, 'decodes':0}
            receipt = page['records'][0]
            self.consumer.format_for(receipt['source'])
            with self.catalog.write() as db:
                if db.execute('UPDATE application_catalog SET receipt=?,cursor=? WHERE name=? AND receipt IS NULL',
                              (_json(receipt), _json(cursor), self.name)).rowcount != 1:
                    raise Conflict('catalog application dispatch changed')
            fault('after_catalog_dispatch')
            return {'status':'dispatched', 'raw_bytes':0, 'decodes':0}
        receipt = json.loads(row['receipt'])
        original = tuple(json.loads(row['cursor']))
        current = self.consumer.cursor()
        if current == original:
            return self.consumer.step(expected_cursor=current, max_records=1, fault=fault)
        if current[0] != receipt['seq'] + 1:
            raise Conflict('catalog application consumer ownership changed')
        if self.consumer.format_for(receipt['source']) is not None:
            state = self.catalog.progress(receipt['source'], receipt['revision'])
            if state['cursor'] < receipt['end']:
                return self.catalog.index_step(receipt['source'], receipt['revision'],
                    expected_cursor=state['cursor'], max_records=8, fault=fault, prefix_reader=self.prefix_reader)
        fault('after_catalog_child_commit')
        with self.catalog.write() as db:
            if db.execute('UPDATE application_catalog SET receipt=NULL,cursor=NULL WHERE name=? AND receipt=?',
                          (self.name, row['receipt'])).rowcount != 1:
                raise Conflict('catalog application ACK changed')
        fault('after_catalog_ack')
        return {'status':'acknowledged', 'raw_bytes':0, 'decodes':0}
