"""Local incremental metadata revisions on immutable pages; no runtime wiring."""
from contextlib import contextmanager,nullcontext
from datetime import datetime,timezone
from dataclasses import replace
import hashlib
import json
import uuid
from projection_repository import Conflict,Unavailable,_json
from metadata_tree import MetadataTree,SCHEMA as TREE_SCHEMA
from position_history import PositionSnapshot
from research_identity import ResearchIdentityResolver
from maintenance import MaintenanceEvent,normalize_utc_timestamp
from maintenance_service import MaintenanceHistoryError
from timeline import TimelineEvent,TechnicalEventSource,project_maintenance
from research_budget import ResearchQueryError,check_budget

SEMANTICS='original-position-epochs/maintenance-revisions/timeline-v1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS metadata_revision(id TEXT PRIMARY KEY,parent TEXT,provenance TEXT,semantics TEXT,
 status TEXT,version INTEGER DEFAULT 0,last_position TEXT DEFAULT '',previous TEXT DEFAULT '{}',ordinal INTEGER DEFAULT 0,completion TEXT,position_phase TEXT DEFAULT 'ready',dirty_from TEXT,position_after TEXT DEFAULT '',parent_proof TEXT);
CREATE TABLE IF NOT EXISTS metadata_root(revision TEXT REFERENCES metadata_revision,name TEXT,
 node INTEGER REFERENCES metadata_node,PRIMARY KEY(revision,name));
CREATE INDEX IF NOT EXISTS metadata_root_node ON metadata_root(node);
CREATE TABLE IF NOT EXISTS metadata_cursor(revision TEXT REFERENCES metadata_revision,source TEXT,
 sequence INTEGER,digest TEXT,PRIMARY KEY(revision,source));
CREATE TABLE IF NOT EXISTS metadata_maintenance_reset(revision TEXT REFERENCES metadata_revision,source TEXT,after_key TEXT DEFAULT '',PRIMARY KEY(revision,source));
CREATE TABLE IF NOT EXISTS metadata_head(name TEXT PRIMARY KEY,revision TEXT REFERENCES metadata_revision);
CREATE TABLE IF NOT EXISTS metadata_reader(token TEXT PRIMARY KEY,revision TEXT REFERENCES metadata_revision);
CREATE INDEX IF NOT EXISTS metadata_reader_revision ON metadata_reader(revision);
'''
ROOTS=('positions','topology','epochs','serials','timeline','maintenance','config','epoch_order','maintenance_revisions','maintenance_requests','maintenance_order','position_serials','position_starts','position_start_order','position_nonempty','maintenance_retired')


def timekey(value):
    return datetime.fromisoformat(normalize_utc_timestamp(value,'timestamp').replace('Z','+00:00')).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def serialkey(serial):return serial.encode().hex()+'/'
def eventkey(event):return timekey(event.timestamp)+'/'+hashlib.sha256(_json(event.to_dict()).encode()).hexdigest()


class MetadataRevision:
    def __init__(self,repository,revision):self.repository,self.revision=repository,revision
    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+TREE_SCHEMA+SCHEMA+'COMMIT;')
    def begin(self,*,provenance,parent=None,semantics=SEMANTICS):
        if not provenance or semantics!=SEMANTICS:raise ValueError('explicit provenance and supported semantics required')
        proof=_json(provenance)
        with self.repository._write() as db:
            old=db.execute('SELECT parent,provenance,semantics FROM metadata_revision WHERE id=?',(self.revision,)).fetchone()
            if old:
                if tuple(old)!=(parent,proof,semantics):raise Conflict('immutable revision contract changed')
                return
            prior=None
            if parent:
                prior=db.execute('SELECT * FROM metadata_revision WHERE id=?',(parent,)).fetchone()
                if prior is None or prior['status']!='ready' or prior['semantics']!=semantics:raise Conflict('parent not confirmed/compatible')
            parent_proof=None
            if parent:
                roots=db.execute('SELECT r.name,n.digest FROM metadata_root r LEFT JOIN metadata_node n ON n.id=r.node WHERE r.revision=? ORDER BY r.name',(parent,)).fetchall()
                if {r[0] for r in roots}!=set(ROOTS):raise Conflict('parent root manifest incomplete')
                parent_proof=_json({'revision':parent,'state_sha256':hashlib.sha256(_json(dict(prior)).encode()).hexdigest(),'roots':[tuple(r) for r in roots]})
            db.execute("INSERT INTO metadata_revision(id,parent,provenance,semantics,status,last_position,previous,ordinal,parent_proof) VALUES(?,?,?,?,'preparing',?,?,?,?)",(self.revision,parent,proof,semantics,prior['last_position'] if prior else '',prior['previous'] if prior else '{}',prior['ordinal'] if prior else 0,parent_proof))
            for name in ROOTS:
                node=db.execute('SELECT node FROM metadata_root WHERE revision=? AND name=?',(parent,name)).fetchone() if parent else None
                db.execute('INSERT INTO metadata_root VALUES(?,?,?)',(self.revision,name,node[0] if node else None))
            if parent:
                rows=db.execute('SELECT source,sequence,digest FROM metadata_cursor WHERE revision=? ORDER BY source LIMIT 17',(parent,)).fetchall()
                if len(rows)>16:raise Conflict('source scope exceeds declared budget')
                db.executemany('INSERT INTO metadata_cursor VALUES(?,?,?,?)',[(self.revision,*r) for r in rows])
    def state(self):
        with self.repository._connection() as db:
            row=db.execute('SELECT * FROM metadata_revision WHERE id=?',(self.revision,)).fetchone()
            if row is None:raise Conflict('metadata revision absent')
            return dict(row)
    def _trees(self,db):
        roots={r['name']:r['node'] for r in db.execute('SELECT * FROM metadata_root WHERE revision=?',(self.revision,))}
        if set(roots)!=set(ROOTS):raise Conflict('metadata root manifest incomplete')
        return {name:MetadataTree(db,node) for name,node in roots.items()}
    @staticmethod
    def _position(trees,state,item):
        key=timekey(item.effective_at)+'/'+item.created_at+'|'+item.position_history_id
        if key<=state['last_position']:raise Conflict('retroactive/out-of-order position requires explicit affected-window correction')
        previous=json.loads(state['previous']);positions=item.positions;ordinal=state['ordinal']
        for serial in sorted((set(previous.values())|set(positions.values()))-{None}):
            old=next((int(k) for k,v in previous.items() if v==serial),None)
            new=next((int(k) for k,v in positions.items() if v==serial),None)
            prior=trees['epochs'].floor(serialkey(serial)+timekey(item.effective_at)+'/~')
            if prior and not prior[0].startswith(serialkey(serial)):prior=None
            if prior and old==new:continue
            if prior:trees['epochs'].put(prior[0],dict(prior[1],valid_to=item.effective_at))
            ordinal+=1
            epoch={'physical_serial':serial,'position':new,'valid_from':item.effective_at,'valid_to':None,
                'position_history_id':item.position_history_id,'identity_epoch_id':ResearchIdentityResolver._epoch_id(serial,item.effective_at,new)}
            epoch_key=serialkey(serial)+timekey(item.effective_at)+'/'+f'{ordinal:020d}'
            trees['epochs'].put(epoch_key,epoch)
            trees['epoch_order'].put(timekey(item.effective_at)+'/'+serialkey(serial)+f'{ordinal:020d}',{'key':epoch_key,'serial':serial})
            trees['serials'].put(serialkey(serial),serial)
        # Selected-position lookups must not replay every historical snapshot.
        # Starts retain the original service's transition semantics, including
        # equal-time ordering; occurrence labels are never guessed from slots.
        for pos,serial in positions.items():
            if serial is None or previous.get(pos)==serial:
                continue
            prefix=pos+'/'+serialkey(serial)
            start_key=prefix+key
            nonempty_key=pos+'/'+key
            trees['position_serials'].put(prefix,serial)
            trees['position_starts'].put(start_key,item.effective_at)
            trees['position_nonempty'].put(nonempty_key,serial)
            trees['position_start_order'].put(key+'/'+pos,
                {'start':start_key,'nonempty':nonempty_key})
        topology={'timestamp':item.effective_at,'position_history_id':item.position_history_id,'positions':[]}
        for pos,serial in positions.items():
            prior=trees['epochs'].floor(serialkey(serial)+timekey(item.effective_at)+'/~') if serial else None
            topology['positions'].append({'position':int(pos),'physical_serial':serial,'resolved':serial is not None,
                'position_history_id':item.position_history_id,'identity_epoch_id':prior[1]['identity_epoch_id'] if prior else None})
        trees['positions'].put(key,item.to_dict());trees['topology'].put(timekey(item.effective_at),topology)
        state.update(last_position=key,previous=_json(positions),ordinal=ordinal)
    @staticmethod
    def _maintenance(trees,item):
        prior=trees['maintenance'].get(item.maintenance_event_id)
        expected=1 if prior is None else prior['revision']+1
        if item.revision!=expected:raise MaintenanceHistoryError(f'revision gap or duplicate: expected {expected}, found {item.revision}')
        if prior:
            if item.created_at!=prior['created_at'] or item.updated_at is None:raise MaintenanceHistoryError('maintenance revision provenance changed')
            trees['timeline'].delete(prior['timeline_key'])
            trees['maintenance_order'].delete(timekey(prior['record']['occurred_at'])+'/'+item.maintenance_event_id)
        elif item.updated_at is not None:raise MaintenanceHistoryError('initial revision has updated_at')
        event=project_maintenance(item,item.module_serial);key=eventkey(event)
        trees['timeline'].put(key,event.to_dict())
        trees['maintenance'].put(item.maintenance_event_id,{'revision':item.revision,'created_at':item.created_at,'timeline_key':key,'record':item.to_dict()})
        trees['maintenance_order'].put(timekey(item.occurred_at)+'/'+item.maintenance_event_id,item.to_dict())
        trees['maintenance_revisions'].put(item.maintenance_event_id+'/'+f'{item.revision:012d}',item.to_dict())
        if item.revision == 1 and item.source.get('create_request_id'):
            request = item.source['create_request_id']
            if trees['maintenance_requests'].get(request) is not None:
                raise MaintenanceHistoryError('duplicate maintenance create request')
            trees['maintenance_requests'].put(request,{'event_id':item.maintenance_event_id,'first':item.to_dict()})
    def apply_page(self,kind,records,*,source,sequence,expected_version,provenance,fault=lambda stage:None,_db=None):
        # Explicit confirmed-source adapter feeds bounded pages. This method
        # does not discover files, infer missing prefixes, or assert fsync.
        if kind not in ('position','maintenance','retired','technical','timeline_dto','config') or not source or not provenance:raise ValueError('declared source/provenance required')
        if not isinstance(records,(list,tuple)) or not 1<=len(records)<=16:raise ValueError('metadata page record budget')
        encoded=_json({'kind':kind,'records':records,'provenance':provenance})
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with (self.repository._write() if _db is None else nullcontext(_db)) as db:
            state=dict(db.execute('SELECT * FROM metadata_revision WHERE id=?',(self.revision,)).fetchone())
            if state['status']!='preparing' or state['version']!=expected_version:raise Conflict('metadata page CAS changed')
            prior=db.execute('SELECT sequence,digest FROM metadata_cursor WHERE revision=? AND source=?',(self.revision,source)).fetchone()
            if sequence!=(prior['sequence']+1 if prior else 1):raise Conflict('metadata source sequence changed')
            if prior is None and len(db.execute('SELECT source FROM metadata_cursor WHERE revision=? LIMIT 16',(self.revision,)).fetchall())>=16:raise Conflict('metadata source scope budget')
            trees=self._trees(db)
            for ordinal,record in enumerate(records):
                check_budget()
                if kind=='position':
                    item=PositionSnapshot.from_dict(record)
                    key=timekey(item.effective_at)+'/'+item.created_at+'|'+item.position_history_id
                    if trees['positions'].get(key) is not None:raise Conflict('duplicate position snapshot')
                    trees['positions'].put(key,item.to_dict())
                    dirty=timekey(item.effective_at)
                    state['dirty_from']=min(state['dirty_from'],dirty) if state.get('dirty_from') else dirty
                    state['position_phase']='trim_epochs'
                elif kind=='maintenance':self._maintenance(trees,MaintenanceEvent.from_dict(record))
                elif kind=='retired':
                    digest=record['digest']
                    if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                        raise ValueError('invalid retired request digest')
                    trees['maintenance_retired'].put(digest,True)
                elif kind=='config':
                    from history_epoch_adapter import decode_epoch
                    stamp,priority,identity,value=decode_epoch('config',record)
                    trees['config'].put(timekey(value['timestamp'])+'/'+priority,value)
                else:
                    event=TechnicalEventSource._project(record,provenance['line_numbers'][ordinal] if 'line_numbers' in provenance else provenance['first_line']+ordinal) if kind=='technical' else TimelineEvent(**record)
                    trees['timeline'].put(eventkey(event),event.to_dict())
            for name,tree in trees.items():db.execute('UPDATE metadata_root SET node=? WHERE revision=? AND name=?',(tree.root,self.revision,name))
            db.execute('INSERT INTO metadata_cursor VALUES(?,?,?,?) ON CONFLICT(revision,source) DO UPDATE SET sequence=excluded.sequence,digest=excluded.digest',(self.revision,source,sequence,hashlib.sha256(((prior['digest'] if prior else '0'*64)+digest).encode()).hexdigest()))
            db.execute('UPDATE metadata_revision SET last_position=?,previous=?,ordinal=?,position_phase=?,dirty_from=?,position_after=?,version=version+1 WHERE id=?',(state['last_position'],state['previous'],state['ordinal'],state['position_phase'],state['dirty_from'],'' if kind=='position' else state['position_after'],self.revision))
            fault('before_metadata_page_commit')
            measured={'records':len(records),'input_bytes':len(encoded.encode()),'node_reads':sum(t.reads for t in trees.values()),'node_writes':sum(t.writes for t in trees.values()),'version':expected_version+1}
        fault('after_metadata_page_commit');return measured
    def reset_maintenance_step(self,source,*,db,limit=8):
        """Private replacement revision: remove only maintenance timeline rows.

        Original current/leased revisions remain immutable until publication
        and their separately fenced retirement; no timeline DTO is rewritten.
        """
        state=db.execute('SELECT status FROM metadata_revision WHERE id=?',(self.revision,)).fetchone()
        if state is None or state[0]!='preparing':raise Conflict('maintenance reset requires private revision')
        db.execute('INSERT OR IGNORE INTO metadata_maintenance_reset(revision,source) VALUES(?,?)',(self.revision,source))
        after=db.execute('SELECT after_key FROM metadata_maintenance_reset WHERE revision=? AND source=?',(self.revision,source)).fetchone()[0]
        trees=self._trees(db);rows=trees['maintenance'].page(after=after,limit=limit)
        for _,value in rows:trees['timeline'].delete(value['timeline_key'])
        if rows:
            db.execute('UPDATE metadata_maintenance_reset SET after_key=? WHERE revision=? AND source=?',(rows[-1][0],self.revision,source))
        else:
            for name in ('maintenance','maintenance_revisions','maintenance_requests','maintenance_order'):
                trees[name].root=None
            db.execute('DELETE FROM metadata_cursor WHERE revision=? AND source=?',(self.revision,source))
            db.execute('DELETE FROM metadata_maintenance_reset WHERE revision=? AND source=?',(self.revision,source))
        for name,tree in trees.items():db.execute('UPDATE metadata_root SET node=? WHERE revision=? AND name=?',(tree.root,self.revision,name))
        db.execute('UPDATE metadata_revision SET version=version+1 WHERE id=?',(self.revision,))
        return not rows

    def position_step(self,*,expected_version,limit=8,fault=lambda stage:None):
        if not 1<=limit<=16:raise ValueError('position correction page budget')
        with self.repository._write() as db:
            state=dict(db.execute('SELECT * FROM metadata_revision WHERE id=?',(self.revision,)).fetchone())
            if state['version']!=expected_version:raise Conflict('position correction CAS changed')
            if state['position_phase']=='ready':return {'complete':True,'rows':0,'node_reads':0,'node_writes':0}
            if state['status']!='preparing':raise Conflict('confirmed revision is immutable')
            trees=self._trees(db);cut=state['dirty_from'];rows=[]
            if state['position_phase']=='trim_epochs':
                rows=trees['epoch_order'].page(after=cut,limit=limit)
                for key,value in rows:
                    trees['epochs'].delete(value['key']);trees['epoch_order'].delete(key)
                    prefix=serialkey(value['serial']);prior=trees['epochs'].floor(prefix+cut)
                    if prior and prior[0].startswith(prefix):trees['epochs'].put(prior[0],dict(prior[1],valid_to=None))
                if not rows:state['position_phase']='trim_position_starts'
            elif state['position_phase']=='trim_position_starts':
                rows=trees['position_start_order'].page(after=cut,limit=limit)
                for key,value in rows:
                    trees['position_starts'].delete(value['start'])
                    trees['position_nonempty'].delete(value['nonempty'])
                    trees['position_start_order'].delete(key)
                if not rows:state['position_phase']='trim_topology'
            elif state['position_phase']=='trim_topology':
                # Fixed UTC keys end in Z. Y is the immediate lexical lower
                # bound for the exact dirty timestamp, including all its ties.
                rows=trees['topology'].page(after=cut[:-1]+'Y',limit=limit)
                for key,_ in rows:trees['topology'].delete(key)
                if not rows:
                    previous=trees['positions'].floor(cut)
                    state['previous']=_json(previous[1]['positions']) if previous else '{}'
                    state['last_position']=previous[0] if previous else ''
                    state['position_after']=cut;state['position_phase']='project'
            elif state['position_phase']=='project':
                rows=trees['positions'].page(after=state['position_after'],limit=limit)
                for key,value in rows:self._position(trees,state,PositionSnapshot.from_dict(value))
                if rows:state['position_after']=rows[-1][0]
                else:state.update(position_phase='ready',dirty_from=None,position_after='')
            else:raise Conflict('unknown position correction state')
            for name,tree in trees.items():db.execute('UPDATE metadata_root SET node=? WHERE revision=? AND name=?',(tree.root,self.revision,name))
            db.execute('UPDATE metadata_revision SET last_position=?,previous=?,ordinal=?,position_phase=?,dirty_from=?,position_after=?,version=version+1 WHERE id=?',
                (state['last_position'],state['previous'],state['ordinal'],state['position_phase'],state['dirty_from'],state['position_after'],self.revision))
            measured={'complete':state['position_phase']=='ready','rows':len(rows),'node_reads':sum(t.reads for t in trees.values()),'node_writes':sum(t.writes for t in trees.values())}
            fault('before_position_correction_commit')
        fault('after_position_correction_commit');return measured

    def publish(self,*,expected_version,head,expected_head,scope_proof,fault=lambda stage:None):
        if not head or not scope_proof:raise ValueError('explicit completed-scope proof required')
        with self.repository._write() as db:
            state=db.execute('SELECT status,version,position_phase FROM metadata_revision WHERE id=?',(self.revision,)).fetchone()
            current=db.execute('SELECT revision FROM metadata_head WHERE name=?',(head,)).fetchone()
            if state is None or tuple(state)!=('preparing',expected_version,'ready') or (current[0] if current else None)!=expected_head:raise Conflict('metadata publication CAS changed')
            db.execute("UPDATE metadata_revision SET status='ready',completion=?,version=version+1 WHERE id=?",(_json(scope_proof),self.revision))
            db.execute('INSERT INTO metadata_head VALUES(?,?) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision',(head,self.revision))
            fault('before_metadata_publish_commit')
        fault('after_metadata_publish_commit')


class RevisionMetadataReader:
    def __init__(self,repository,revision):self.repository,self.revision=repository,revision
    @contextmanager
    def _read(self):
        token=uuid.uuid4().hex
        try:
            with self.repository._write() as db:
                row=db.execute('SELECT status,semantics FROM metadata_revision WHERE id=?',(self.revision,)).fetchone()
                if row is None or tuple(row)!=('ready',SEMANTICS):raise ResearchQueryError('history_preparation_required','metadata revision unavailable',503)
                db.execute('INSERT INTO metadata_reader VALUES(?,?)',(token,self.revision))
        except Conflict as exc:
            raise ResearchQueryError('history_preparation_required',str(exc),503) from exc
        from lifecycle_leases import read_scope,release_reader
        try:
            with read_scope(self.repository,token,'metadata_reader'), self.repository._connection() as db:
                db.execute('BEGIN');yield MetadataRevision(self.repository,self.revision)._trees(db)
        finally:
            release_reader(self.repository,token)
    @staticmethod
    def _topology(trees,timestamp):
        target=normalize_utc_timestamp(timestamp,'timestamp');value=trees['topology'].floor(timekey(target))
        return {**value[1],'timestamp':target} if value else ResearchIdentityResolver([]).topology_at(target)
    def topology_at(self,timestamp):
        with self._read() as trees:return self._topology(trees,timestamp)
    def position_at(self,physical_serial,timestamp):
        topology=self.topology_at(timestamp);matches=[x for x in topology['positions'] if x['physical_serial']==physical_serial];item=matches[0] if len(matches)==1 else None
        return {'physical_serial':physical_serial,'position_at_time':item['position'] if item else None,'resolved':item is not None,'position_history_id':topology['position_history_id'],'identity_epoch_id':item['identity_epoch_id'] if item else None}
    def serial_at(self,position,timestamp):
        if not isinstance(position,int) or not 1<=position<=6:raise ValueError('position must be between 1 and 6')
        return {**self.topology_at(timestamp)['positions'][position-1],'position_at_time':position}
    def epochs(self,physical_serial=None,timestamp_from=None,timestamp_to=None):
        with self._read() as trees:
            serials=[physical_serial] if physical_serial else [v for _,v in trees['serials'].page(limit=256)]
            if physical_serial is None and len(serials)==256 and trees['serials'].page(after=serialkey(serials[-1]),limit=1):raise ResearchQueryError('history_window_budget','Select a serial for this metadata set',503)
            result=[]
            for serial in serials:
                prefix=serialkey(serial);start=prefix+(timekey(timestamp_from) if timestamp_from else '');end=prefix+(timekey(timestamp_to)+'/~' if timestamp_to else '~')
                prior=trees['epochs'].floor(start) if timestamp_from else None
                selected=trees['epochs'].page(after=start,end=end,limit=256)
                if len(selected)==256 and trees['epochs'].page(after=selected[-1][0],end=end,limit=1):raise ResearchQueryError('history_window_budget','Identity page budget',503)
                if prior and prior[0].startswith(prefix):selected.insert(0,prior)
                result.extend(value for _,value in selected if value['valid_to'] is None or not timestamp_from or timekey(value['valid_to'])>=timekey(timestamp_from))
            return result
    def epoch_at(self,physical_serial,timestamp):
        target=normalize_utc_timestamp(timestamp,'timestamp');matches=[v for v in self.epochs(physical_serial,target,target) if v['valid_from']<=target and (v['valid_to'] is None or target<v['valid_to'])]
        return matches[-1] if matches else None
    def position_snapshot_at(self, timestamp=None):
        """One original snapshot via the prepared ordered position tree."""
        with self._read() as trees:
            value = trees['positions'].floor('~' if timestamp is None else timekey(timestamp)+'/~')
            return None if value is None else PositionSnapshot.from_dict(value[1])

    def position_validation_records(self, timestamp):
        """Latest, predecessor and same-time snapshots for the existing CAS rule."""
        with self._read() as trees:
            key = timekey(timestamp)
            same = trees['positions'].page(after=key, end=key+'/~', limit=256)
            if len(same)==256 and trees['positions'].page(after=same[-1][0], end=key+'/~', limit=1):
                raise ResearchQueryError('history_window_budget','Position same-time validation budget',503)
            selected = same
            for value in (trees['positions'].floor('~'), trees['positions'].floor(key)):
                if value is not None:
                    selected.append(value)
            unique = {v['position_history_id']:PositionSnapshot.from_dict(v) for _,v in selected}
            return sorted(unique.values(), key=lambda v:(v.effective_at,v.created_at,v.position_history_id))

    @staticmethod
    def _selected_rows(tree, *, prefix=''):
        # Complete explicitly selected output, cancellable between bounded pages.
        # No Raw access; output size (not unrelated source age) drives the cost.
        after=prefix
        while True:
            check_budget()
            page=tree.page(after=after,end=prefix+'~',limit=128)
            yield from page
            if len(page)<128:return
            after=page[-1][0]

    def position_snapshots(self):
        with self._read() as trees:
            return [PositionSnapshot.from_dict(value)
                    for _,value in self._selected_rows(trees['positions'])]

    def position_known_serials(self,position):
        with self._read() as trees:
            return sorted(value for _,value in self._selected_rows(
                trees['position_serials'],prefix=str(position)+'/'))

    def position_last_documented(self):
        with self._read() as trees:
            result={}
            for pos in range(1,7):
                prefix=str(pos)+'/'
                value=trees['position_nonempty'].floor(prefix+'~')
                result[str(pos)]=value[1] if value and value[0].startswith(prefix) else None
            return result

    def position_serial_options(self,position,timestamp):
        with self._read() as trees:
            target=timekey(timestamp)
            snapshot=trees['positions'].floor(target+'/~')
            effective=snapshot[1]['positions'][str(position)] if snapshot else None
            result=[]
            for key,serial in self._selected_rows(trees['position_serials'],prefix=str(position)+'/'):
                check_budget()
                if serial==effective:
                    relation='effective'
                else:
                    earlier=trees['position_starts'].floor(key+target+'/~')
                    later=trees['position_starts'].page(after=key+target+'/~',end=key+'~',limit=1)
                    before=bool(earlier and earlier[0].startswith(key))
                    relation='earlier_and_later' if before and later else 'earlier' if before else 'later'
                result.append({'serial':serial,'relationship':relation})
            order={'effective':0,'earlier':1,'earlier_and_later':2,'later':3}
            return sorted(result,key=lambda value:(order[value['relationship']],value['serial']))

    def position_window(self,start,end):
        first=datetime.fromtimestamp(start,timezone.utc).isoformat();last=datetime.fromtimestamp(end,timezone.utc).isoformat()
        with self._read() as trees:
            prior=trees['positions'].floor(timekey(first)+'/~')
            selected=trees['positions'].page(after=timekey(prior[1]['effective_at']) if prior else timekey(first),end=timekey(last),limit=256)
            if len(selected)==256 and trees['positions'].page(after=selected[-1][0],end=timekey(last),limit=1):raise ResearchQueryError('history_window_budget','Position page budget',503)
            return [v for _,v in selected]
    def config_window(self, start, end):
        """Original ConfigHistory order: predecessor plus selected changes."""
        with self._read() as trees:
            first, last = timekey(start), timekey(end) + '/~'
            prior = trees['config'].floor(first)
            rows, after = [], first
            while True:
                page = trees['config'].page(after=after, end=last, limit=min(256, 10001-len(rows)))
                rows.extend(page)
                if len(rows) > 10000:
                    raise ResearchQueryError('history_window_budget', 'Config window budget', 503)
                if len(page) < 256:
                    break
                after = page[-1][0]
            return ([prior[1]] if prior else []) + [value for _, value in rows]

    def technical_window(self, start, end):
        events = self.query(timestamp_from=start, timestamp_to=end, include_archived=True)
        return sorted((event for event in events if event.source == 'guardian_events'),
                      key=lambda event: int(event.projection_key.removeprefix('technical:')))

    def maintenance_request_retired(self,digest):
        with self._read() as trees:return trees['maintenance_retired'].get(digest) is True

    def maintenance_get(self, event_id):
        with self._read() as trees:
            value = trees['maintenance'].get(event_id)
            return None if value is None else MaintenanceEvent.from_dict(value['record'])

    def maintenance_history(self, event_id):
        with self._read() as trees:
            prefix = event_id+'/'
            rows = trees['maintenance_revisions'].page(after=prefix,end=prefix+'~',limit=256)
            if len(rows)==256 and trees['maintenance_revisions'].page(after=rows[-1][0],end=prefix+'~',limit=1):
                raise ResearchQueryError('history_window_budget','Maintenance revision page budget',503)
            return tuple(MaintenanceEvent.from_dict(value) for _,value in rows)

    def maintenance_request(self, request_id):
        with self._read() as trees:
            value = trees['maintenance_requests'].get(request_id)
            if value is None:
                return None
            current = trees['maintenance'].get(value['event_id'])
            return (MaintenanceEvent.from_dict(value['first']), MaintenanceEvent.from_dict(current['record']))

    def maintenance_list(self, *, include_archived=False, newest_first=True,
                         occurred_from=None, occurred_to=None):
        with self._read() as trees:
            first = '' if occurred_from is None else timekey(occurred_from)
            last = None if occurred_to is None else timekey(occurred_to)+'/~'
            rows = trees['maintenance_order'].page(after=first,end=last,limit=256)
            if len(rows)==256 and trees['maintenance_order'].page(after=rows[-1][0],end=last,limit=1):
                raise ResearchQueryError('history_window_budget','Maintenance selected window page budget',503)
            events = [MaintenanceEvent.from_dict(value) for _,value in rows]
            return sorted((event for event in events if include_archived or event.archived_at is None),
                key=lambda event:(event.occurred_at,event.maintenance_event_id), reverse=newest_first)

    def maintenance_window(self, start, end):
        events = self.query(timestamp_from=start, timestamp_to=end,
                            event_types=('maintenance',), include_archived=True)
        with self._read() as trees:
            records = []
            for event in events:
                value = trees['maintenance'].get(event.maintenance_event_id)
                if value is None or 'record' not in value:
                    raise ResearchQueryError('history_preparation_required',
                        'Metadata revision lacks original maintenance records; explicit migration required', 503)
                records.append(MaintenanceEvent.from_dict(value['record']))
            return sorted(records, key=lambda item: (item.occurred_at, item.maintenance_event_id), reverse=True)

    def query(self,*,timestamp_from,timestamp_to,event_types=(),include_archived=False,**kwargs):
        with self._read() as trees:
            rows=[];after=timekey(timestamp_from);end=timekey(timestamp_to)+'/~'
            while True:
                page=trees['timeline'].page(after=after,end=end,limit=min(256,10001-len(rows)))
                rows.extend(page)
                if len(rows)>10000:raise ResearchQueryError('history_window_budget','Timeline page budget',503)
                if not page or len(page)<256:break
                after=page[-1][0]
            result=[]
            for _,value in rows:
                item=TimelineEvent(**value)
                if item.event_type=='maintenance' and item.module_serial is None and item.module_number is not None:
                    topology=self._topology(trees,item.timestamp);item=replace(item,module_serial=topology['positions'][item.module_number-1]['physical_serial'])
                if event_types and item.event_type not in event_types:continue
                if not include_archived and item.metadata.get('archived_at'):continue
                if kwargs.get('module_number') is not None and item.module_number!=kwargs['module_number']:continue
                if kwargs.get('cell_number') is not None and item.cell_number!=kwargs['cell_number']:continue
                if kwargs.get('category') is not None and (item.event_type!='maintenance' or item.metadata.get('category')!=kwargs['category']):continue
                result.append(item)
            return sorted(result,key=lambda e:(e.timestamp,e.event_type,e.projection_key),reverse=True)
