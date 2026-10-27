"""Durable application subscriber to confirmed preparation jobs.

One invocation performs one bounded repository operation. HTTP never invokes
this subscriber. ACK follows the child commit; restart repeats only an
idempotent completion check, never a Raw append or a scientific full rebuild.
"""
import json
from projection_repository import Conflict, Unavailable, _json

SCHEMA = '''
CREATE TABLE IF NOT EXISTS application_tail(
 name TEXT PRIMARY KEY, after_job INTEGER NOT NULL DEFAULT 0,
 job INTEGER REFERENCES preparation_job(id), tasks TEXT, position INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS application_tail_frontier ON application_tail(after_job);
CREATE INDEX IF NOT EXISTS application_tail_job ON application_tail(job);
CREATE TABLE IF NOT EXISTS application_tail_task(
 name TEXT REFERENCES application_tail(name), position INTEGER,
 bundle INTEGER, product TEXT, partition_key TEXT,
 PRIMARY KEY(name,position),
 FOREIGN KEY(bundle,product,partition_key) REFERENCES window_index_job(bundle,product,partition_key));
CREATE INDEX IF NOT EXISTS application_tail_target ON application_tail_task(bundle,product,partition_key);
'''


class ApplicationTail:
    def __init__(self, preparation, diagnostics, publication_index, *, name):
        self.preparation = preparation
        self.repository = preparation.repository
        self.diagnostics = diagnostics
        self.index = publication_index
        self.name = name

    def initialize(self):
        with self.repository._connection() as db:
            db.executescript('BEGIN IMMEDIATE;' + SCHEMA + 'COMMIT;')
        with self.repository._write() as db:
            db.execute('INSERT OR IGNORE INTO application_tail(name) VALUES(?)', (self.name,))

    def state(self):
        with self.repository._connection() as db:
            from application_schema import require_schema
            require_schema(self, db, SCHEMA, ('application_tail', 'application_tail_task'))
            row = db.execute('SELECT * FROM application_tail WHERE name=?', (self.name,)).fetchone()
        if row is None:
            raise Unavailable('application tail requires explicit preparation')
        return dict(row)

    def _tasks(self, job):
        payload = json.loads(job['payload'])
        if job['kind'] == 'registry':
            receipt = payload['receipt']
            return ([{'kind': 'diagnostic', 'source': receipt['source'],
                      'revision': receipt['revision'], 'end': receipt['end']}]
                    if receipt['end'] else [])
        if job['kind'] != 'display':
            return []
        day = payload['day']
        with self.repository._connection() as db:
            streams = [row[0] for row in db.execute(
                'SELECT stream FROM canonical_stream WHERE origin<=? ORDER BY stream LIMIT 65', (day,))]
        if len(streams) > 64:
            raise Unavailable('explicit application stream budget exceeded')
        products = [(self.preparation.canonical._head(stream, day),
                     self.preparation.canonical._product(stream, day), 'canonical') for stream in streams]
        products.append((self.preparation.display.head(day), self.preparation.display.product(day), 'display'))
        tasks = []
        for head, product, kind in products:
            bundle = self.repository.head(head)
            if bundle is None:
                raise Unavailable('completed display job has unpublished dependency')
            self.index.begin(bundle, product, day, kind)  # Durable pin before task dispatch.
            tasks.append({'kind': kind, 'bundle': bundle, 'product': product, 'day': day})
        return tasks

    def step(self, *, fault=lambda stage: None):
        state = self.state()
        if state['job'] is None:
            with self.repository._connection() as db:
                row = db.execute('SELECT * FROM preparation_job WHERE id>? ORDER BY id LIMIT 1',
                                 (state['after_job'],)).fetchone()
            if row is None:
                return {'status': 'caught_up', 'raw_bytes': 0, 'decodes': 0}
            if row['status'] != 'done':
                return {'status': row['status'], 'job': row['id'], 'raw_bytes': 0, 'decodes': 0}
            tasks = self._tasks(row)
            with self.repository._write() as db:
                if db.execute('UPDATE application_tail SET job=?,tasks=?,position=0 WHERE name=? AND job IS NULL AND after_job=?',
                              (row['id'], _json(tasks), self.name, state['after_job'])).rowcount != 1:
                    raise Conflict('application tail cursor changed')
                for position, task in enumerate(tasks):
                    if task['kind'] != 'diagnostic':
                        db.execute('INSERT INTO application_tail_task VALUES(?,?,?,?,?)',
                                   (self.name, position, task['bundle'], task['product'], task['day']))
            fault('after_tail_dispatch')
            return {'status': 'dispatched', 'raw_bytes': 0, 'decodes': 0}
        tasks = json.loads(state['tasks'])
        if state['position'] == len(tasks):
            with self.repository._write() as db:
                if db.execute('UPDATE application_tail SET after_job=job,job=NULL,tasks=NULL,position=0 WHERE name=? AND job=? AND position=?',
                              (self.name, state['job'], state['position'])).rowcount != 1:
                    raise Conflict('application tail ACK changed')
            fault('after_tail_ack')
            return {'status': 'acknowledged', 'raw_bytes': 0, 'decodes': 0}
        task = tasks[state['position']]
        if task['kind'] == 'diagnostic':
            result = self.diagnostics.consume(self.name, task['source'], task['revision'],
                                              target_offset=task['end'], fault=fault)
            complete = result['status'] == 'caught_up'
        else:
            self.index.begin(task['bundle'], task['product'], task['day'], task['kind'])
            result = self.index.step(task['bundle'], task['product'], task['day'])
            complete = result['complete']
        fault('after_tail_child_commit')
        if complete:
            with self.repository._write() as db:
                if db.execute('UPDATE application_tail SET position=position+1 WHERE name=? AND job=? AND position=?',
                              (self.name, state['job'], state['position'])).rowcount != 1:
                    raise Conflict('application tail task ACK changed')
                db.execute('DELETE FROM application_tail_task WHERE name=? AND position=?',
                           (self.name, state['position']))
        return result
