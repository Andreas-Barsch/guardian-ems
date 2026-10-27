"""Explicit confirmed-source window adapter; no discovery/indexing in reads."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import hashlib
import json
from cell_source_resolver import CellSourceResolver,WindowBudgetExceeded
from cell_window_adapter import utc_seconds
from source_catalog import MAX_BYTES, MAX_PAGE
from research_budget import check_budget, ResearchQueryError
from projection_repository import Conflict, Unavailable


class IndexedHistoryWindow:
    def __init__(self, catalog, *, max_pages=2048, prefix_reader=None):
        if not isinstance(max_pages,int) or not 1 <= max_pages <= 2048:
            raise ValueError('window page budget must be 1..2048')
        self.resolver=CellSourceResolver(catalog,prefix_reader=prefix_reader)
        self.max_pages=max_pages
        self._metrics=ContextVar('indexed_history_metrics',default=None)

    @property
    def metrics(self):
        return dict(self._metrics.get() or {})

    @contextmanager
    def read(self, start, end, *, modules=(), predecessor_identity=None, prepared_snapshot=None):
        first,last=utc_seconds(start),utc_seconds(end)
        if last<first:raise ValueError('invalid window')
        metrics={'raw_bytes':0,'decodes':0,'files_opened':0,'sql_vm_steps':0,
                 'pages':0,'first_observation':None,'last_observation':None,'source_mode':'confirmed_indexed_window',
                 'coverage_basis':'frozen indexed source prefixes; undiscovered sources unknown'}
        self._metrics.set(metrics)
        snapshot=None
        try:
            check_budget()
            if prepared_snapshot is None:
                opened=self.resolver.open_window(first,math.nextafter(last,math.inf),modules=modules,
                                                  predecessor_identity=predecessor_identity)
                snapshot,prior=(opened,None) if predecessor_identity is None else opened
            else:
                if predecessor_identity is not None:raise ValueError('prepared window cannot add predecessor')
                snapshot,prior=prepared_snapshot,None
                with self.resolver.catalog.connection() as db:
                    state=self.resolver._snapshot(db,snapshot)
                    if (state['start'],state['end'],json.loads(state['modules']))!=(first,math.nextafter(last,math.inf),list(modules)):
                        raise Conflict('prepared window bounds/selection differ')
            metrics['predecessor_decodes']=0
            metrics['snapshot_id']=snapshot
            with self.resolver.catalog.connection() as db:
                inputs=[tuple(r) for r in db.execute(
                    'SELECT source,revision,receipt,prefix_end FROM window_source WHERE snapshot=? ORDER BY source', (snapshot,))]
            metrics['confirmed_prefixes']=[]
            metrics['proof_metadata_bytes']=0
            metrics['latest_raw_coverage']='not_asserted; frozen indexed prefixes only'
            metrics['source_fingerprint']=hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
            def rows():
                after=None
                for _ in range(self.max_pages):
                    check_budget()
                    plan=self.resolver.plan_snapshot(snapshot,after=after,limit=MAX_PAGE)
                    if after is None and prior is not None and prior.timestamp < first:
                        plan['refs']=[prior]+[ref for ref in plan['refs'] if ref.key!=prior.key]
                    metrics['sql_vm_steps']+=plan['sql_vm_steps']
                    metrics['pending_sources']=plan['pending_sources'];metrics['invalid_records']=plan['invalid_records']
                    metrics['generation']=plan['generation'];metrics['pages']+=1
                    if after is None and not plan['refs'] and plan['pending_sources']:
                        raise ResearchQueryError('history_preparation_required','No prepared source in the requested window; registered sources still pending',503)
                    refs=[];size=0
                    for ref in plan['refs']:
                        if len(refs)>=MAX_PAGE or size+ref.end-ref.start>MAX_BYTES:break
                        refs.append(ref);size+=ref.end-ref.start
                    if plan['refs'] and not refs:
                        raise ResearchQueryError('history_window_budget','One record exceeds the read page budget; explicit preparation required',503)
                    read=self.resolver.read_refs(refs)
                    metrics['predecessor_decodes']+=sum(ref is prior for ref in refs)
                    for key in ('raw_bytes','decodes','files_opened'):metrics[key]+=read[key]
                    metrics['proof_metadata_bytes']+=read.get('proof_metadata_bytes',0)
                    prefixes={item['source']:item for item in metrics['confirmed_prefixes']}
                    prefixes.update({item['source']:item for item in read.get('confirmed_prefixes',())})
                    metrics['confirmed_prefixes']=list(prefixes.values())
                    for ref,item in zip(refs,read['records']):
                        check_budget()
                        if metrics['first_observation'] is None:metrics['first_observation']=ref.timestamp
                        metrics['last_observation']=ref.timestamp
                        yield item['record'],ref
                    if len(refs)==len(plan['refs']) and plan['next'] is None:return
                    after=refs[-1].key
                raise ResearchQueryError('history_window_budget','Requested window exceeds the page budget; no truncated success',503)
            yield rows()
        except (Conflict,Unavailable,WindowBudgetExceeded,OSError) as exc:
            raise ResearchQueryError('history_preparation_required',str(exc),503) from exc
        finally:
            if snapshot is not None and prepared_snapshot is None:self.resolver.release_window(snapshot)
