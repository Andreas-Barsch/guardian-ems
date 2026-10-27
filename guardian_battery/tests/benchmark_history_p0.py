"""Synthetic isolated RS485 benchmark; source tree is selected by PYTHONPATH."""
import json,sys,time,tracemalloc,tempfile,hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
from rs485_evidence import Rs485HistorySeries
from history_block_index import build_index
from position_history import PositionSnapshot,PositionHistoryLog
from inspect import signature

root=Path(tempfile.mkdtemp(prefix='guardian-p0-bench-'));bus=root/'bus';bus.mkdir()
positions=root/'positions.jsonl'
serials={str(i):f'Y225004C32250{i:03d}' for i in range(1,7)}
positions.write_text(json.dumps({'schema_version':1,'position_history_id':'PHS-00000000-0000-0000-0000-000000000001','effective_at':'2024-01-01T00:00:00+00:00','created_at':'2024-01-01T00:00:00+00:00','maintenance_event_id':'MEV-benchmark','positions':serials})+'\n')
endday=datetime(2026,9,1,tzinfo=timezone.utc)
new='module_numbers' in signature(Rs485HistorySeries.query_bundles).parameters
counts={};real_open=Path.open;real_loads=json.loads
class Measured:
 def __init__(self,handle):self.handle=handle
 def __enter__(self):return self
 def __exit__(self,*args):self.handle.close()
 def __getattr__(self,k):return getattr(self.handle,k)
 def readline(self,*args):
  value=self.handle.readline(*args);counts['raw_bytes']+=len(value.encode() if isinstance(value,str) else value);return value
 def read(self,*args):
  value=self.handle.read(*args);counts['raw_bytes']+=len(value.encode() if isinstance(value,str) else value);return value
 def __iter__(self):return self
 def __next__(self):
  value=self.readline()
  if not value:raise StopIteration
  return value

def measured_open(path,*args,**kwargs):
 handle=real_open(path,*args,**kwargs)
 if path.parent==bus and path.suffix=='.jsonl':
  counts['raw_opens']+=1;return Measured(handle)
 return handle

def measured_loads(value,*args,**kwargs):
 result=real_loads(value,*args,**kwargs)
 if isinstance(result,dict) and result.get('record_type')=='frame': counts['frame_decodes']+=1
 return result

def create(day):
 path=bus/f'{day.date()}.jsonl'
 if path.exists():return
 rows=[]
 for i in range(1,7):
  serial=serials[str(i)]
  rows.append({'record_type':'frame','timestamp':day.isoformat(),'direction':'response','paired_command':147,'checksum_valid':True,'frame_complete':True,'request_matched':True,'adr':i,'info_raw':(bytes([i])+serial.encode()).hex()})
 for n in range(60):
  stamp=(day+timedelta(minutes=n*24+1)).isoformat()
  for i in range(1,7):
   rows.append({'record_type':'frame','timestamp':stamp,'adr':i,'paired_command':146,'physical_serial':serials[str(i)],'position':i,'position_history_id':'PHS-00000000-0000-0000-0000-000000000001','decoded':{'charge_current_limit_a':n//10,'charge_enable':n%20<10}})
 path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
 build_index(path,timestamp_field='timestamp',iso_timestamp=True)

baseline_values=None
for age in (1,10,30,365):
 for days in range(age):create(endday-timedelta(days=days))
 reader=Rs485HistorySeries(bus,position_history_path=positions)
 for state in ('cold','warm'):
  counts={'raw_bytes':0,'raw_opens':0,'frame_decodes':0}
  Path.open=measured_open;json.loads=measured_loads
  tracemalloc.start();started=time.perf_counter()
  try:
   kwargs=dict(timestamp_from=endday.isoformat(),timestamp_to=(endday+timedelta(days=1,microseconds=-1)).isoformat())
   requests=[{'metric':'rs485_ccl'},{'metric':'rs485_charge_enable'}]
   if new: output=reader.query_bundles(requests,module_numbers=(1,2,3,4,5,6),**kwargs)
   else: output=[result for module in range(1,7) for result in reader.query_bundles(requests,module_number=module,**kwargs)]
   values=sorted((result['metric'],point.get('module_number',result.get('module_number')),point['timestamp'],point['value']) for result in output for point in result['points'])
   if baseline_values is None:baseline_values=values
   assert values==baseline_values
   canonical=[]
   for metric in ('rs485_ccl','rs485_charge_enable'):
    for module in range(1,7):
     stream=[row for row in values if row[:2]==(metric,module)]
     if metric.endswith('_enable') and stream:
      changes=[stream[0]]
      for row in stream[1:]:
       if row[3]!=changes[-1][3]:changes.append(row)
      if changes[-1]!=stream[-1]:changes.append(stream[-1])
      stream=changes
     canonical.extend(stream)
   semantic_hash=hashlib.sha256(json.dumps(canonical).encode()).hexdigest()
   peak=tracemalloc.get_traced_memory()[1]
  finally:
   elapsed=time.perf_counter()-started;tracemalloc.stop();Path.open=real_open;json.loads=real_loads
  print(json.dumps({'implementation':'p0' if new else 'baseline086','days':age,'cache':state,**counts,'seconds':round(elapsed,4),'peak_bytes':peak,'points':len(values),'semantic_sha256':semantic_hash}),flush=True)
