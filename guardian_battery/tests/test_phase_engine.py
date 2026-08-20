import json
from config_history import ConfigHistory
from phase_engine import PhaseEngine

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
