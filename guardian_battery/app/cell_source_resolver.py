"""Isolated indexed UTC window planning; no raw discovery/rebuild in plan_window."""
from dataclasses import dataclass
from pathlib import Path
import json
import math
import sqlite3
import uuid
import os

from projection_repository import Conflict
from source_catalog import MAX_BYTES, MAX_PAGE, digest, signature
from research_budget import check_budget


class WindowBudgetExceeded(RuntimeError):
    """Explicitly incomplete plan; caller must not treat it as empty history."""


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    revision: str
    path: str
    storage_convention: str
    start: int
    end: int
    timestamp: float
    module: int | None
    identity_context: str
    source_context: str
    record_hash: str
    file_signature: tuple
    key: tuple
    snapshot_id: str | None = None
    receipt: str | None = None
    prefix_end: int | None = None


class CellSourceResolver:
    def __init__(self,catalog,*,prefix_reader=None):
        self.catalog=catalog
        self.prefix_reader=prefix_reader

    def _query(self,start,end,modules,after,limit):
        if not math.isfinite(start) or not math.isfinite(end) or end<=start or end-start>32*86400:
            raise ValueError('UTC window must be positive and at most 32 days')
        if not 1<=limit<=MAX_PAGE or len(modules)>16:
            raise ValueError('invalid window page/selection')
        query=('SELECT e.*,s.path,s.namespace,s.context,s.dev,s.ino,s.size,s.mtime '
               'FROM entry e INDEXED BY entry_window '
               'JOIN active a ON a.source=e.source AND a.revision=e.revision '
               'JOIN source s ON s.source=e.source AND s.revision=e.revision '
               'WHERE (e.day,e.stamp)>=(?,?) AND (e.day,e.stamp)<(?,?)')
        params=[math.floor(start/86400),start,math.floor(end/86400),end]
        if modules:
            query+=' AND e.module IN ('+','.join('?' for _ in modules)+')';params.extend(modules)
        if after is not None:
            query+=' AND (e.day,e.stamp,e.source,e.revision,e.offset)>(?,?,?,?,?)';params.extend(after)
        query+=' ORDER BY e.day,e.stamp,e.source,e.revision,e.offset LIMIT ?';params.append(limit+1)
        return query,params

    def plan_window(self,start,end,*,modules=(),after=None,expected_generation=None,limit=MAX_PAGE,sql_step_limit=50000):
        """Half-open UTC seconds. Pagination fails if catalog snapshot changes.

        Never infers day membership from file names, never deduplicates records.
        Catalog pending/error counts do not certify undiscovered source coverage.
        """
        if not 1<=sql_step_limit<=100000:
            raise ValueError('invalid SQL planning budget')
        query,params=self._query(start,end,modules,after,limit)
        with self.catalog.connection() as db:
            db.execute('BEGIN')
            state=dict(db.execute('SELECT * FROM state WHERE id=1').fetchone())
            if after is not None and expected_generation is None:
                raise ValueError('pagination requires catalog generation')
            if expected_generation is not None and expected_generation!=state['generation']:
                raise Conflict('catalog generation changed; restart explicit query')
            steps=0
            def budget():
                nonlocal steps
                steps+=1
                return int(steps>=sql_step_limit)
            db.set_progress_handler(budget,1)
            try:
                rows=db.execute(query,params).fetchall()
            except sqlite3.OperationalError as error:
                if steps>=sql_step_limit:
                    raise WindowBudgetExceeded(f'window SQL budget exhausted ({steps} steps); no rebuild') from error
                raise
            finally:
                db.set_progress_handler(None,0)
            refs=[SourceRef(r['source'],r['revision'],r['path'],r['namespace'],r['offset'],r['end_offset'],
                  r['stamp'],r['module'],r['identity'],r['context'],r['hash'],
                  (r['dev'],r['ino'],r['size'],r['mtime']),
                  (r['day'],r['stamp'],r['source'],r['revision'],r['offset'])) for r in rows[:limit]]
            return {'refs':refs,'generation':state['generation'],
                    'next':refs[-1].key if len(rows)>limit else None,
                    'coverage':'indexed_records_only; undiscovered_sources_unknown',
                    'pending_sources':state['pending'],'invalid_records':state['errors'],
                    'files_opened':0,'raw_bytes':0,'decodes':0,'sql_rows_materialized':len(rows),'sql_vm_steps':steps}

    def explain_window(self,start,end,*,modules=(),after=None,limit=MAX_PAGE):
        query,params=self._query(start,end,modules,after,limit)
        with self.catalog.connection() as db:
            return [r[3] for r in db.execute('EXPLAIN QUERY PLAN '+query,params)]

    @staticmethod
    def _budgeted(db,query,params,limit):
        if not 1<=limit<=100000:raise ValueError('invalid SQL planning budget')
        steps=0; failure=[]
        def budget():
            nonlocal steps
            steps+=1
            if steps % 1000 == 0:
                try: check_budget()
                except BaseException as exc: failure.append(exc);return 1
            return int(steps>=limit)
        db.set_progress_handler(budget,1)
        try:
            return db.execute(query,params).fetchall(),lambda:steps
        except sqlite3.OperationalError as error:
            if failure: raise failure[0]
            if steps>=limit:raise WindowBudgetExceeded(f'window SQL budget exhausted ({steps} steps); no rebuild') from error
            raise
        finally:db.set_progress_handler(None,0)

    def open_window(self,start,end,*,modules=(),sql_step_limit=50000,predecessor_identity=None):
        """Freeze <=256 matching source prefixes, not every matching raw record.

        Index rows are append-only. New sources and later indexed offsets cannot
        enter this query. No DB/read transaction is held across caller pages.
        """
        query,params=self._query(start,end,modules,None,MAX_PAGE)
        query='SELECT DISTINCT e.source,e.revision,s.receipt,s.cursor '+query[query.index('FROM entry'):query.index(' ORDER BY')]
        query+=' LIMIT ?'
        snapshot=uuid.uuid4().hex
        with self.catalog.write() as db:
            rows,_=self._budgeted(db,query,params,sql_step_limit)
            prior = None
            if predecessor_identity is not None:
                prior = self._predecessor_row(db,start,predecessor_identity,sql_step_limit)
                if prior is not None and not any(r['source']==prior['source'] for r in rows):
                    rows.append(prior)
            if len(rows)>MAX_PAGE:raise WindowBudgetExceeded('too many selected sources for one frozen window')
            state=db.execute('SELECT * FROM state WHERE id=1').fetchone()
            db.execute('INSERT INTO window_snapshot VALUES(?,?,?,?,?,?,?)',
                       (snapshot,start,end,json.dumps(list(modules)),state['generation'],state['pending'],state['errors']))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='window_owner'").fetchone():
                db.execute('INSERT INTO window_owner(snapshot,owner,proof) VALUES(?,?,?)',(snapshot,'request:'+snapshot,'created by explicit local window request'))
            db.executemany('INSERT INTO window_source VALUES(?,?,?,?,?)',
                           [(snapshot,r['source'],r['revision'],r['receipt'],r['cursor']) for r in rows])
        if predecessor_identity is not None:
            return snapshot, None if prior is None else self._row_ref(prior,snapshot)
        return snapshot

    @staticmethod
    def _snapshot(db,snapshot):
        state=db.execute('SELECT * FROM window_snapshot WHERE id=?',(snapshot,)).fetchone()
        if state is None:raise Conflict('window snapshot absent or released')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='window_owner'").fetchone():
            owner=db.execute('SELECT status FROM window_owner WHERE snapshot=?',(snapshot,)).fetchone()
            if owner is not None and owner[0]!='active':raise Conflict('window snapshot retiring')
        changed=db.execute('SELECT 1 FROM window_source w LEFT JOIN active a ON a.source=w.source '
                           'WHERE w.snapshot=? AND (a.revision IS NULL OR a.revision!=w.revision) LIMIT 1',(snapshot,)).fetchone()
        if changed:raise Conflict('snapshot source revision changed')
        return state

    def plan_snapshot(self,snapshot,*,after=None,limit=MAX_PAGE,sql_step_limit=50000):
        with self.catalog.connection() as db:
            db.execute('BEGIN')
            state=self._snapshot(db,snapshot)
            query,params=self._query(state['start'],state['end'],json.loads(state['modules']),after,limit)
            query=query.replace('SELECT e.*,','SELECT e.*,w.receipt AS frozen_receipt,w.prefix_end AS frozen_end,')
            query=query.replace('WHERE (e.day,e.stamp)', 'JOIN window_source w ON w.source=e.source AND w.revision=e.revision '
                                'WHERE w.snapshot=? AND e.end_offset<=w.prefix_end AND (e.day,e.stamp)')
            rows,steps=self._budgeted(db,query,[snapshot,*params],sql_step_limit)
            refs=[SourceRef(r['source'],r['revision'],r['path'],r['namespace'],r['offset'],r['end_offset'],
                  r['stamp'],r['module'],r['identity'],r['context'],r['hash'],
                  (r['dev'],r['ino'],r['size'],r['mtime']),
                  (r['day'],r['stamp'],r['source'],r['revision'],r['offset']),
                  snapshot,r['frozen_receipt'],r['frozen_end']) for r in rows[:limit]]
            return {'snapshot':snapshot,'refs':refs,'next':refs[-1].key if len(rows)>limit else None,
                    'generation':state['generation'],'pending_sources':state['pending'],'invalid_records':state['errors'],
                    'coverage':'frozen_indexed_prefixes; undiscovered_sources_unknown',
                    'files_opened':0,'raw_bytes':0,'decodes':0,'sql_rows_materialized':len(rows),'sql_vm_steps':steps()}

    def _predecessor_row(self,db,timestamp,identity,sql_step_limit):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='entry_identity_stamp'").fetchone():
            from projection_repository import Unavailable
            raise Unavailable('predecessor index requires explicit preparation')
        rows,_ = self._budgeted(db,
            'SELECT e.*,s.path,s.namespace,s.context,s.dev,s.ino,s.size,s.mtime,s.receipt,s.cursor '
            'FROM entry e INDEXED BY entry_identity_stamp '
            'JOIN active a ON a.source=e.source AND a.revision=e.revision '
            'JOIN source s ON s.source=e.source AND s.revision=e.revision '
            'WHERE e.identity=? AND e.stamp<=? '
            'ORDER BY e.stamp DESC,e.source DESC,e.revision DESC,e.offset DESC LIMIT 1',
            [identity,timestamp],sql_step_limit)
        return rows[0] if rows else None

    @staticmethod
    def _row_ref(r,snapshot):
        return SourceRef(r['source'],r['revision'],r['path'],r['namespace'],r['offset'],r['end_offset'],
            r['stamp'],r['module'],r['identity'],r['context'],r['hash'],
            (r['dev'],r['ino'],r['size'],r['mtime']),
            (r['day'],r['stamp'],r['source'],r['revision'],r['offset']),snapshot,r['receipt'],r['cursor'])

    def open_predecessor(self, timestamp, identity, *, sql_step_limit=50000):
        """Pin exactly one indexed predecessor; no older Raw scan or discovery."""
        if not math.isfinite(timestamp):raise ValueError('finite predecessor timestamp required')
        with self.catalog.write() as db:
            r=self._predecessor_row(db,timestamp,identity,sql_step_limit)
            if r is None:return None
            state=db.execute('SELECT * FROM state WHERE id=1').fetchone()
            snapshot=uuid.uuid4().hex
            db.execute('INSERT INTO window_snapshot VALUES(?,?,?,?,?,?,?)',
                (snapshot,r['stamp'],math.nextafter(r['stamp'],math.inf),'[]',state['generation'],state['pending'],state['errors']))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='window_owner'").fetchone():
                db.execute('INSERT INTO window_owner(snapshot,owner,proof) VALUES(?,?,?)',
                    (snapshot,'request:'+snapshot,'explicit indexed predecessor'))
            db.execute('INSERT INTO window_source VALUES(?,?,?,?,?)',
                (snapshot,r['source'],r['revision'],r['receipt'],r['cursor']))
            return self._row_ref(r,snapshot)

    def release_window(self,snapshot):
        with self.catalog.write() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='window_owner'").fetchone():
                db.execute('DELETE FROM window_owner WHERE snapshot=?',(snapshot,))
            db.execute('DELETE FROM window_snapshot WHERE id=?',(snapshot,))

    def _validate_ref(self,ref,verified=None,validation_cache=None):
        if ref.start<0 or ref.end<=ref.start:raise Conflict('invalid source range')
        if ref.snapshot_id is not None and ref.end>ref.prefix_end:raise Conflict('range outside frozen source prefix')
        key=(ref.snapshot_id,ref.source_id,ref.revision,ref.receipt,ref.prefix_end,ref.path,ref.file_signature)
        if validation_cache is not None and key in validation_cache:return
        if ref.snapshot_id is None:
            if signature(ref.path)!=ref.file_signature:raise Conflict('planned source changed')
            if validation_cache is not None:validation_cache.add(key)
            return
        with self.catalog.connection() as db:
            db.execute('BEGIN');self._snapshot(db,ref.snapshot_id)
            member=db.execute('SELECT * FROM window_source WHERE snapshot=? AND source=? AND revision=?',
                              (ref.snapshot_id,ref.source_id,ref.revision)).fetchone()
            if member is None or member['receipt']!=ref.receipt or member['prefix_end']!=ref.prefix_end or ref.end>ref.prefix_end:
                raise Conflict('range outside frozen source prefix')
            source=self.catalog._source(db,ref.source_id,ref.revision)
            receipts=db.execute('SELECT old.seq,old.frontier,new.seq AS latest FROM receipt old JOIN receipt new '
                                'ON new.source=old.source AND new.revision=old.revision '
                                'WHERE old.source=? AND old.revision=? AND old.receipt=? AND new.receipt=?',
                                (ref.source_id,ref.revision,ref.receipt,source['receipt'])).fetchone()
            if (receipts is None or receipts['seq']>receipts['latest'] or ref.prefix_end>receipts['frontier']
                    or source['path']!=ref.path or (source['dev'],source['ino'])!=ref.file_signature[:2]):
                raise Conflict('unproven append chain for frozen prefix')
            proof_key=(ref.source_id,ref.revision,ref.receipt,ref.prefix_end)
            if self.prefix_reader is None:
                self.catalog._verify(source)
            elif verified is None or proof_key not in verified:
                state=self.prefix_reader.verify(db,source,ref)
                if verified is not None:verified[proof_key]=state
            if validation_cache is not None:validation_cache.add(key)

    def read_refs(self,refs,*,max_bytes=MAX_BYTES):
        """One FD per selected physical path; merge only touching/overlapping ranges.

        No gap reads or whole-prefix hashes. Hash every original record and return
        the original plan order, including duplicates/conflicts and all channels.
        Frozen refs accept later registered appends; legacy refs retain exact-stat
        semantics. Real writer durability/authenticity remains an external gate.
        """
        if len(refs)>MAX_PAGE or not 0<max_bytes<=MAX_BYTES or sum(r.end-r.start for r in refs)>max_bytes:
            raise ValueError('record read budget exceeded')
        groups={};before={};validated_before=set()
        for index,ref in enumerate(refs):
            check_budget()
            self._validate_ref(ref,before,validated_before)
            groups.setdefault((ref.path,*ref.file_signature[:2]),[]).append((index,ref))
        output=[None]*len(refs);read_bytes=files=calls=0
        for (path,dev,ino),items in groups.items():
            spans=[]
            for _,ref in sorted(items,key=lambda pair:(pair[1].start,pair[1].end)):
                if spans and ref.start<=spans[-1][1]:spans[-1][1]=max(spans[-1][1],ref.end)
                else:spans.append([ref.start,ref.end])
            chunks=[]
            with Path(path).open('rb') as handle:
                files+=1;st=os.fstat(handle.fileno())
                if (st.st_dev,st.st_ino)!=(dev,ino):raise Conflict('selected file replaced')
                for start,end in spans:
                    check_budget()
                    handle.seek(start);raw=handle.read(end-start);calls+=1;read_bytes+=len(raw)
                    if len(raw)!=end-start:raise Conflict('selected file truncated')
                    chunks.append((start,end,raw))
                for index,ref in items:
                    check_budget()
                    raw=next(chunk[ref.start-start:ref.end-start] for start,end,chunk in chunks
                             if start<=ref.start and end>=ref.end)
                    if digest(raw)!=ref.record_hash:raise Conflict('planned record hash mismatch; new revision required')
                    output[index]={'source_id':ref.source_id,'revision':ref.revision,'byte_offset':ref.start,
                                   'record_hash':ref.record_hash,'record':json.loads(raw)}
        after={};validated_after=set()
        for ref in refs:self._validate_ref(ref,after,validated_after)
        return {'records':output,'files_opened':files,'selected_files':len(groups),'read_calls':calls,
                'raw_bytes':read_bytes,'decodes':len(output),'confirmed_prefixes':list(after.values()),
                'proof_metadata_bytes':sum(x['proof_metadata_bytes'] for x in [*before.values(),*after.values()])}
