import json
from pathlib import Path
from datetime import datetime,timedelta

from canonical_phase import (ALGORITHM_VERSION, CanonicalPhaseMachine,
    CanonicalPhaseProjection, CanonicalPhaseReader, SEMANTICS_VERSION,
    clip_interval)
from config_history import ConfigHistory


PARAMS={"cell_diag_low_soc_percent":30,"cell_diag_high_soc_percent":80,
        "cell_diag_charge_current_a":.8,"cell_diag_discharge_current_a":.8}


def sample(timestamp,current=0,soc=50):
    return {"schema_version":1,"timestamp":timestamp,"module":1,
            "module_serial":"SERIAL-A","current_a":current,"soc_percent":soc,
            "voltages_mv":[3300]*15,"temperatures_c":[20]*15}


def feed(machine, values):
    for order,item in enumerate(values):
        machine.process(item,PARAMS,config_id="cfg",
            source_cursor={"source_day":"2026-01-01",
                           "source_offset":int(float(item["timestamp"])) if isinstance(item["timestamp"],(int,float)) else order})


def test_snapshot_restore_equals_continuous():
    values=[sample(0,0),sample(60,1.1),sample(180,1.1),sample(240,1.1),
            sample(300,1.1),sample(400,0),sample(700,0)]
    continuous=CanonicalPhaseMachine("epoch","SERIAL-A");feed(continuous,values)
    first=CanonicalPhaseMachine("epoch","SERIAL-A");feed(first,values[:3])
    prior_diagnostic=list(first.final_diagnostic);prior_visual=list(first.final_visual)
    prior_endpoints=list(first.endpoints)
    restored=CanonicalPhaseMachine.restore(first.snapshot());feed(restored,values[3:])
    result=restored.materialized();expected=continuous.materialized()
    assert prior_diagnostic+result["diagnostic_intervals"]==expected["diagnostic_intervals"]
    assert prior_visual+result["visual_intervals"]==expected["visual_intervals"]
    assert prior_endpoints+result["relative_endpoints"]==expected["relative_endpoints"]
    assert result["checkpoint"]==expected["checkpoint"]


def test_request_clips_but_canonical_count_is_stable():
    item={"canonical_from":"2026-01-01T00:00:00+00:00",
          "canonical_to":"2026-01-01T00:10:00+00:00","phase":"charge",
          "phases":["charge"],"canonical_sample_count":3,
          "count_timestamps":[1767225600,1767225900,1767226200],"config_id":"cfg"}
    clipped=clip_interval(item,"2026-01-01T00:04:00+00:00",
                          "2026-01-01T00:06:00+00:00")
    assert clipped["canonical_sample_count"]==3 and clipped["sample_count"]==1
    assert clipped["canonical_from"]==item["canonical_from"]


def test_cross_day_candidate_is_not_reinitialized():
    machine=CanonicalPhaseMachine("epoch","SERIAL-A",
        parameters={"minimum_duration_seconds":120})
    values=[sample("2026-01-01T23:57:00+00:00",0),
            sample("2026-01-01T23:59:00+00:00",1.1),
            sample("2026-01-02T00:00:00+00:00",1.1),
            sample("2026-01-02T00:01:00+00:00",1.1)]
    feed(machine,values)
    phases=machine.materialized()["visual_intervals"]
    assert phases[0]["phase"]=="rest"
    assert phases[-1]["phase"]=="charge"
    assert phases[-1]["canonical_from"]=="2026-01-02T00:01:00+00:00"


def test_short_gap_streaming_and_endpoint():
    machine=CanonicalPhaseMachine("epoch","SERIAL-A",
        parameters={"minimum_duration_seconds":0,"current_hysteresis_a":0,
                    "short_gap_seconds":90})
    feed(machine,[sample(0,1),sample(600,0),sample(601,0),sample(660,1),
                  sample(661,1),sample(720,0)])
    visual=machine.materialized()["visual_intervals"]
    assert visual[0]["phase"]=="charge"
    assert visual[0]["canonical_sample_count"]==6


def test_projection_reader_and_corrupt_fallback(tmp_path):
    cell=tmp_path/"cell";cell.mkdir();output=tmp_path/"phase"
    day="2026-01-01";start=1767225600
    with (cell/f"{day}.jsonl").open("w") as handle:
        for index,current in enumerate((0,1.1,1.1,1.1)):
            handle.write(json.dumps(sample(start+index*180,current))+"\n")
    config=tmp_path/"config.jsonl"
    config.write_text(json.dumps({"schema_version":1,
        "timestamp":"2025-12-31T00:00:00+00:00","config_id":"cfg",
        "parameters":PARAMS})+"\n")
    result=CanonicalPhaseProjection(cell,output,ConfigHistory(config)).build()
    assert result["samples"]==4
    reader=CanonicalPhaseReader(output)
    actual=reader.query("2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:20:00+00:00",module_number=1)
    assert actual["available"] and actual["semantics_version"]==SEMANTICS_VERSION
    assert actual["diagnostic_intervals"][0]["canonical_sample_count"]==1
    (output/f"{day}.json.gz").write_bytes(b"broken")
    assert reader.query("2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:20:00+00:00")["reason"]=="corrupt canonical phase artifact"


def test_duplicate_timestamp_uses_source_offset_and_out_of_order_rejected():
    machine=CanonicalPhaseMachine("epoch")
    machine.process(sample(1),PARAMS,source_cursor={"source_day":"x","source_offset":1})
    machine.process(sample(1),PARAMS,source_cursor={"source_day":"x","source_offset":2})
    try:
        machine.process(sample(0),PARAMS,source_cursor={"source_day":"x","source_offset":3})
    except ValueError as exc:
        assert "canonical order" in str(exc)
    else: raise AssertionError("clock regression was accepted")


def test_current_day_update_consumes_only_new_tail(tmp_path):
    cell=tmp_path/"cell";cell.mkdir();output=tmp_path/"phase";day="2026-01-01"
    config=tmp_path/"config.jsonl";config.write_text(json.dumps({"schema_version":1,
        "timestamp":"2025-01-01T00:00:00+00:00","config_id":"cfg",
        "parameters":PARAMS})+"\n")
    path=cell/f"{day}.jsonl";path.write_text(json.dumps(sample(1767225600))+"\n")
    projection=CanonicalPhaseProjection(cell,output,ConfigHistory(config))
    first=projection.update_current(day);assert first["samples"]==1
    with path.open("a") as handle:handle.write(json.dumps(sample(1767225660,1.1))+"\n")
    second=projection.update_current(day)
    assert second["samples"]==1 and second["bytes"]>0
    assert projection.update_current(day)["samples"]==0


def test_reader_rejects_source_change(tmp_path):
    cell=tmp_path/"cell";cell.mkdir();output=tmp_path/"phase";day="2026-01-01"
    path=cell/f"{day}.jsonl";path.write_text(json.dumps(sample(1767225600))+"\n")
    config=tmp_path/"config.jsonl";config.write_text(json.dumps({"schema_version":1,
        "timestamp":"2025-01-01T00:00:00+00:00","config_id":"cfg",
        "parameters":PARAMS})+"\n")
    CanonicalPhaseProjection(cell,output,ConfigHistory(config)).build()
    reader=CanonicalPhaseReader(output,cell,config)
    assert reader.query("2026-01-01T00:00:00+00:00","2026-01-01T01:00:00+00:00",
                        module_number=1)["available"]
    with path.open("a") as handle:handle.write("\n")
    assert reader.query("2026-01-01T00:00:00+00:00","2026-01-01T01:00:00+00:00",
                        module_number=1)["reason"]=="source_signature_mismatch"


def test_position_change_splits_provenance_without_resetting_phase():
    machine=CanonicalPhaseMachine("SERIAL-A@observed","SERIAL-A",
        parameters={"minimum_duration_seconds":0})
    first=sample(0,1);second=sample(1,1);second["module"]=2
    feed(machine,[first,second])
    intervals=machine.materialized()["diagnostic_intervals"]
    assert [item["module_position"] for item in intervals]==[1,2]
    assert machine.axis_previous=="charge"


def test_config_change_splits_interval_and_keeps_state():
    machine=CanonicalPhaseMachine("epoch","SERIAL-A")
    machine.process(sample(0,1),PARAMS,config_id="a",
                    source_cursor={"source_day":"x","source_offset":0})
    changed={**PARAMS,"cell_diag_charge_current_a":2}
    machine.process(sample(1,1),changed,config_id="b",
                    source_cursor={"source_day":"x","source_offset":1})
    assert [item["config_id"] for item in machine.materialized()["diagnostic_intervals"]]==["a","b"]
    assert machine.stable_axis=="charge"


def _multiday_fixture(root,days):
    cell=root/"cell";cell.mkdir();config=root/"config.jsonl"
    config.write_text(json.dumps({"schema_version":1,
        "timestamp":"2025-01-01T00:00:00+00:00","config_id":"cfg",
        "parameters":PARAMS})+"\n")
    start=datetime(2026,1,1)
    for offset in range(days):
        moment=(start+timedelta(days=offset)).timestamp()
        path=cell/f"{(start+timedelta(days=offset)).date().isoformat()}.jsonl"
        path.write_text("".join(json.dumps(sample(moment+second,current))+"\n"
            for second,current in ((1,0),(301,1.1))))
    return cell,config


def test_bounded_rebuild_resume_and_idempotence(tmp_path):
    cell,config=_multiday_fixture(tmp_path,30);output=tmp_path/"phase"
    projection=CanonicalPhaseProjection(cell,output,ConfigHistory(config))
    first=projection.build(stop_after_days=7)
    assert first["days_rebuilt"]==7 and first["peak_sample_objects"]==2
    second=projection.build()
    assert second["days_reused"]==7 and second["days_rebuilt"]==23
    before={path.name:path.read_bytes() for path in output.glob("*.json.gz")}
    third=projection.build()
    assert third["days_reused"]==30 and third["days_rebuilt"]==0
    assert before=={path.name:path.read_bytes() for path in output.glob("*.json.gz")}


def test_peak_day_collection_does_not_grow_with_duration(tmp_path):
    cell,config=_multiday_fixture(tmp_path,30)
    result=CanonicalPhaseProjection(cell,tmp_path/"phase",ConfigHistory(config)).build()
    assert result["peak_sample_objects"]==2


def test_historical_build_leaves_current_day_to_tail_updater(tmp_path):
    cell,config=_multiday_fixture(tmp_path,1);output=tmp_path/"phase"
    clock=datetime(2026,1,1,12).timestamp
    projection=CanonicalPhaseProjection(cell,output,ConfigHistory(config),clock=clock)
    result=projection.build()
    assert result["days"]==0 and not (output/"2026-01-01.json.gz").exists()
    assert projection.update_current("2026-01-01")["samples"]==2


def test_source_day_contract_rejects_misfiled_timestamp(tmp_path):
    cell,config=_multiday_fixture(tmp_path,1)
    wrong=sample(datetime(2026,1,2).timestamp(),0)
    (cell/"2026-01-01.jsonl").write_text(json.dumps(wrong)+"\n")
    try:CanonicalPhaseProjection(cell,tmp_path/"phase",ConfigHistory(config)).build()
    except ValueError as exc:assert "source-day" in str(exc)
    else:raise AssertionError("misfiled timestamp accepted")


def test_source_change_rebuild_propagation_stops_after_equal_following_day(tmp_path):
    cell,config=_multiday_fixture(tmp_path,4);output=tmp_path/"phase"
    projection=CanonicalPhaseProjection(cell,output,ConfigHistory(config))
    projection.build()
    first=cell/"2026-01-01.jsonl"
    records=[json.loads(line) for line in first.read_text().splitlines()]
    first.write_text("".join(json.dumps(item)+" \n" for item in records))
    result=projection.build()
    assert result["days_reused"]==0
    assert result["days_rebuilt"]==2
    assert result["propagation_stopped"] is True


def test_failure_before_day_publish_keeps_previous_completed_day(tmp_path,monkeypatch):
    cell,config=_multiday_fixture(tmp_path,3);output=tmp_path/"phase"
    projection=CanonicalPhaseProjection(cell,output,ConfigHistory(config))
    original=projection.store.write_day
    def fail(day,artifact):
        if day=="2026-01-02":raise OSError("injected")
        original(day,artifact)
    monkeypatch.setattr(projection.store,"write_day",fail)
    try:projection.build()
    except OSError:pass
    else:raise AssertionError("failure not injected")
    assert (output/"2026-01-01.json.gz").is_file()
    resumed=CanonicalPhaseProjection(cell,output,ConfigHistory(config)).build()
    assert resumed["days_reused"]==1 and resumed["days_rebuilt"]==2
