"""Explicit local migration control over real bounded store adapters.

No discovery, startup activation, source mutation or implied readiness. The
caller initializes adapters and supplies exact provenance/rollback scope.
Cross-store steps commit before their acknowledgement; resume inspects the
adapter's durable cursor instead of replaying an acknowledged input page.
"""
import json
import uuid
from projection_repository import Conflict,_json
from snapshot_lifecycle import identity
from store_retirement import fingerprint

SCHEMA='''
CREATE TABLE IF NOT EXISTS lifecycle_migration(id TEXT PRIMARY KEY,nonce TEXT NOT NULL,contract TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'preparing');
CREATE TABLE IF NOT EXISTS lifecycle_migration_unit(migration TEXT REFERENCES lifecycle_migration ON DELETE CASCADE,ordinal INTEGER,kind TEXT,scope TEXT,stores TEXT,status TEXT DEFAULT 'pending',evidence TEXT,PRIMARY KEY(migration,ordinal));
'''


def store_contract(store):
    connection=getattr(store,'_connection',None) or store.connection
    with connection() as db:
        return {'identity':identity(store.path),'schema':fingerprint(db)}


class MigrationManifest:
    def __init__(self,repository,identifier):self.repository,self.identifier=repository,identifier
    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
    @staticmethod
    def describe(adapter):
        from store_retirement import StoreRetirement
        from snapshot_lifecycle import SnapshotLifecycle
        from metadata_migration import MetadataMigration
        from metadata_log_consumer import MetadataLogConsumer
        from history_epoch_adapter import EpochLogConsumer
        from source_catalog import SourceCatalog
        from legacy_source_inventory import LegacySourceInventory
        from publication_window_index import PublicationWindowIndex
        if isinstance(adapter,StoreRetirement):return 'ownership',[adapter.repository]
        if isinstance(adapter,SnapshotLifecycle):return 'snapshots',[adapter.repository,adapter.catalog]
        if isinstance(adapter,MetadataMigration):return 'metadata-file',[adapter.repository]
        if isinstance(adapter,MetadataLogConsumer):return 'metadata-log',[adapter.repository]
        if isinstance(adapter,EpochLogConsumer):return 'epoch-log',[adapter.store]
        if isinstance(adapter,SourceCatalog):return 'catalog-index',[adapter]
        if isinstance(adapter,LegacySourceInventory):return 'inventory',[adapter.catalog]
        if isinstance(adapter,PublicationWindowIndex):return 'publication-index',[adapter.repository]
        raise ValueError('unsupported migration adapter; no inferred conversion')
    def begin(self,units,*,provenance,rollback,semantics='guardian-local-store-migration-1'):
        if semantics!='guardian-local-store-migration-1' or not provenance or not rollback or not 1<=len(units)<=32:
            raise ValueError('explicit supported migration scope/provenance/rollback required')
        descriptions=[]
        for adapter,scope in units:
            kind,stores=self.describe(adapter)
            if not isinstance(scope,dict) or not scope.get('origin'):raise ValueError('confirmed origin required for each store step')
            if kind=='metadata-file' and scope.get('revision')!=adapter.revision:raise ValueError('exact metadata revision required')
            if kind in ('metadata-log','epoch-log'):
                if scope.get('registry')!=adapter.identity or not isinstance(scope.get('last_sequence'),int):raise ValueError('explicit registry and frozen receipt frontier required')
                if kind=='metadata-log' and scope.get('revision')!=adapter.revision.revision:raise ValueError('metadata revision mismatch')
            descriptions.append((kind,scope,[store_contract(s) for s in stores]))
        contract=_json({'semantics':semantics,'provenance':provenance,'rollback':rollback,'units':descriptions})
        with self.repository._write() as db:
            prior=db.execute('SELECT contract FROM lifecycle_migration WHERE id=?',(self.identifier,)).fetchone()
            if prior:
                if prior[0]!=contract:raise Conflict('migration manifest changed')
                return
            db.execute('INSERT INTO lifecycle_migration(id,nonce,contract) VALUES(?,?,?)',(self.identifier,uuid.uuid4().hex,contract))
            for ordinal,(kind,scope,stores) in enumerate(descriptions):
                db.execute('INSERT INTO lifecycle_migration_unit(migration,ordinal,kind,scope,stores) VALUES(?,?,?,?,?)',(self.identifier,ordinal,kind,_json(scope),_json(stores)))
    def state(self):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM lifecycle_migration WHERE id=?',(self.identifier,)).fetchone()
            if row is None:raise Conflict('migration manifest absent')
            return dict(row)
    def step(self,adapter,*,expected_version,fault=lambda stage:None):
        state=self.state()
        if state['version']!=expected_version:raise Conflict('migration manifest CAS changed')
        if state['status']=='complete':return {'complete':True,'version':expected_version,'work':0}
        with self.repository._connection() as db:
            unit=db.execute("SELECT * FROM lifecycle_migration_unit WHERE migration=? AND status='pending' ORDER BY ordinal LIMIT 1",(self.identifier,)).fetchone()
        if unit is None:raise Conflict('missing pending migration unit')
        kind,stores=self.describe(adapter);scope=json.loads(unit['scope'])
        if kind!=unit['kind'] or [store_contract(s) for s in stores]!=json.loads(unit['stores']):raise Conflict('migration store identity/schema changed')
        done=False
        if kind=='ownership':
            result=adapter.index_step(limit=8);done=result['complete']
        elif kind=='snapshots':
            result=adapter.bind_step();done=result['complete']
        elif kind=='metadata-file':
            if adapter.revision!=scope['revision']:raise Conflict('metadata revision changed')
            result=adapter.step(expected_version=adapter.status()['version'],rows=8);done=result['complete']
        elif kind in ('metadata-log','epoch-log'):
            if adapter.identity!=scope['registry']:raise Conflict('registry identity changed')
            if kind=='metadata-log' and adapter.revision.revision!=scope['revision']:raise Conflict('metadata revision changed')
            cursor=adapter.cursor()
            if cursor[0]>scope['last_sequence']:
                if cursor[0]!=scope['last_sequence']+1 or cursor[1]:raise Conflict('consumer advanced beyond frozen manifest')
                if kind=='metadata-log':
                    revision=adapter.revision;current=revision.state()
                    if current['status']=='ready':result={'revision':revision.revision,'state':current};done=True
                    else:
                        result=revision.position_step(expected_version=current['version'],limit=8)
                        if result['complete']:
                            revision.publish(expected_version=revision.state()['version'],head=scope['head'],expected_head=scope['expected_head'],scope_proof={'manifest':self.identifier,'origin':scope['origin']})
                            result={'revision':revision.revision,'state':revision.state()};done=True
                else:result={'cursor':cursor};done=True
            else:
                result=adapter.step(expected_cursor=cursor,max_records=8)
                # caught_up describes the journal, not failure to reach the
                # frozen manifest. Inspect the committed cursor after the page.
                committed=adapter.cursor()
                frontier=scope['last_sequence']+1
                if committed[0]>frontier or (committed[0]==frontier and committed[1]):
                    raise Conflict('consumer advanced beyond frozen manifest')
                if result.get('caught_up') and (committed[0]!=frontier or committed[1]):
                    raise Conflict('manifest receipt frontier unavailable')
        elif kind=='catalog-index':
            if 'catalog_state' in scope:
                current=adapter.status()
                if current!=scope['catalog_state'] or current['pending']:
                    raise Conflict('frozen complete catalog scope changed or still pending')
                result={'catalog_state':current,'migration_scope_complete':True};done=True
            else:
                current=adapter.progress(scope['source'],scope['revision'])
                if (current['frontier'],current['receipt'])!=(scope['frontier'],scope['receipt']):raise Conflict('catalog source frontier changed')
                result=current;done=current['cursor']==current['frontier']
                if not done:result=adapter.index_step(scope['source'],scope['revision'],expected_cursor=current['cursor'],max_records=8)
        elif kind=='inventory':
            result=adapter.status(scope['job']);done=result['status']=='complete'
            if not done:result=adapter.step(scope['job'],max_entries=8)
        else:
            current=adapter.state(scope['bundle'],scope['product'],scope['day']);done=bool(current['complete']);result=current
            if not done:result=adapter.step(scope['bundle'],scope['product'],scope['day']);done=result['complete']
        fault('after_adapter_commit')
        with self.repository._write() as db:
            if db.execute('SELECT version FROM lifecycle_migration WHERE id=?',(self.identifier,)).fetchone()[0]!=expected_version:raise Conflict('migration acknowledgement CAS changed; resume actual cursor')
            db.execute('UPDATE lifecycle_migration_unit SET status=?,evidence=? WHERE migration=? AND ordinal=?',('complete' if done else 'pending',_json(result),self.identifier,unit['ordinal']))
            pending=db.execute("SELECT 1 FROM lifecycle_migration_unit WHERE migration=? AND status='pending' LIMIT 1",(self.identifier,)).fetchone()
            db.execute('UPDATE lifecycle_migration SET version=version+1,status=? WHERE id=?',('preparing' if pending else 'complete',self.identifier))
            fault('before_manifest_commit')
        fault('after_manifest_commit')
        return {'complete':not pending,'version':expected_version+1,'unit':unit['ordinal'],'evidence':result}
    def archive_page(self,*,after=-1,limit=8):
        if not 1<=limit<=32:raise ValueError('archive page budget')
        state=self.state()
        if state['status']!='complete':raise Conflict('migration not complete')
        with self.repository._connection() as db:
            rows=db.execute('SELECT * FROM lifecycle_migration_unit WHERE migration=? AND ordinal>? ORDER BY ordinal LIMIT ?',(self.identifier,after,limit)).fetchall()
        return {'manifest':state,'units':[dict(r) for r in rows],'next':rows[-1]['ordinal'] if rows else None}
    def prune(self,*,nonce,archive_sha256,retry_closed):
        if retry_closed is not True or len(archive_sha256)!=64 or any(c not in '0123456789abcdef' for c in archive_sha256):raise ValueError('explicit archive/retry-closure proof required')
        with self.repository._write() as db:
            row=db.execute('SELECT * FROM lifecycle_migration WHERE id=?',(self.identifier,)).fetchone()
            if row is None:return
            if row['status']!='complete' or row['nonce']!=nonce:raise Conflict('active migration or stale archival proof')
            # The manifest has an enforced maximum of 32 units, not source rows.
            db.execute('DELETE FROM lifecycle_migration WHERE id=?',(self.identifier,))
