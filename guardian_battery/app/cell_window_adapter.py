"""Unactivated adapter from real CellHistoryWriter schema1 to indexed windows.

No directory discovery or fallback. Existing callers still need explicit factory
integration before they can consume this contract.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import math
import json

from cell_source_resolver import CellSourceResolver
from source_catalog import MAX_BYTES, MAX_PAGE

FIELDS=('voltages_mv','current_a','soc_percent','temperatures_c','balancing')


def utc_seconds(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if isinstance(value,datetime):
        if value.tzinfo is None:raise ValueError('explicit timezone required')
        value=value.timestamp()
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value):
        raise ValueError('finite UTC timestamp required')
    return float(value)


def local_day_window(day,zone):
    """A calendar day in an explicit IANA zone, including 23/25-hour days."""
    d=date.fromisoformat(day);tz=ZoneInfo(zone)
    return (datetime.combine(d,time(),tzinfo=tz).timestamp(),
            datetime.combine(d+timedelta(days=1),time(),tzinfo=tz).timestamp())


class CellWindowAdapter:
    def __init__(self,catalog):self.resolver=CellSourceResolver(catalog)

    def open(self,start,end,*,modules=(),inclusive_api_end=False):
        start=utc_seconds(start);end=utc_seconds(end)
        if end<start or (end==start and not inclusive_api_end):raise ValueError('invalid window')
        # Smallest representable larger float: include precisely end, no epsilon
        # interval or sampling-cadence assumption. Apply once to the whole request.
        stop=math.nextafter(end,math.inf) if inclusive_api_end else end
        return self.resolver.open_window(start,stop,modules=modules)

    def page(self,snapshot,*,after=None,fields=FIELDS,limit=MAX_PAGE,max_bytes=MAX_BYTES):
        if not fields or len(set(fields))!=len(fields) or any(f not in FIELDS for f in fields):
            raise ValueError('explicit supported measurement selection required')
        if not 1<=max_bytes<=MAX_BYTES:raise ValueError('invalid byte budget')
        plan=self.resolver.plan_snapshot(snapshot,after=after,limit=limit)
        selected=[];size=0
        for ref in plan['refs']:
            if size+ref.end-ref.start>max_bytes:break
            selected.append(ref);size+=ref.end-ref.start
        if plan['refs'] and not selected:
            return dict(records=[],next=after,complete=False,budget_blocked=True,raw_bytes=0,decodes=0,
                        coverage=plan['coverage'])
        read=self.resolver.read_refs(selected,max_bytes=max_bytes)
        output=[]
        for ref,item in zip(selected,read['records']):
            raw=item['record']
            if raw.get('schema_version')!=1 or utc_seconds(raw.get('timestamp'))!=ref.timestamp:
                raise ValueError('cell schema/index mismatch')
            output.append(dict(source_id=ref.source_id,revision=ref.revision,namespace=ref.storage_convention,
                byte_offset=ref.start,record_hash=ref.record_hash,source_context=json.loads(ref.source_context),
                timestamp=ref.timestamp,module=raw.get('module'),
                module_serial=raw.get('module_serial'),position_history_id=raw.get('position_history_id'),
                identity_epoch=raw.get('identity_epoch'),position_epoch=raw.get('position_epoch'),
                measurements={f:raw[f] for f in fields if f in raw},
                missing_fields=[f for f in fields if f not in raw]))
        more=len(selected)<len(plan['refs']) or plan['next'] is not None
        return dict(read,records=output,next=selected[-1].key if more else None,complete=not more,
                    budget_blocked=False,coverage=plan['coverage'],pending_sources=plan['pending_sources'],
                    invalid_records=plan['invalid_records'],sql_vm_steps=plan['sql_vm_steps'])

    def close(self,snapshot):self.resolver.release_window(snapshot)
