"""Manual production-volume benchmark for the Phase-2 display reader."""
import gzip
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))

from display_history_projection import (AGGREGATION_ALGORITHM_VERSION,
    CHANNEL_VALUE_FIELDS,DISPLAY_PROJECTION_SCHEMA_VERSION,RESOLUTIONS)
from display_history_reader import DisplayHistoryReader,select_resolution
from history_series import CellHistorySeries


REQUESTS=({"metric":"soc"},{"metric":"current"},
          {"metric":"cell_voltage","cell_numbers":tuple(range(1,16))},
          {"metric":"cell_temperature","cell_numbers":tuple(range(1,16))})


def packed(day,resolution,module,bucket):
    start=datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()+bucket*RESOLUTIONS[resolution]
    channels={}
    for metric,cells in (("soc",(0,)),("current",(0,)),
                         ("cell_voltage",range(1,16)),("cell_temperature",range(1,16))):
        for cell in cells:
            base=(50 if metric=="soc" else -2 if metric=="current" else
                  3300+cell if metric=="cell_voltage" else 25+cell/10)
            name=metric+(f":{cell}" if cell else "")
            channels[name]=[base,1,base,RESOLUTIONS[resolution]-1,
                            base-1,RESOLUTIONS[resolution]/3,
                            base+1,2*RESOLUTIONS[resolution]/3,base,12]
    return {"display_projection_schema_version":1,
      "aggregation_algorithm_version":AGGREGATION_ALGORITHM_VERSION,
      "resolution":resolution,"bucket_start":datetime.fromtimestamp(start,timezone.utc).isoformat(),
      "bucket_end":datetime.fromtimestamp(start+RESOLUTIONS[resolution],timezone.utc).isoformat(),
      "source":"cell_history","module_position":module,
      "physical_serial":f"SERIAL-{module}","identity_quality":"physical_serial",
      "channels":channels,"quality":"observed","derived":True,"authoritative":False}


def fixture(root,days,modules,resolution,start=None):
    cell=root/"cell";display=root/"display";cell.mkdir()
    start=start or datetime(2026,1,1,tzinfo=timezone.utc)
    for offset in range(days):
        day=(start+timedelta(days=offset)).date().isoformat();source=cell/f"{day}.jsonl"
        source.write_text("{}\n");stat=source.stat()
        for resolution,seconds in ((resolution,RESOLUTIONS[resolution]),):
            directory=display/resolution;directory.mkdir(parents=True,exist_ok=True)
            records=[packed(day,resolution,module,bucket)
                     for bucket in range(86400//seconds) for module in range(1,modules+1)]
            data=directory/f"{day}.jsonl.gz"
            with gzip.open(data,"wt",encoding="utf-8") as handle:
                for record in records:handle.write(json.dumps(record,separators=(",",":"))+"\n")
            meta={"display_projection_schema_version":DISPLAY_PROJECTION_SCHEMA_VERSION,
              "aggregation_algorithm_version":AGGREGATION_ALGORITHM_VERSION,
              "status":"complete","day":day,"resolution":resolution,
              "source_signatures":{"cell":{"filename":source.name,"size":stat.st_size,
                 "mtime_ns":stat.st_mtime_ns,"source_schema_version":1,"sha256":"fixture"}},
              "bucket_count":len(records)}
            (directory/f"{day}.meta.json").write_text(json.dumps(meta))
    return DisplayHistoryReader(display,cell,root/"hycube",CellHistorySeries(cell))


def run():
    results=[]
    for modules,days in ((1,1),(6,1),(1,10),(6,10),(1,30),(6,30)):
      with tempfile.TemporaryDirectory(prefix="guardian-display-reader-bench-") as temp:
        root=Path(temp)
        start="2026-01-01T00:00:00+00:00"
        end=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=days)-timedelta(microseconds=1)).isoformat()
        chosen=select_resolution(timestamp_from=start,timestamp_to=end,requests=REQUESTS,
                                 module_numbers=tuple(range(1,modules+1)))
        reader=fixture(root,days,modules,chosen)
        for mode in ("cold","warm"):
            wall=time.perf_counter();cpu=time.thread_time()
            result=reader.query_bundles(requests=REQUESTS,timestamp_from=start,
                timestamp_to=end,module_number=1,module_numbers=tuple(range(1,modules+1)))
            payload=json.dumps(result["series"],separators=(",",":"),ensure_ascii=False).encode()
            results.append({"modules":modules,"days":days,"mode":mode,
              "resolution":chosen,"wall_seconds":time.perf_counter()-wall,
              "thread_cpu_seconds":time.thread_time()-cpu,
              "display_bytes":result["display_observability"]["display_bytes"],
              "display_buckets":result["display_observability"]["display_buckets"],
              "expanded_points":result["display_observability"]["display_points_expanded"],
              "response_series":len(result["series"]),
              "response_points":sum(len(x["points"]) for x in result["series"]),
              "response_bytes":len(payload),"source_mode":result["display_observability"]["history_source_mode"]})
    print(json.dumps(results,indent=2))


if __name__=="__main__":run()
