"""Use existing metric/downsampling rules on explicitly indexed raw windows."""
from datetime import datetime, timezone
import json
import math
import time
from collections import defaultdict

from research_budget import check_budget, ResearchQueryError
from history_series_contract import normalize_series


def _iso(stamp):return datetime.fromtimestamp(stamp,timezone.utc).isoformat()


def project_cells(window, normalized, *, timestamp_from, timestamp_to, selected_modules,
                  primary_module,max_points,include_all_module_soc,include_samples,
                  timing=None,position_window=None):
    from history_series import CellHistorySeries,_ExtremaCollector
    from stack_soc import STACK_SOC_METRICS,project_stack_soc
    from position_history import PositionSnapshot
    start,end=(datetime.fromisoformat(v).timestamp() for v in (timestamp_from,timestamp_to))
    collectors=[{} for _ in normalized];counts=[0 for _ in normalized]
    soc={};samples=[];stack_records=[];seen_records=0;last_source={}
    needs_stack=any(metric in STACK_SOC_METRICS for metric,_,_ in normalized)
    if needs_stack and position_window is None:
        raise ResearchQueryError('history_preparation_required','Indexed position context required for stack metrics',503)
    limits=[max(4,max_points//(len(selected_modules)*(len(cells) if cells else 15 if metric in ('cell_voltage','cell_temperature') and cell is None else 1))) for metric,cell,cells in normalized]
    def add(groups,key,point,limit,ref,changes=False):
        # Revision boundaries are evidence boundaries, never silently bridged.
        provenance=(ref.source_id,ref.revision)
        previous=last_source.get((id(groups),key))
        if previous is not None and previous!=provenance:point['_segment_start']=True
        last_source[(id(groups),key)]=provenance
        point.update(source_id=ref.source_id,source_revision=ref.revision,
                     source_offset=ref.start,source_record_hash=ref.record_hash)
        groups.setdefault(key,_ExtremaCollector(limit,start,end,preserve_changes=changes)).add(point)
    started=time.perf_counter()
    with window.read(start,end,modules=() if needs_stack else selected_modules) as rows:
        for record,ref in rows:
            check_budget()
            if record.get('schema_version')!=1:raise ResearchQueryError('source_unavailable','Cell schema mismatch',503)
            epoch=float(record['timestamp']);module=int(record['module']);timestamp=_iso(epoch)
            if needs_stack:stack_records.append(record)
            if module not in selected_modules:continue
            seen_records+=1
            if include_samples and module==primary_module:
                samples.append({'timestamp':timestamp,'current_a':float(record['current_a']),
                    'soc_percent':float(record['soc_percent']),'voltages_mv':[float(v) for v in record['voltages_mv']],
                    'module_serial':record.get('module_serial')})
            if include_all_module_soc:
                point=CellHistorySeries._points(record,'soc',None,timestamp,epoch)[0]
                point.update(module_number=module,source='pylontech')
                add(soc,module,point,max(4,max_points//len(selected_modules)),ref,True)
            for i,(metric,cell,cells) in enumerate(normalized):
                if metric in STACK_SOC_METRICS:continue
                if metric=='soc' and include_all_module_soc:counts[i]+=1;continue
                for point in CellHistorySeries._points(record,metric,cell,timestamp,epoch,cells):
                    if len(selected_modules)>1:point['module_number']=module
                    add(collectors[i],(module,point.get('cell_number',cell or 0)),point,limits[i],ref,metric=='soc')
                    counts[i]+=1
    if needs_stack:
        snapshots=[PositionSnapshot.from_dict(x) for x in position_window(start,math.nextafter(end,math.inf))]
        for item in project_stack_soc(stack_records,snapshots):
            if item['module'] not in selected_modules:continue
            for i,(metric,cell,cells) in enumerate(normalized):
                if metric not in STACK_SOC_METRICS:continue
                key_name='stack_soc_median' if metric=='stack_soc_median' else 'soc_deviation_pp'
                point={'timestamp':item['timestamp'],'_epoch':item['_epoch'],'value':item[key_name],
                       'module_serial':item['module_serial'],'active_module_count':item['active_module_count']}
                if len(selected_modules)>1:point['module_number']=item['module']
                collectors[i].setdefault((item['module'],0),_ExtremaCollector(limits[i],start,end)).add(point);counts[i]+=1
    read_seconds=time.perf_counter()-started;down=time.perf_counter();metrics=window.metrics
    projected=[]
    for i,(metric,cell,cells) in enumerate(normalized):
        points=[p for collector in (soc.values() if metric=='soc' and include_all_module_soc else collectors[i].values()) for p in collector.points()]
        if metric=='soc' and include_all_module_soc and len(selected_modules)==1:
            points=[{k:v for k,v in p.items() if k not in ('module_number','source')} for p in points]
        value={'metric':metric,'cell_number':cell,'cell_numbers':list(cells or ()),'points':points,
               'raw_points':counts[i],'data_state':dict(metrics)}
        projected.append(normalize_series(value,timestamp_from,timestamp_to))
    soc_series=[normalize_series({'metric':'soc','label':f'Modul {module} SOC','unit':'%',
                 'source':'pylontech','module_number':module,'points':collector.points(),
                 'data_state':dict(metrics)},timestamp_from,timestamp_to) for module,collector in soc.items()]
    if timing:timing.counts(cell_files_opened=metrics['files_opened'],cell_bytes_read=metrics['raw_bytes'],
        cell_parsed_records=metrics['decodes'],cell_seek_mode='confirmed_indexed_window',cache_hit=False,
        cell_sql_vm_steps=metrics['sql_vm_steps'],cell_window_pages=metrics['pages'])
    return {'series':projected,'samples':samples,'raw_records':seen_records,
            'raw_file_records':metrics['decodes'],'file_count':metrics['files_opened'],
            'bytes_read':metrics['raw_bytes'],'parsed_records':metrics['decodes'],
            'selected_modules':list(selected_modules),'soc_module_series':soc_series,
            'read_seconds':read_seconds,'downsample_seconds':time.perf_counter()-down,'cache_hit':False,
            'data_state':metrics,'display_observability':{'history_source_mode':'confirmed_indexed_window',
             'history_window_pages':metrics['pages'],'history_sql_vm_steps':metrics['sql_vm_steps']}}


def project_hycube(window, *, timestamp_from,timestamp_to,max_points=850,timing=None):
    from history_series import _ExtremaCollector
    from hycube_projection import parse_projection_record
    start,end=(datetime.fromisoformat(v.replace('Z','+00:00')).timestamp() for v in (timestamp_from,timestamp_to))
    collector=_ExtremaCollector(max_points,start,end)
    count=0;started=time.perf_counter();previous=None
    with window.read(start,end) as rows:
        for record,ref in rows:
            kind=record.get('record_type')
            if kind=='hycube_history_projection':record,epoch,capacity=parse_projection_record(record)
            elif kind=='hycube_system_observation':
                epoch=ref.timestamp;capacity=record.get('BatteryCapacity')
                if isinstance(capacity,bool) or not isinstance(capacity,(float,int)):continue
            else:raise ResearchQueryError('source_unavailable','Unexpected Hycube source format',503)
            if not math.isfinite(capacity):continue
            point={'timestamp':_iso(epoch),'_epoch':epoch,'value':float(capacity),
                   'source_id':ref.source_id,'source_revision':ref.revision,'source_record_hash':ref.record_hash}
            point.update(source='hycube',source_field='BatteryCapacity',
                **{k:record.get(k) for k in ('device_timestamp','timezone_semantics','parse_quality','payload_sha256','configured_interval_seconds','actual_interval_seconds','actual_interval_quality')},
                _history_storage='projection' if kind=='hycube_history_projection' else 'raw')
            provenance=(ref.source_id,ref.revision)
            if previous is not None and previous!=provenance:point['_segment_start']=True
            previous=provenance;collector.add(point);count+=1
    read_seconds=time.perf_counter()-started;down=time.perf_counter();points=collector.points();m=window.metrics
    if timing:timing.counts(hycube_files_opened=m['files_opened'],hycube_bytes_read=m['raw_bytes'],
        hycube_parsed_records=m['decodes'],hycube_source_mode='confirmed_indexed_window',hycube_sql_vm_steps=m['sql_vm_steps'])
    return normalize_series({'metric':'hycube_battery_capacity','label':'Hycube Battery Capacity','unit':'%',
        'source':'hycube','source_field':'BatteryCapacity','timestamp_source':'received_at','points':points,
        'raw_points':count,'raw_records':count,'read_seconds':read_seconds,
        'downsample_seconds':time.perf_counter()-down,'cache_hit':False,
        'source_mode':'confirmed_indexed_window','data_state':m},timestamp_from,timestamp_to)
