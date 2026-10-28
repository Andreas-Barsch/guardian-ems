"""Event-driven planning and cheap live reach display; no device actions."""
import copy
from datetime import timedelta
from .model import Policy, InputError, iso, timestamp, resample, flows, forward
from .planning import autonomy
from .sources import ReadClient, read_token, estimate_soc_energy
from .guardian_history import current


def guarded_requirement(snapshot, policy, now, seconds):
    """Maximum 24h starting-energy need until next planning deadline.

    Use original PV boundaries and boundaries shifted by outage duration, not
    averaged whole hours. Between these knots the requirement is piecewise
    linear/convex, so the maximum is at a knot. No extra solar discount.
    """
    forecast=snapshot['forecasts']['hycube']['intervals']
    factor=snapshot.get('solar_learning',{}).get('hycube',{}).get('factor',policy.solar_correction_factor)
    load=snapshot['rolling_minimum_kw'];stop=now+timedelta(seconds=seconds)
    outage=timedelta(hours=policy.outage_hours)
    boundaries={timestamp(r[k]) for r in forecast for k in ('start','end')}
    candidates=sorted({now,stop}|{t for t in boundaries if now<t<stop}|{t-outage for t in boundaries if now<t-outage<stop})
    answers=[]
    for start in candidates:
        end=start+outage
        edges=sorted({start,end}|{t for t in boundaries if start<t<end})
        energies=resample(forecast,edges,'hycube')
        if any(v is None for v in energies):raise InputError('Rollendes 24h-Fenster nicht vollständig prognostiziert')
        requirement=policy.terminal_reserve_kwh;peak=requirement;bad=False
        for i in reversed(range(len(energies))):
            hours=(edges[i+1]-edges[i]).total_seconds()/3600
            d,g,unmet=flows(energies[i]*factor/hours,load+policy.standby_kw,policy)
            requirement=max(0,requirement+(d-g)*hours);peak=max(peak,requirement);bad|=unmet>1e-9
        requirement=max(policy.minimum_reserve_kwh,requirement+policy.safety_margin_kwh)
        answers.append((requirement,max(peak,requirement)<=policy.capacity_kwh+1e-8 and not bad))
    return {'required_now_kwh':max(r for r,_ in answers),'target_feasible':all(ok for _,ok in answers),
            'valid_until':iso(stop),'outage_hours':policy.outage_hours,'evaluated_starts':len(answers),
            'device_actions':False}


def prepare(snapshot,result,config,now):
    policy=Policy.parse(config.get('policy',{}));load=config['load_profiles']['minimum_kw']
    if not isinstance(load,(int,float)) or isinstance(load,bool):raise InputError('Rollende Planung benötigt konstante konfigurierte Notstromlast')
    snapshot['rolling_minimum_kw']=load
    if now-timestamp(snapshot['battery']['observed_at'])>timedelta(seconds=90):
        result.setdefault('issues',[]).append('Aktuelle Hycube-Messung älter als 90 Sekunden')
    seconds=int(config.get('rolling',{}).get('max_plan_age_seconds',3600))+int(config.get('rolling',{}).get('poll_seconds',60))
    if not result.get('issues'):
        result['rolling_reserve']=guarded_requirement(snapshot,policy,now,seconds)
    result['rolling_planned_at']=iso(now)
    return result


class Triggers:
    def __init__(self):
        self.deficit=False;self.pending=None;self.pending_since=None;self.at_limit=False;self.last_config=None;self.last_issue=None;self.invalid=False

    def check(self, observations, energy, reserve, config, now, planned_at, issue=None):
        cfg=config.get('rolling',{});reasons=[]
        margin=float(cfg.get('deficit_hysteresis_w',50));debounce=float(cfg.get('deficit_debounce_seconds',120))
        house=observations['house_power']['value'];pv=observations['hycube_pv_power']['value']+max(0,-observations['external_power']['value'])
        want=True if house-pv>margin else False if house-pv < -margin else self.deficit
        if want!=self.deficit:
            if self.pending!=want:self.pending=want;self.pending_since=now
            if (now-self.pending_since).total_seconds()>=debounce:
                self.deficit=want;self.pending=None;reasons.append('DEFIZIT_BEGINN' if want else 'DEFIZIT_ENDE')
        else:self.pending=None
        if (now-planned_at).total_seconds()>=float(cfg.get('max_plan_age_seconds',3600)):reasons.append('STUENDLICHE_PLANUNG')
        at_limit=energy<=reserve+0.02
        if at_limit and not self.at_limit:reasons.append('RESERVEGRENZE_ERREICHT')
        self.at_limit=at_limit
        if self.last_config is not None and config!=self.last_config:reasons.append('KONFIGURATION_GEAENDERT')
        self.last_config=copy.deepcopy(config)
        if issue is not None:
            if self.last_issue is not None and issue!=self.last_issue:reasons.append('NEUE_SOLARPROGNOSE')
            self.last_issue=issue
        if self.invalid:reasons.append('MESSWERTE_WIEDER_VERFUEGBAR');self.invalid=False
        return reasons


def refresh(snapshot,result,config,now,trigger):
    """Read only one existing Guardian tail and Solcast's cache issue timestamp.

    Recompute current autonomy from measured SOC, without a balancing/history
    scan or a new reserve optimization. Invalid live data never retain numbers.
    """
    policy=Policy.parse(config['policy']);soc,observations=current(config['sources']['guardian_shared'],now)
    if (now-timestamp(soc['observed_at'])).total_seconds()>90:raise InputError('Aktuelle Hycube-Messung älter als 90 Sekunden')
    observations={**snapshot['observations'],**observations,'hycube_soc':soc}
    battery=estimate_soc_energy(observations,config['battery_mapping'])
    issue=None
    ha=config['sources'].get('home_assistant',{});fs=config['sources']['forecasts']['hycube']
    if ha and fs.get('issue_entity'):
        state=ReadClient(ha['base_url'],'ha',read_token(ha,'HA_TOKEN')).read('/api/states/'+fs['issue_entity'])
        issue=state.get('state')
    reasons=trigger.check(observations,battery['energy_kwh'],result.get('rolling_reserve',{}).get('required_now_kwh',0),config,now,timestamp(result['rolling_planned_at']),issue)
    live=copy.deepcopy(result);live['battery']=battery;live['live_observed_at']=iso(now)
    live['rolling_trigger_state']={'deficit':trigger.deficit,'device_actions':False}
    if reasons:return live,reasons
    if live.get('issues') or timestamp(live['rolling_reserve']['valid_until'])<now:raise InputError('Rollende Reserve abgelaufen oder nicht berechenbar')
    fc=snapshot['forecasts']['hycube']
    if now-timestamp(fc['generated_at'])>timedelta(hours=policy.forecast_max_age_hours):raise InputError('Solarprognose veraltet')
    end=min(now+timedelta(hours=policy.horizon_hours),max(timestamp(r['end']) for r in fc['intervals']))
    n=int((end-now).total_seconds()/(policy.step_minutes*60))
    if n*policy.step_minutes<policy.outage_hours*60:raise InputError('Prognose deckt keine 24 Stunden ab')
    edges=[now+timedelta(minutes=i*policy.step_minutes) for i in range(n+1)]
    energy=resample(fc['intervals'],edges,'hycube')
    if any(v is None for v in energy):raise InputError('Lücke in aktueller Reichweitenprognose')
    dt=policy.step_minutes/60;factor=result['effective_solar_factors']['hycube']
    run=forward([v/dt*factor for v in energy],[snapshot['rolling_minimum_kw']]*n,battery['energy_kwh'],policy,dt,edges)
    live['scenarios'][0]['autonomy']=autonomy(run,{'energy_kwh':battery['energy_kwh'],'efficiency':policy.discharge_efficiency,'at':iso(now)},n*dt)
    return live,[]
