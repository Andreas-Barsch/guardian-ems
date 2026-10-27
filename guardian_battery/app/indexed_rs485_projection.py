"""Existing bus metric projection from explicit indexed windows and identities."""
import json
import time
from research_budget import check_budget,ResearchQueryError


def project_rs485(reader,requests,*,timestamp_from,timestamp_to,modules,
                  module_number,module_numbers,adr,timing=None):
    from rs485_evidence import _METRIC_FIELDS
    if modules and reader.identity_backend is None:
        raise ResearchQueryError('history_preparation_required','Prepared bus position metadata unavailable',503)
    points={item['metric']:[] for item in requests};unresolved=0;last_sources={}
    started=time.perf_counter();cpu=time.thread_time()
    with reader.window_backend.read(timestamp_from,timestamp_to) as rows:
        for record,ref in rows:
            check_budget()
            if reader._serial_decode(record) is not None or record.get('paired_command') not in (None,0x92):continue
            record_adr=int(record.get('adr',-1));timestamp=record['timestamp']
            identity=json.loads(ref.identity_context).get('bus_identity')
            if record.get('physical_serial'):
                identity={'serial_string':record['physical_serial'],'decode_source':record.get('identity_decode_source','stored_record')}
            position=snapshot_id=None
            if modules:
                if identity is None:unresolved+=1;continue
                if record.get('position') is not None and record.get('position_history_id') and record.get('physical_serial'):
                    position,snapshot_id=record['position'],record['position_history_id']
                else:
                    resolved=reader.identity_backend.position_at(identity['serial_string'],timestamp)
                    position,snapshot_id=resolved['position_at_time'],resolved['position_history_id']
                if position not in modules:continue
            elif adr!=record_adr:continue
            decoded=record.get('decoded') or {}
            for request in requests:
                field=_METRIC_FIELDS[request['metric']]
                if field not in decoded:continue
                value=decoded[field];key=(request['metric'],record_adr,position)
                source=(ref.source_id,ref.revision);previous=last_sources.get(key);last_sources[key]=source
                points[request['metric']].append({'timestamp':timestamp,'value':int(value) if isinstance(value,bool) else value,
                    'adr':record_adr,'identity_resolved':position is not None,
                    'physical_serial':identity['serial_string'] if identity else None,'position':position,
                    **({'module_number':position} if module_numbers else {}),'position_history_id':snapshot_id,
                    'identity_decode_source':identity.get('decode_source') if identity else None,
                    'source_id':ref.source_id,'source_revision':ref.revision,'source_offset':ref.start,
                    **({'_segment_start':True} if previous is not None and previous!=source else {})})
    metrics=reader.window_backend.metrics;invalid=metrics.get('invalid_records',0)
    if timing:
        timing.record('rs485_read_decode_identity',time.perf_counter()-started,time.thread_time()-cpu)
        timing.counts(rs485_files=metrics['files_opened'],rs485_bytes=metrics['raw_bytes'],
            rs485_decodes=metrics['decodes'],rs485_unresolved=unresolved,rs485_invalid_records=invalid,
            rs485_sql_vm_steps=metrics['sql_vm_steps'],rs485_index_validation_bytes=0)
    return [{'metric':request['metric'],
        **({'module_number':module_number} if module_number is not None else {'adr':adr}),
        'identity_unresolved_records':unresolved,'invalid_records':invalid,
        'quality':'partial_data' if invalid else 'partial_identity' if unresolved else 'observed',
        'state_semantics':{'0':'STOP REQUEST','1':'ENABLED'} if request['metric'].endswith('_enable') else None,
        'data_state':metrics,'points':reader._downsample(points[request['metric']],request['metric'].endswith('_enable'))}
        for request in requests]
