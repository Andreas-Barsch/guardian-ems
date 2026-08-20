"""Diagnostic phases and a strictly separate, smoothed visual projection."""
from __future__ import annotations
from bisect import bisect_right
from datetime import datetime
from cell_diagnostics import classify_phases

PHASE_MODES=frozenset({"historical","current","what_if"})
PHASE_PARAMETER_KEYS=("cell_diag_low_soc_percent","cell_diag_high_soc_percent","cell_diag_charge_current_a","cell_diag_discharge_current_a")
VISUAL_PHASE_DEFAULTS={"minimum_duration_seconds":180,"current_hysteresis_a":0.2,"short_gap_seconds":120}
class PhaseEngineError(ValueError):pass

class VisualPhaseProjection:
    """UI-only state stabilisation; never feeds diagnostic evaluation."""
    def __init__(self,parameters=None):self.parameters={**VISUAL_PHASE_DEFAULTS,**(parameters or {})}
    @staticmethod
    def _seconds(a,b):return (datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds()
    def project(self,classified,window_to):
        if not classified:return []
        minimum=float(self.parameters["minimum_duration_seconds"]);gap=float(self.parameters["short_gap_seconds"])
        stable=classified[0]["visual_axis"];candidate=None;candidate_at=None;states=[]
        for item in classified:
            value=item["visual_axis"]
            if value==stable:candidate=candidate_at=None
            elif value!=candidate:candidate=value;candidate_at=item["timestamp"]
            elif self._seconds(candidate_at,item["timestamp"])>=minimum:
                stable=candidate;candidate=candidate_at=None
            states.append({**item,"visual_axis":stable})
        segments=[]
        for index,item in enumerate(states):
            end=states[index+1]["timestamp"] if index+1<len(states) else window_to
            soc=item["soc_region"];phase=item["visual_axis"]+("+"+soc if soc else "")
            if segments and segments[-1]["phase"]==phase:segments[-1]["to"]=end;segments[-1]["sample_count"]+=1
            else:segments.append({"from":item["timestamp"],"to":end,"phase":phase,"phases":[item["visual_axis"]]+([soc] if soc else []),"sample_count":1})
        changed=True
        while changed and len(segments)>=3:
            changed=False;merged=[];i=0
            while i<len(segments):
                if i+2<len(segments) and segments[i]["phase"]==segments[i+2]["phase"] and self._seconds(segments[i+1]["from"],segments[i+1]["to"])<=gap:
                    item=dict(segments[i]);item["to"]=segments[i+2]["to"];item["sample_count"]+=segments[i+1]["sample_count"]+segments[i+2]["sample_count"];merged.append(item);i+=3;changed=True
                else:merged.append(segments[i]);i+=1
            segments=merged
        return segments

class PhaseEngine:
    def __init__(self,config_history,current_config_provider,visual_projection=None):
        self.config_history=config_history;self.current_config_provider=current_config_provider
        self.visual_projection=visual_projection or VisualPhaseProjection()
    def _resolver(self,mode,what_if):
        if mode=="historical":
            records=self.config_history.records();times=[r["timestamp"] for r in records]
            return lambda timestamp: records[bisect_right(times,timestamp)-1]["parameters"] if bisect_right(times,timestamp) else None
        if mode=="current":
            options=self.current_config_provider();return lambda _timestamp:options
        if not isinstance(what_if,dict) or set(what_if)!=set(PHASE_PARAMETER_KEYS):raise PhaseEngineError("what_if requires exactly the four phase parameters")
        return lambda _timestamp:what_if
    @staticmethod
    def _axis(current,options,previous,hysteresis):
        charge=float(options["cell_diag_charge_current_a"]);discharge=float(options["cell_diag_discharge_current_a"])
        if previous=="charge" and current>=charge-hysteresis:return "charge"
        if previous=="discharge" and current<=-discharge+hysteresis:return "discharge"
        if current>=charge+hysteresis:return "charge"
        if current<=-discharge-hysteresis:return "discharge"
        return "rest"
    def analyse(self,samples,*,mode="historical",what_if=None,window_to=None):
        if mode not in PHASE_MODES:raise PhaseEngineError("unsupported analysis mode")
        resolve=self._resolver(mode,what_if);diagnostic=[];classified=[];previous="rest"
        for index,sample in enumerate(samples):
            options=resolve(sample["timestamp"]);phases=classify_phases(sample,options) if options else ["unknown"]
            end=samples[index+1]["timestamp"] if index+1<len(samples) else (window_to or sample["timestamp"]);key="+".join(phases)
            if diagnostic and diagnostic[-1]["phase"]==key:diagnostic[-1]["to"]=end;diagnostic[-1]["sample_count"]+=1
            else:diagnostic.append({"from":sample["timestamp"],"to":end,"phase":key,"phases":phases,"sample_count":1})
            if options:
                previous=self._axis(float(sample["current_a"]),options,previous,float(self.visual_projection.parameters["current_hysteresis_a"]))
                soc="low" if "low" in phases else "high" if "high" in phases else None
            else:previous="unknown";soc=None
            classified.append({"timestamp":sample["timestamp"],"visual_axis":previous,"soc_region":soc})
        return {"diagnostic_intervals":diagnostic,"visual_intervals":self.visual_projection.project(classified,window_to or (samples[-1]["timestamp"] if samples else "")),"visual_parameters":dict(self.visual_projection.parameters)}
    def intervals(self,samples,*,mode="historical",what_if=None,window_to=None):
        return self.analyse(samples,mode=mode,what_if=what_if,window_to=window_to)["diagnostic_intervals"]
