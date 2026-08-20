import json
import pytest
from config_history import ConfigHistory
from phase_engine import PhaseEngine, VisualPhaseProjection

PARAMS={"cell_diag_low_soc_percent":30,"cell_diag_high_soc_percent":80,
        "cell_diag_charge_current_a":0.8,"cell_diag_discharge_current_a":0.8}
def sample(at,current=0,soc=50,voltage=3300):
    return {"timestamp":at,"current_a":current,"soc_percent":soc,"voltages_mv":[voltage]*15}
def test_historical_current_and_unknown_modes(tmp_path):
    path=tmp_path/"config.jsonl"; record={"schema_version":1,"timestamp":"2026-08-20T00:00:00+00:00","config_id":"a","parameters":PARAMS}
    path.write_text(json.dumps(record)+"\n")
    engine=PhaseEngine(ConfigHistory(path),lambda:{**PARAMS,"cell_diag_charge_current_a":2})
    values=[sample("2026-08-19T00:00:00+00:00",1),sample("2026-08-20T01:00:00+00:00",1)]
    assert [i["phase"] for i in engine.intervals(values,mode="historical")] == ["unknown","charge"]
    assert engine.intervals(values[1:],mode="current")[0]["phase"] == "rest"
def test_what_if_is_explicit_and_does_not_write_history(tmp_path):
    path=tmp_path/"missing.jsonl"; engine=PhaseEngine(ConfigHistory(path),lambda:PARAMS)
    assert engine.intervals([sample("2026-08-20T00:00:00+00:00",1)],mode="what_if",what_if=PARAMS)[0]["phase"]=="charge"
    assert not path.exists()

def test_visual_projection_suppresses_brief_threshold_jitter_without_changing_diagnostics(tmp_path):
    engine=PhaseEngine(ConfigHistory(tmp_path/"missing.jsonl"),lambda:PARAMS,
                       VisualPhaseProjection({"minimum_duration_seconds":120,"current_hysteresis_a":.2,"short_gap_seconds":90}))
    values=[sample("2026-08-20T00:00:00+00:00",0),sample("2026-08-20T00:01:00+00:00",1.1),
            sample("2026-08-20T00:02:00+00:00",0),sample("2026-08-20T00:03:00+00:00",1.1),
            sample("2026-08-20T00:05:00+00:00",1.1)]
    result=engine.analyse(values,mode="current",window_to="2026-08-20T00:06:00+00:00")
    assert [item["phase"] for item in result["diagnostic_intervals"]] == ["rest","charge","rest","charge"]
    assert [item["phase"] for item in result["visual_intervals"]] == ["rest","charge"]
    assert result["visual_parameters"]["current_hysteresis_a"] == .2

def test_visual_projection_keeps_long_rest_and_soc_regions(tmp_path):
    engine=PhaseEngine(ConfigHistory(tmp_path/"missing.jsonl"),lambda:PARAMS,
                       VisualPhaseProjection({"minimum_duration_seconds":60,"current_hysteresis_a":.1,"short_gap_seconds":30}))
    values=[sample("2026-08-20T00:00:00+00:00",0,20),sample("2026-08-20T00:02:00+00:00",0,50),
            sample("2026-08-20T00:04:00+00:00",-1.2,50),sample("2026-08-20T00:05:00+00:00",-1.2,50)]
    result=engine.analyse(values,mode="current",window_to="2026-08-20T00:06:00+00:00")
    assert [item["phase"] for item in result["visual_intervals"]] == ["rest+low","rest","discharge"]
    assert result["diagnostic_intervals"] == engine.intervals(values,mode="current",window_to="2026-08-20T00:06:00+00:00")

@pytest.mark.parametrize(("current","phase"),[(1.2,"charge"),(-1.2,"discharge"),(0,"rest")])
def test_stable_visual_operating_states(tmp_path,current,phase):
    engine=PhaseEngine(ConfigHistory(tmp_path/"missing.jsonl"),lambda:PARAMS,
                       VisualPhaseProjection({"minimum_duration_seconds":60,"current_hysteresis_a":.2,"short_gap_seconds":60}))
    values=[sample(f"2026-08-20T00:0{minute}:00+00:00",current) for minute in range(5)]
    assert engine.analyse(values,mode="current",window_to="2026-08-20T00:05:00+00:00")["visual_intervals"][0]["phase"] == phase

@pytest.mark.parametrize("impulse",[1.2,-1.2])
def test_short_impulse_during_rest_is_suppressed(tmp_path,impulse):
    engine=PhaseEngine(ConfigHistory(tmp_path/"missing.jsonl"),lambda:PARAMS)
    values=[sample("2026-08-20T00:00:00+00:00",0),sample("2026-08-20T00:01:00+00:00",impulse),
            sample("2026-08-20T00:02:00+00:00",0),sample("2026-08-20T00:06:00+00:00",0)]
    assert [item["phase"] for item in engine.analyse(values,mode="current",window_to="2026-08-20T00:07:00+00:00")["visual_intervals"]] == ["rest"]

@pytest.mark.parametrize("surrounding",[1.2,-1.2])
def test_short_rest_gap_is_merged_into_surrounding_state(tmp_path,surrounding):
    engine=PhaseEngine(ConfigHistory(tmp_path/"missing.jsonl"),lambda:PARAMS,
                       VisualPhaseProjection({"minimum_duration_seconds":0,"current_hysteresis_a":0,"short_gap_seconds":90}))
    values=[sample("2026-08-20T00:00:00+00:00",surrounding),sample("2026-08-20T00:10:00+00:00",0),
            sample("2026-08-20T00:11:00+00:00",surrounding)]
    assert len(engine.analyse(values,mode="current",window_to="2026-08-20T00:20:00+00:00")["visual_intervals"]) == 1

def test_hysteresis_prevents_threshold_chatter_and_real_change_appears(tmp_path):
    engine=PhaseEngine(ConfigHistory(tmp_path/"missing.jsonl"),lambda:PARAMS,
                       VisualPhaseProjection({"minimum_duration_seconds":60,"current_hysteresis_a":.2,"short_gap_seconds":0}))
    values=[sample("2026-08-20T00:00:00+00:00",0),sample("2026-08-20T00:01:00+00:00",.85),
            sample("2026-08-20T00:02:00+00:00",.95),sample("2026-08-20T00:03:00+00:00",1.1),
            sample("2026-08-20T00:04:00+00:00",1.1)]
    assert [item["phase"] for item in engine.analyse(values,mode="current",window_to="2026-08-20T00:05:00+00:00")["visual_intervals"]] == ["rest","charge"]
