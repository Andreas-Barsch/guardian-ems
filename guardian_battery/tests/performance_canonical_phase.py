"""Manual 5-second canonical phase build/read benchmark."""
import json
import tempfile
import time
import os
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))

from canonical_phase import CanonicalPhaseProjection,CanonicalPhaseReader
from config_history import ConfigHistory

PARAMS={"cell_diag_low_soc_percent":30,"cell_diag_high_soc_percent":80,
 "cell_diag_charge_current_a":.8,"cell_diag_discharge_current_a":.8}
os.environ["TZ"]="UTC"
if hasattr(time,"tzset"):time.tzset()

def create(root,days,modules):
    cell=root/"cell";cell.mkdir();config=root/"config.jsonl"
    config.write_text(json.dumps({"schema_version":1,"timestamp":"2025-01-01T00:00:00+00:00",
      "config_id":"cfg","parameters":PARAMS})+"\n")
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    for day_offset in range(days):
        day=start+timedelta(days=day_offset)
        with (cell/f"{day.date().isoformat()}.jsonl").open("w") as handle:
            for tick in range(0,86400,5):
                axis=(tick//21600)%4
                current=(0,1.2,0,-1.2)[axis]
                for module in range(1,modules+1):
                    record={"schema_version":1,"timestamp":day.timestamp()+tick+module/100,
                      "module":module,"module_serial":f"SERIAL-{module}","current_a":current,
                      "soc_percent":50,"voltages_mv":[3300]*15,
                      "temperatures_c":[25]*15,"balancing":[False]*15}
                    handle.write(json.dumps(record,separators=(",",":"))+"\n")
    return cell,config

def run():
    results=[]
    matrix=((1,1),(6,1),(1,10),(6,10),(1,30),(6,30))
    selected=os.environ.get("CANONICAL_BENCH_MATRIX")
    if selected:
      modules,days=(int(value) for value in selected.split("x"));matrix=((modules,days),)
    for modules,days in matrix:
      with tempfile.TemporaryDirectory(prefix="guardian-canonical-phase-") as value:
        root=Path(value);cell,config=create(root,days,modules);output=root/"phase"
        build=CanonicalPhaseProjection(cell,output,ConfigHistory(config)).build()
        reader=CanonicalPhaseReader(output,cell,config)
        start="2026-01-01T00:00:00+00:00"
        end=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=days)-timedelta(microseconds=1)).isoformat()
        for mode in ("cold","warm"):
          before=time.perf_counter();cpu=time.thread_time()
          response=reader.query(start,end,module_number=1)
          payload=json.dumps(response,separators=(",",":"),ensure_ascii=False).encode()
          results.append({"modules":modules,"days":days,"mode":mode,
            "wall_seconds":time.perf_counter()-before,"thread_cpu_seconds":time.thread_time()-cpu,
            "response_bytes":len(payload),"visual_intervals":len(response["visual_intervals"]),
            "diagnostic_intervals":len(response["diagnostic_intervals"]),
            "relative_endpoints":len(response["relative_endpoints"]),**({"build":build} if mode=="cold" else {})})
        print(json.dumps(results[-2:],indent=2),flush=True)

if __name__=="__main__":run()
