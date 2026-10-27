"""Selected daily policy references, without a scan of accumulated policy Raw.

The predecessor is proved inside the selected first day or its immediate prior
context day. If that context is not indexed, retain the bounded legacy path;
never invent a historical policy from a current setting.
"""
from datetime import datetime, timedelta, timezone
import json
import math

from history_append_receipt import signature
from history_block_index import _load_valid, BlockIndexError, _epoch
from research_budget import ResearchQueryError, check_budget


MAX_RECORDS = 256
MAX_BYTES = 256 * 1024


def indexed_policy_records(directory, timestamp_from, timestamp_to, timing=None):
    start=datetime.fromisoformat(timestamp_from).timestamp()
    end=datetime.fromisoformat(timestamp_to).timestamp()
    first=datetime.fromtimestamp(start,timezone.utc).date()
    last=datetime.fromtimestamp(end,timezone.utc).date()
    # The caller's manual page is one hour. Keep direct calls structurally bounded.
    if not 0 <= (last-first).days <= 90:
        raise ResearchQueryError('history_context_index_required','Policy window exceeds indexed day bound',503)
    candidates=[]; sources={}; opened=0

    def refs(day):
        nonlocal opened
        check_budget()
        path=directory/(day.isoformat()+'.jsonl')
        if not path.exists(): return []
        before=signature(path)
        value=_load_valid(path,timestamp_field='observed_at',iso_timestamp=True)
        if value['indexed_size'] != before['size']:
            raise BlockIndexError('policy writer index is behind')
        if signature(path) != before:
            raise BlockIndexError('policy source changed during selection')
        sources[path]=before; opened+=1
        result=[]
        for ref in value.get('policy_refs',[]):
            check_budget()
            offset,length,stamp=ref['offset'],ref['length'],ref['timestamp']
            if (type(offset) is not int or type(length) is not int or offset<0
                    or not 0<length<=65536 or offset+length>value['indexed_size']
                    or isinstance(stamp,bool) or not math.isfinite(stamp)):
                raise BlockIndexError('invalid policy reference')
            result.append((stamp,str(path),offset,length,ref.get('changed',True)))
        return result

    try:
        for number in range((last-first).days+1):
            candidates.extend(refs(first+timedelta(days=number)))
        before=[r for r in candidates if r[0]<=start]
        if not before:
            before=[r for r in refs(first-timedelta(days=1)) if r[0]<=start]
        if not before:
            # An arbitrary older observation must not be assumed absent.
            return None
        previous=max(before,key=lambda r:r[:3])
        chosen=[previous]+sorted((r for r in candidates if start<r[0]<=end and r[4]),
                                  key=lambda r:r[:3])
        if len(chosen)>MAX_RECORDS or sum(r[3] for r in chosen)>MAX_BYTES:
            raise ResearchQueryError('history_context_index_required',
                'Selected policy references exceed bounded page allowance',503)
        records=[]
        for stamp,name,offset,length,changed in chosen:
            check_budget()
            from pathlib import Path
            path=Path(name)
            if signature(path)!=sources[path]:
                raise BlockIndexError('policy source changed before read')
            with path.open('rb') as stream:
                stream.seek(offset); raw=stream.read(length)
            if len(raw)!=length or not raw.endswith(b'\n'):
                raise BlockIndexError('policy record changed')
            record=json.loads(raw)
            if (record.get('record_type')!='hycube_policy_observation'
                    or record.get('parse_quality')!='complete'
                    or _epoch(record,'observed_at',True)!=stamp
                    or record.get('policy_changed',True)!=changed):
                raise BlockIndexError('policy reference does not match Raw')
            records.append(record)
        if any(signature(path)!=saved for path,saved in sources.items()):
            raise BlockIndexError('policy source changed during read')
    except (OSError,BlockIndexError,KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,ResearchQueryError): raise
        return None
    if timing:timing.counts(policy_files=opened,policy_records=len(records),
        policy_source_mode='selected_indexed_references',policy_bytes=sum(r[3] for r in chosen))
    return records
