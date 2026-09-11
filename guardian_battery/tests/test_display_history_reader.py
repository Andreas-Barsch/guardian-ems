import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from display_history_projection import DisplayHistoryProjection
from display_history_reader import (
    DisplayHistoryReader, expand_bucket, select_resolution,
)
from history_series import CellHistorySeries
from history_api import HistoryApi


DAY="2026-09-01"


def record(epoch,module=1,soc=50,current=0,voltages=None,temperatures=None):
    return {"schema_version":1,"timestamp":epoch,"module":module,
            "module_serial":f"SERIAL-{module}","soc_percent":soc,
            "current_a":current,"voltages_mv":voltages or [3300]*15,
            "temperatures_c":temperatures or [25]*15,"balancing":[False]*15,
            "physical_groups":{}}


def write(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text("".join(json.dumps(row)+"\n" for row in rows))


def setup_reader(tmp_path,rows,*,now="2026-09-02T00:00:00+00:00",finalize=True):
    cell=tmp_path/"cell";hycube=tmp_path/"hycube";display=tmp_path/"display"
    write(cell/f"{DAY}.jsonl",rows)
    projection=DisplayHistoryProjection(cell,hycube,display,
        clock=lambda:datetime.fromisoformat(now).timestamp(),max_records=100000)
    projection.process_chunk(DAY,finalize=finalize)
    full=CellHistorySeries(cell)
    return DisplayHistoryReader(display,cell,hycube,full),projection,cell,display


def query(reader,start,end,requests=({"metric":"soc"},),modules=(1,),timing=None):
    return reader.query_bundles(requests=requests,timestamp_from=start,
        timestamp_to=end,module_number=modules[0],module_numbers=modules,
        include_all_module_soc=any(r["metric"]=="soc" for r in requests),timing=timing)


def test_resolution_selection_uses_duration_channels_and_budget():
    common={"timestamp_from":"2026-09-01T00:00:00+00:00",
            "requests":({"metric":"soc"},),"max_points":6000}
    assert select_resolution(**common,timestamp_to="2026-09-01T00:10:00+00:00",
                             module_numbers=(1,)) is None
    assert select_resolution(**common,timestamp_to="2026-09-02T00:00:00+00:00",
                             module_numbers=(1,))=="1m"
    assert select_resolution(**common,timestamp_to="2026-09-02T00:00:00+00:00",
                             module_numbers=tuple(range(1,7)))=="15m"
    assert select_resolution(**common,timestamp_to="2026-09-11T00:00:00+00:00",
                             module_numbers=(1,))=="15m"
    assert select_resolution(**common,timestamp_to="2026-10-02T00:00:00+00:00",
                             module_numbers=tuple(range(1,7)))=="60m"


def test_bucket_expansion_preserves_extrema_order_and_deduplicates_aliases():
    bucket={"resolution":"1m","first_timestamp":"2026-09-01T00:00:10+00:00",
        "first_value":80,"min_timestamp":"2026-09-01T00:00:20+00:00","min_value":30,
        "max_timestamp":"2026-09-01T00:00:10+00:00","max_value":80,
        "last_timestamp":"2026-09-01T00:00:50+00:00","last_value":78,
        "physical_serial":"SERIAL-1","module_position":1}
    points=expand_bucket(bucket)
    assert [(p["timestamp"],p["value"]) for p in points]==[
        ("2026-09-01T00:00:10+00:00",80),
        ("2026-09-01T00:00:20+00:00",30),
        ("2026-09-01T00:00:50+00:00",78)]
    assert all(p["derived"] and p["display_source"]=="display_1m" for p in points)


def test_projection_preserves_soc_current_voltage_and_temperature_extrema(tmp_path):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    rows=[record(base+offset,soc=soc,current=current,
          voltages=[3300]*7+[voltage]+[3300]*7,
          temperatures=[25]*7+[temperature]+[25]*7)
          for offset,soc,current,voltage,temperature in
          [(5,80,-1,3300,25),(15,30,8,2900,40),(45,78,-7,3500,10)]]
    reader,_,_,_=setup_reader(tmp_path,rows)
    requests=({"metric":"soc"},{"metric":"current"},
              {"metric":"cell_voltage","cell_numbers":(8,)},
              {"metric":"cell_temperature","cell_numbers":(8,)})
    result=query(reader,"2026-09-01T00:00:00+00:00",
                 "2026-09-02T00:00:00+00:00",requests)
    values={item["metric"]:[p["value"] for p in item["points"]]
            for item in result["series"]}
    assert values["soc"]==[80,30,78]
    assert min(values["current"])==-7 and max(values["current"])==8
    assert min(values["cell_voltage"])==2900 and max(values["cell_voltage"])==3500
    assert min(values["cell_temperature"])==10 and max(values["cell_temperature"])==40
    assert result["display_observability"]["history_source_mode"]=="display_projection"
    assert all("derived" not in point and "display_source" not in point
               for item in result["series"] for point in item["points"])


def test_changed_source_and_corrupt_projection_fall_back_per_day(tmp_path):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    reader,_,cell,display=setup_reader(tmp_path,[record(base+5,soc=10)])
    with (cell/f"{DAY}.jsonl").open("a") as handle:
        handle.write(json.dumps(record(base+10,soc=20))+"\n")
    result=query(reader,"2026-09-01T00:00:00+00:00","2026-09-02T00:00:00+00:00")
    assert result["display_observability"]["history_source_mode"]=="full_resolution"
    assert [p["value"] for p in result["series"][0]["points"]]==[10,20]
    # A gzip failure is also isolated to the full-resolution path.
    (display/"1m"/f"{DAY}.jsonl.gz").write_bytes(b"broken")
    result=query(reader,"2026-09-01T00:00:00+00:00","2026-09-02T00:00:00+00:00")
    assert result["display_observability"]["fallback_days"]==1


def test_open_day_uses_closed_display_prefix_and_exact_full_tail(tmp_path):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    rows=[record(base+5,soc=10),record(base+65,soc=20),record(base+125,soc=30)]
    reader,_,_,_=setup_reader(tmp_path,rows,now="2026-09-01T00:03:00+00:00",finalize=False)
    result=query(reader,"2026-09-01T00:00:00+00:00","2026-09-02T00:00:00+00:00")
    assert result["display_observability"]["history_source_mode"]=="display_plus_tail"
    points=result["series"][0]["points"]
    assert [p["value"] for p in points]==[10,20,30]
    assert len({(p["timestamp"],p["value"]) for p in points})==3
    assert result["display_observability"]["tail_records"]==1


@pytest.mark.parametrize("soc",[0,100,37.5])
def test_soc_edge_values_are_not_filtered_or_normalized(tmp_path,soc):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    reader,_,_,_=setup_reader(tmp_path,[record(base+5,soc=soc)])
    result=query(reader,"2026-09-01T00:00:00+00:00","2026-09-02T00:00:00+00:00")
    assert result["series"][0]["points"][0]["value"]==soc


def test_history_api_prefers_projection_but_keeps_exact_phase_samples(tmp_path):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    rows=[record(base+5,soc=80,current=-1),record(base+15,soc=30,current=-2),
          record(base+45,soc=78,current=3)]
    reader,_,cell,_=setup_reader(tmp_path,rows)
    class Overlays:
        def markers(self,_context):return []
    class Phase:
        def __init__(self):self.samples=None
        def analyse(self,samples,**_kwargs):
            self.samples=samples
            return {"visual_intervals":[],"diagnostic_intervals":[],
                    "relative_endpoints":[],"visual_parameters":{}}
    phase=Phase();api=HistoryApi(CellHistorySeries(cell),Overlays(),phase,
                                 display_reader=reader)
    response=api.handle("GET","/api/history/series?metric=soc&module_number=1"
        "&from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z")
    assert response.status==200
    assert [p["value"] for p in response.body["series"]["points"]]==[80,30,78]
    assert [sample["soc_percent"] for sample in phase.samples]==[80,30,78]
    assert response.body["performance"]["history_source_mode"]=="display_projection"


def test_history_api_uses_complete_canonical_phase_without_full_phase_read(tmp_path):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    reader,_,cell,_=setup_reader(tmp_path,[record(base+5,soc=80,current=-1)])
    class Overlays:
        def markers(self,_context):return []
    class Phase:
        visual_projection=type("Visual",(),{"parameters":{}})()
        def analyse(self,*_args,**_kwargs):raise AssertionError("legacy phase executed")
    class Canonical:
        def query(self,*_args,**_kwargs):
            return {"available":True,"days":["2026-09-01"],"bytes":42,
                "visual_intervals":[{"from":"2026-09-01T00:00:00+00:00",
                    "to":"2026-09-02T00:00:00+00:00","phase":"rest",
                    "phases":["rest"],"sample_count":1,"canonical_sample_count":1}],
                "diagnostic_intervals":[],"relative_endpoints":[],
                "semantics_version":"guardian_canonical_phase_v2",
                "source_mode":"canonical_phase_v2"}
    api=HistoryApi(CellHistorySeries(cell),Overlays(),Phase(),display_reader=reader,
                   canonical_phase_reader=Canonical())
    response=api.handle("GET","/api/history/series?metric=soc&module_number=1"
        "&from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z")
    assert response.status==200
    assert response.body["phase_analysis"]["source_mode"]=="canonical_phase_v2"


def test_missing_one_day_produces_mixed_range_not_global_fallback(tmp_path):
    cell=tmp_path/"cell";display=tmp_path/"display";hycube=tmp_path/"hycube"
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    write(cell/"2026-09-01.jsonl",[record(base+5,soc=10)])
    write(cell/"2026-09-02.jsonl",[record(base+86400+5,soc=20)])
    projection=DisplayHistoryProjection(cell,hycube,display,
        clock=lambda:datetime(2026,9,3,tzinfo=timezone.utc).timestamp())
    projection.build_day("2026-09-01")
    reader=DisplayHistoryReader(display,cell,hycube,CellHistorySeries(cell))
    result=query(reader,"2026-09-01T00:00:00+00:00","2026-09-03T00:00:00+00:00")
    assert result["display_observability"]["history_source_mode"]=="mixed_display_full"
    assert result["display_observability"]["display_days"]==1
    assert result["display_observability"]["fallback_days"]==1
    assert [p["value"] for p in result["series"][0]["points"]]==[10,20]


def test_hycube_soc_uses_display_extrema_and_invalid_day_falls_back(tmp_path):
    display=tmp_path/"display";cell=tmp_path/"cell";hycube=tmp_path/"hycube"
    rows=[{"schema_version":1,"record_type":"hycube_history_projection",
           "received_at":f"2026-09-01T00:00:{second:02d}+00:00",
           "battery_capacity":value,"source_raw_end_offset":second}
          for second,value in ((5,80),(15,0),(45,100))]
    write(hycube/f"{DAY}.jsonl",rows)
    projection=DisplayHistoryProjection(cell,hycube,display,
        clock=lambda:datetime(2026,9,2,tzinfo=timezone.utc).timestamp())
    projection.build_day(DAY)
    class Full:
        def query(self,**_kwargs):
            return {"points":[{"timestamp":"2026-09-01T00:00:30+00:00",
                    "value":55,"source":"hycube","source_field":"BatteryCapacity"}],
                    "raw_records":1}
    reader=DisplayHistoryReader(display,cell,hycube,CellHistorySeries(cell),Full())
    result=reader.query_hycube(timestamp_from="2026-09-01T00:00:00+00:00",
                               timestamp_to="2026-09-01T23:59:59+00:00")
    assert [p["value"] for p in result["points"]]==[80,0,100]
    with (hycube/f"{DAY}.jsonl").open("a") as handle:handle.write(json.dumps(rows[-1])+"\n")
    result=reader.query_hycube(timestamp_from="2026-09-01T00:00:00+00:00",
                               timestamp_to="2026-09-01T23:59:59+00:00")
    assert result["points"][0]["value"]==55
