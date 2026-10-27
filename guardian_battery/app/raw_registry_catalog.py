"""Explicit, unactivated bounded registry-to-derived-catalog consumer.

No Raw writes, discovery, automatic retries or production defaults. Its SQLite
transaction never encloses the writer's registration or append operation.
"""
from raw_registry import MAX_BLOCK, ZERO, encoded, hashed, file_signature, RegistryConflict, RegistryCorrupt

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS registry_format(registry_id TEXT PRIMARY KEY,record_format TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS registry_cursor(registry_id TEXT PRIMARY KEY,next_seq INTEGER NOT NULL,previous_hash TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS registry_proof(registry_id TEXT,seq INTEGER,source TEXT,revision TEXT,receipt TEXT,block_chain TEXT,start INTEGER,end INTEGER,block_hash TEXT,PRIMARY KEY(registry_id,seq))',
    'CREATE INDEX IF NOT EXISTS registry_source_tip ON registry_proof(registry_id,source,seq DESC)',
)


class RegistryCatalogConsumer:
    FORMATS = frozenset(('cell', 'rs485', 'hycube_system_observation', 'hycube_history_projection', 'hycube_policy_observation',
                         'daily_rs485', 'daily_position', 'daily_config', 'daily_maintenance'))

    def __init__(self, registry, catalog, *, record_format='cell', source_formats=None):
        if record_format not in self.FORMATS:
            raise ValueError('explicit supported catalog format required')
        if source_formats is not None:
            if not source_formats or len(source_formats) > 16 or any(
                    not isinstance(key, str) or not key or value is not None and value not in self.FORMATS
                    for key, value in source_formats.items()):
                raise ValueError('explicit bounded source format map required')
        self.record_format = record_format
        self.source_formats = dict(source_formats) if source_formats is not None else None
        self.format_contract = encoded(self.source_formats).decode() if source_formats is not None else record_format
        self.registry, self.catalog = registry, catalog
        self.registry_id = registry.config['registry_id']

    def format_for(self, source):
        if self.source_formats is None:
            return self.record_format
        if source not in self.source_formats:
            raise RegistryConflict('unmapped catalog source; no implicit selection')
        return self.source_formats[source]

    def initialize(self):
        """Explicit setup only; does not consume any bytes or reset existing cursors."""
        with self.catalog.write() as db:
            for statement in SCHEMA:db.execute(statement)
            prior=db.execute('SELECT record_format FROM registry_format WHERE registry_id=?',(self.registry_id,)).fetchone()
            if prior and prior[0]!=self.format_contract:raise RegistryConflict('catalog registry format changed')
            if prior is None:
                existing=db.execute('SELECT 1 FROM registry_proof WHERE registry_id=? LIMIT 1',(self.registry_id,)).fetchone()
                if existing and self.format_contract!='cell':raise RegistryConflict('old consumer format not proven; explicit migration required')
            db.execute('INSERT OR IGNORE INTO registry_format VALUES(?,?)',(self.registry_id,self.format_contract))
            db.execute('INSERT OR IGNORE INTO registry_cursor VALUES(?,1,?)',(self.registry_id,ZERO))

    def cursor(self):
        with self.catalog.connection() as db:
            row=db.execute('SELECT next_seq,previous_hash FROM registry_cursor WHERE registry_id=?',(self.registry_id,)).fetchone()
            if row is None:raise RegistryConflict('consumer not initialized')
            return tuple(row)

    def step(self,*,expected_cursor,max_records=32,max_raw_bytes=MAX_BLOCK,fault=lambda _:None):
        if not 1<=max_raw_bytes<=MAX_BLOCK:raise ValueError('invalid raw proof budget')
        if self.cursor()!=expected_cursor:raise RegistryConflict('consumer cursor changed')
        page=self.registry.read_page(next_seq=expected_cursor[0],previous_hash=expected_cursor[1],max_records=max_records)
        selected=[];used=0
        # Verify bounded source evidence before acquiring the derived DB write lock.
        for r in page['records']:
            size=r['end']-r['start']
            if not 0<=size<=MAX_BLOCK:raise RegistryCorrupt('invalid receipt range')
            if used+size>max_raw_bytes:break
            sig=file_signature(r['path'])
            if sig[:2]!=r['file_id'] or sig[2]<r['end']:raise RegistryConflict('receipt source unavailable or truncated')
            data=self.registry._raw_range(r['path'],r['start'],r['end']) if size else b''
            if hashed(data)!=r['block_hash'] or (size and not data.endswith(b'\n')) or data.count(b'\n')!=r['lines']:
                raise RegistryConflict('receipt block changed or incomplete')
            if file_signature(r['path'])!=sig:raise RegistryConflict('raw changed during bounded proof')
            chain=hashed(encoded([r['block_previous'],r['start'],r['end'],r['block_hash']]))
            if chain!=r['block_chain']:raise RegistryCorrupt('block chain mismatch')
            selected.append((r,sig));used+=size
        cursor=expected_cursor
        with self.catalog.write() as db:
            current=db.execute('SELECT next_seq,previous_hash FROM registry_cursor WHERE registry_id=?',(self.registry_id,)).fetchone()
            if tuple(current)!=expected_cursor:raise RegistryConflict('consumer cursor changed')
            for r,sig in selected:
                prior=db.execute('SELECT * FROM registry_proof WHERE registry_id=? AND source=? ORDER BY seq DESC LIMIT 1',
                                 (self.registry_id,r['source'])).fetchone()
                if (None if prior is None else prior['receipt'])!=r['source_previous']:
                    raise RegistryConflict('source proof predecessor mismatch')
                if file_signature(r['path'])!=sig:raise RegistryConflict('source changed before catalog transaction')
                receipt=hashed(encoded(r))
                selected_format = self.format_for(r['source'])
                if r['mode'] in ('register','replace'):
                    if r['start'] or r['end'] or r['block_previous']!=ZERO:raise RegistryCorrupt('invalid registration frontier')
                    if selected_format is not None:
                        self.catalog._register(db,dict(source=r['source'],revision=r['revision'],path=r['path'],
                            namespace=r['namespace'],writer_epoch=r['writer_epoch'],receipt=receipt,frontier=0,
                            expected_revision=r['previous_revision'],observed_signature=sig,
                            context={'registry_id':self.registry_id,'record_format':selected_format}))
                elif r['mode'] in ('append','adopt'):
                    if prior is None or prior['revision']!=r['revision'] or prior['end']!=r['start'] or prior['block_chain']!=r['block_previous']:
                        raise RegistryConflict('source block frontier mismatch')
                    if selected_format is not None:
                        self.catalog._confirm_append(db,r['source'],r['revision'],expected_receipt=r['source_previous'],
                                                     receipt=receipt,frontier=r['end'],writer_epoch=r['writer_epoch'])
                else:raise RegistryCorrupt('unknown receipt mode')
                db.execute('INSERT INTO registry_proof VALUES(?,?,?,?,?,?,?,?,?)',
                           (self.registry_id,r['seq'],r['source'],r['revision'],receipt,r['block_chain'],r['start'],r['end'],r['block_hash']))
                cursor=(r['seq']+1,receipt)
            fault('after_catalog_records')
            db.execute('UPDATE registry_cursor SET next_seq=?,previous_hash=? WHERE registry_id=?',(*cursor,self.registry_id))
            fault('after_catalog_cursor')
        fault('after_catalog_commit')
        return dict(cursor=cursor,records=len(selected),raw_bytes=used,metadata_bytes=page['metadata_bytes'],
                    caught_up=cursor[0]>page['head_seq'],budget_blocked=not selected and bool(page['records']))
