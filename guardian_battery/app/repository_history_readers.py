"""Local adapters for real HistoryAPI/DisplayHistoryReader/CanonicalPhaseReader.

No indexing or rebuild in a read. Published data is explicitly as-of its pinned
bundle; current raw coverage is never inferred from an available publication.
"""
from contextlib import contextmanager,ExitStack
from datetime import datetime,timedelta,timezone
import json
import math
import time
from research_budget import check_budget,ResearchQueryError
from projection_repository import Conflict,Unavailable
from publication_window_index import epoch
from history_series_contract import normalize_series


def days(start,end):
    first=datetime.fromtimestamp(epoch(start),timezone.utc).date()
    last=datetime.fromtimestamp(epoch(end),timezone.utc).date()
    if last<first:raise ValueError('invalid window')
    # Explicit bounded response contract, no silently truncated long request.
    if (last-first).days>366:raise ResearchQueryError('range_too_large','Published read window exceeds 367 days',400)
    return [(first+timedelta(days=i)).isoformat() for i in range((last-first).days+1)]


class PublishedHistoryReaders:
    def __init__(self,index,canonical,display,*,streams,full_series,full_hycube=None,selection=None):
        self.index,self.canonical,self.display=index,canonical,display
        self.repository=index.repository
        self.streams=tuple(streams)
        if not 1<=len(self.streams)<=64:raise ValueError('explicit stream selection required')
        self.full_series,self.full_hycube=full_series,full_hycube
        self.selection=dict(selection or {})

    @contextmanager
    def _published(self,kind,selected_days,physical_serial=None):
        captured=[]
        with ExitStack() as leases:
            # Completed read frontiers are selected in one DB snapshot. A newer
            # derivation head cannot hide the previous fully indexed publication.
            with self.repository._connection() as db:
                db.execute('BEGIN')
                for day in selected_days:
                    products=[(None,self.display.head(day),self.display.product(day))] if kind=='display' else [
                        (stream,self.canonical._head(stream,day),self.canonical._product(stream,day)) for stream in self.streams
                        if (physical_serial is None or self.canonical._stream(stream)['serial']==physical_serial)
                        and day>=self.canonical._stream(stream)['origin']]
                    for stream,head,product in products:
                        selected=self.selection.get((kind,stream,day))
                        row=db.execute('SELECT bundle FROM window_index_ready WHERE product=? AND partition_key=?',
                                       (product,day)).fetchone() if selected is None else None
                        bundle=selected if selected is not None else (row[0] if row else None)
                        if bundle is None:raise Unavailable('published read frontier absent; explicit preparation required')
                        captured.append((day,stream,bundle,product))
            for day,stream,bundle,product in captured:
                check_budget();leases.enter_context(self.repository.lease(bundle,owner='local-history-request'))
                state=self.index.state(bundle,product,day)
                if not state['complete']:raise Unavailable('published window index incomplete')
                metadata=json.loads(state['metadata'])
                validity=(self.canonical.publication_validity(stream,day,bundle,metadata) if kind=='canonical' else
                          self.display.read_page(day,bundle=bundle,limit=1)['validity'])
                if validity['stale_dependencies']:raise Unavailable('publication dependencies changed; explicit preparation required')
            yield captured

    def query_bundles(self,*,requests,timestamp_from,timestamp_to,module_number=None,module_numbers=None,
                      max_points=6000,include_all_module_soc=False,timing=None,include_samples=True):
        from display_history_reader import select_resolution,expand_bucket
        modules=tuple(module_numbers or (module_number,))
        resolution=select_resolution(timestamp_from=timestamp_from,timestamp_to=timestamp_to,
            requests=requests,module_numbers=modules,max_points=max_points,include_all_module_soc=include_all_module_soc)
        if resolution is None or include_samples:
            return self.full_series.query_bundles(requests=requests,timestamp_from=timestamp_from,timestamp_to=timestamp_to,
                module_number=module_number,module_numbers=modules,max_points=max_points,
                include_all_module_soc=include_all_module_soc,timing=timing,include_samples=include_samples)
        started=time.perf_counter();series=[];soc=[];total_bytes=decoded=sql_steps=0;vector=[]
        try:
            with self._published('display',days(timestamp_from,timestamp_to)) as captured:
                for request in requests:
                    points=[];raw=0
                    cells=request.get('cell_numbers') or ((request['cell_number'],) if request.get('cell_number') is not None else ())
                    for day,stream,bundle,product in captured:
                        result=self.index.query(bundle,product,day,timestamp_from,timestamp_to,modules=modules,
                            metric=request['metric'],cells=cells,resolution=resolution)
                        total_bytes+=result['bytes'];decoded+=result['decodes'];sql_steps+=result['sql_vm_steps']
                        vector.append({'day':day,'bundle':bundle,'product':product,'status':result['metadata']['status'],'input_hash':result['metadata'].get('input_hash'),'coverage':result['metadata'].get('coverage'),'processed_records':result['metadata'].get('processed_records'),'canonical_dependencies':result['metadata'].get('canonical_dependencies')})
                        for _,bucket in result['rows']:
                            raw+=bucket.get('sample_count',0)
                            for point in expand_bucket(bucket):
                                if request.get('cell_number') is not None:point.pop('cell_number',None)
                                point['publication_bundle']=bundle;point['source']='pylontech'
                                points.append(point)
                    item=normalize_series({'metric':request['metric'],'cell_number':request.get('cell_number'),
                        'cell_numbers':list(cells),'points':points,'raw_points':raw},timestamp_from,timestamp_to)
                    series.append(item)
                    if request['metric']=='soc' and include_all_module_soc:
                        for module in modules:
                            soc.append({'metric':'soc','label':f'Modul {module} SOC','unit':'%',
                                'source':'pylontech','module_number':module,
                                'points':[p for p in item['points'] if p.get('module_number')==module]})
        except (Conflict,Unavailable) as exc:
            raise ResearchQueryError('history_preparation_required',str(exc),503) from exc
        state={'basis':'published_confirmed_inputs','latest_raw_coverage':'not_asserted','publications':vector}
        if timing:timing.counts(display_selected_bytes=total_bytes,display_selected_decodes=decoded,display_sql_vm_steps=sql_steps)
        return {'series':series,'soc_module_series':soc,'samples':[],
                'raw_records':0,'file_count':0,'read_seconds':time.perf_counter()-started,
                'downsample_seconds':0,'cache_hit':False,'data_state':state,
                'display_observability':{'history_source_mode':'pinned_repository_publication',
                    'display_resolution':resolution,'display_selected_bytes':total_bytes,
                    'display_selected_decodes':decoded,'display_sql_vm_steps':sql_steps,'data_state':state}}

    def query_hycube(self,**kwargs):
        if self.full_hycube is None:return None
        return self.full_hycube.query(**kwargs)

    def query(self,timestamp_from,timestamp_to,*,physical_serial=None,module_number=None,timing=None):
        from canonical_phase import clip_interval,SEMANTICS_VERSION
        from canonical_checkpoint_storage import unpack
        selected_days=days(timestamp_from,timestamp_to);items={'diagnostic':[],'visual':[],'endpoint':[]}
        identities=set();byte_count=0;vector=[];sql_steps=0
        try:
            with self._published('canonical',selected_days,physical_serial) as captured:
                for day,stream,bundle,product in captured:
                    definition=self.canonical._stream(stream)
                    if physical_serial is not None and definition['serial']!=physical_serial:continue
                    result=self.index.query(bundle,product,day,timestamp_from,timestamp_to,
                        modules=() if module_number is None else (module_number,))
                    identities.add(definition['identity']);byte_count+=result['bytes'];sql_steps+=result['sql_vm_steps']
                    vector.append({'day':day,'stream':stream,'bundle':bundle,'status':result['metadata']['status'],'input_hash':result['metadata'].get('input_hash'),'coverage':result['metadata'].get('coverage'),'epoch_vector':result['metadata'].get('epoch_vector')})
                    with self.repository._connection() as db:
                        for kind,packed in result['rows']:
                            check_budget();item=unpack(db,packed)
                            if kind=='endpoint':
                                if epoch(timestamp_from)<=epoch(item['timestamp'])<=epoch(timestamp_to):items[kind].append(dict(item,identity_epoch_id=definition['identity']))
                            else:
                                clipped=clip_interval(item,timestamp_from,timestamp_to)
                                if clipped:
                                    # Original reader coalesces count timestamps as a set.
                                    # Only the requested range is inspected, never the full
                                    # accumulated phase; duplicate records stay in raw evidence.
                                    from bisect import bisect_left,bisect_right
                                    timestamps=item.get('count_timestamps',())
                                    left=bisect_left(timestamps,epoch(timestamp_from));right=bisect_right(timestamps,epoch(timestamp_to))
                                    if right-left>2048*256:
                                        raise ResearchQueryError('history_window_budget','Canonical visible-count window exceeds budget',503)
                                    previous=None;count=0
                                    for position in range(left,right):
                                        check_budget();stamp=timestamps[position]
                                        if stamp!=previous:count+=1
                                        previous=stamp
                                    clipped['sample_count']=count
                                if clipped:items[kind].append(dict(clipped,identity_epoch_id=definition['identity']))
            def merge(values):
                groups={}
                for item in values:
                    key=tuple(item.get(k) for k in ('identity_epoch_id','canonical_from','phase','config_id','module_position'))
                    groups.setdefault(key,[]).append(item)
                result=[]
                for group in groups.values():
                    selected=max(group,key=lambda x:(x.get('canonical_to') is not None,x['canonical_sample_count']))
                    result.append(dict(selected,sample_count=sum(x['sample_count'] for x in group),
                                       **{'from':min(x['from'] for x in group),'to':max(x['to'] for x in group)}))
                # Same canonical A-B-A supersession as the existing reader,
                # confined to the physical identity (never merge replacements).
                reduced=[]
                for item in result:
                    check_budget()
                    begin=epoch(item['canonical_from'])
                    finish=epoch(item['canonical_to']) if item.get('canonical_to') else math.inf
                    superseded=any(other is not item and other['identity_epoch_id']==item['identity_epoch_id']
                        and other.get('phase')==item.get('phase')
                        and epoch(other['canonical_from'])<=begin
                        and (epoch(other['canonical_to']) if other.get('canonical_to') else math.inf)>=finish
                        and other['canonical_sample_count']>item['canonical_sample_count'] for other in result)
                    if not superseded:reduced.append(item)
                return sorted(reduced,key=lambda x:(x['from'],x.get('module_position') or 0,x['identity_epoch_id']))
            if not identities:return {'available':False,'reason':'identity_mismatch','days':selected_days}
            return {'available':True,'reason':None,'days':selected_days,'bytes':byte_count,
                'identity_epochs':sorted(identities),'diagnostic_intervals':merge(items['diagnostic']),
                'visual_intervals':merge(items['visual']),'relative_endpoints':sorted(items['endpoint'],key=lambda x:x['timestamp']),
                'semantics_version':SEMANTICS_VERSION,'source_mode':'canonical_phase_v2',
                'data_state':{'basis':'pinned_publications','latest_raw_coverage':'not_asserted','publications':vector},'sql_vm_steps':sql_steps}
        except (Conflict,Unavailable) as exc:
            return {'available':False,'reason':str(exc),'days':selected_days,'source_mode':'preparation_required'}
