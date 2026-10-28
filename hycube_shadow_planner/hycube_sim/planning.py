"""Read-only display recommendations. Never imports device/network writers."""
from datetime import timedelta
from math import ceil
from statistics import median
from .model import timestamp, iso, number, InputError


def autonomy(run, start, horizon_hours):
    """First interruption under interval-constant power, including partial interval."""
    gap = next((r for r in run['rows'] if r['unserved_ac_kwh'] > 1e-8), None)
    if gap is None:
        return {'status':'MINDESTENS', 'hours':horizon_hours,
                'until':run['rows'][-1]['end'], 'forecast_limited':True}
    i=run['rows'].index(gap)
    before=run['rows'][i-1]['battery_energy_kwh'] if i else start['energy_kwh']
    net=max(0, gap['minimum_load_kw']-gap['hycube_pv_kw'])
    # A power-limited interruption starts immediately, irrespective of energy.
    offset=0 if gap['discharge_power_limit_exceeded'] else before*start['efficiency']/net if net else 0
    seconds=(timestamp(gap['end'])-timestamp(gap['start'])).total_seconds()
    until=timestamp(gap['start'])+timedelta(seconds=min(seconds,max(0,offset*3600)))
    return {'status':'ERSTE_UNTERBRECHUNG', 'hours':(until-timestamp(start['at'])).total_seconds()/3600,
            'until':iso(until), 'forecast_limited':False,
            'precision':'Intervallkonstante Prognose; keine sekundengenaue Vorhersage'}


def reserve_advice(result):
    battery=result.get('battery',{}); nominal=battery.get('nominal_capacity_kwh');floor=battery.get('technical_floor_percent')
    scenarios=result.get('reserve_requirements_only',[])
    out={'status':'NUR_ANZEIGE','device_actions':False,'scenarios':[]}
    for s in scenarios:
        curve=[]
        for row in s['curve']:
            e=row['required_energy_kwh']
            feasible=(e<=result['policy']['capacity_kwh'] if result['policy']['capacity_kwh'] is not None else False) and row['maximum_intermediate_requirement_kwh']<= (result['policy']['capacity_kwh'] or 0) and not row['power_limit_exceeded']
            percent=ceil((floor+100*e/nominal)*10)/10 if nominal and floor is not None else None
            curve.append({**row,'normal_discharge_floor_percent':percent if feasible else None,'target_feasible':feasible})
        current=curve[0]
        out['scenarios'].append({'name':s['name'],'curve':curve,'required_now_kwh':current['required_energy_kwh'],
            'normal_discharge_floor_percent':current['normal_discharge_floor_percent'],
            'target_feasible':current['target_feasible'],
            'highest_required_kwh':max(r['required_energy_kwh'] for r in curve),
            'hold_without_assumed_recharge_percent':max((r['normal_discharge_floor_percent'] for r in curve),default=None) if all(r['target_feasible'] and r['normal_discharge_floor_percent'] is not None for r in curve) else None,
            'meaning':'Nur Vorschlag für normale Entladegrenze; Notstrom-Batterieschutz bleibt unverändert. Keine Gerätefreigabe.'})
    return out


def _balancing(history, now):
    """Evaluate documented V1.1 common pack window using explicit, complete evidence.

    No fabricated module identities, healthy temperatures, or historical full charges.
    Input schema and fixture documented in BALANCING_INPUT.json.
    """
    unknown={'version':'V1.1','status':'NICHT_BEURTEILBAR','recommendation':'Keine belegte Ladeempfehlung; vollständige Guardian-Zellhistorie fehlt.', 'modules':[], 'device_actions':False}
    if not history: return unknown
    try:
        step=number(history['sample_interval_seconds'],'sample_interval_seconds',1,300)
        serials=history['module_serials']
        if not serials or len(set(serials))!=len(serials): raise InputError('Modulidentitäten fehlen oder doppelt')
        frames=history['frames']; times=[timestamp(f['at']) for f in frames]
        if any(b<=a for a,b in zip(times,times[1:])) or any(t>now for t in times): raise InputError('Zeitfolge ungültig')
        last_full=max((t for f,t in zip(frames,times) if f['pack_soc_percent']>=100), default=None)
        continuous=bool(times) and all((b-a).total_seconds()<=3*step for a,b in zip(times,times[1:])) and len(times)*step>=.9*(times[-1]-times[0]).total_seconds()
        long_without_full=continuous and (now-times[-1]).total_seconds()<=3*step and (times[-1]-times[0]).total_seconds()>14*86400 and (last_full is None or (now-last_full).total_seconds()>14*86400)
        maintenance={'last_observed_full_charge':iso(last_full) if last_full else None,
            'more_than_14_days_without_full_charge_proven':long_without_full,
            'recommendation':'Vollladefenster prüfen; Solar bevorzugen, manuelle Ergänzung erst nach Energie- und Haltezeitplanung.' if long_without_full else 'Kein belegter zeitlicher Wartungsbedarf; fehlende Historie bedeutet nicht, dass keine Vollladung stattfand.'}
        unknown['maintenance']=maintenance
        if long_without_full: unknown['recommendation']=maintenance['recommendation']
        windows=[];window=[]; last=None;valid_seconds=0
        for f,t in zip(frames,times):
            soc=number(f['pack_soc_percent'],'pack_soc_percent',0,100)
            elapsed=(t-last).total_seconds() if last is not None else 0
            if elapsed>3*step: window=[];valid_seconds=0
            last=t
            if soc<95: window=[];valid_seconds=0;continue
            if not window and soc<98: continue
            if window and elapsed<=step*1.5: valid_seconds+=min(elapsed,step)
            window.append((t,f))
            if valid_seconds>=7200:
                windows.append(window[:]);window=[];valid_seconds=0
        if not windows: return {**unknown,'reason':'Kein belegtes gemeinsames zweistündiges Vollfenster; Datenlücken zählen nicht als Haltezeit.'}
        window=windows[-1]; end=window[-1][0]
        output=[]
        for serial in serials:
            vals=[];safe=True
            for t,f in window:
                modules={m['serial']:m for m in f['modules']}
                if len(modules)!=len(f['modules']): raise InputError('Doppelte Modulidentität')
                m=modules.get(serial)
                if not m: continue
                cells=m['cells_mv']
                if len(cells)!=15: continue
                cells=[number(v,'cell_mv',1500,4500) for v in cells]
                safe &= m.get('temperature_ok') is True and m.get('protection_clear') is True
                vals.append((t,max(cells)-min(cells)))
            def covered(a,b):
                ts=[t for t,v in vals if a<=t<=b]
                return bool(ts) and len(ts)*step >= .9*(b-a).total_seconds() and (ts[0]-a).total_seconds()<=3*step and (b-ts[-1]).total_seconds()<=3*step and all((y-x).total_seconds()<=3*step for x,y in zip(ts,ts[1:]))
            begin=window[0][0]
            first=[v for t,v in vals if t<=begin+timedelta(minutes=5)]
            tail=[v for t,v in vals if t>=end-timedelta(minutes=15)]
            voltage_valid=covered(begin,begin+timedelta(minutes=5)) and covered(end-timedelta(minutes=15),end) and covered(begin,end)
            valid=voltage_valid and safe
            initial=median(first) if first else None; final=median(tail) if tail else None
            state='NICHT_BEURTEILBAR'
            if valid:
                state='ZIEL_ERREICHT' if max(tail)<=30 else 'FORTSCHRITT' if initial and final<=.75*initial else 'NICHT_AUSREICHEND'
            output.append({'serial':serial,'status':state,'initial_median_mv':initial,'end_median_mv':final,
                'end_max_mv':max(tail) if tail else None,'voltage_evidence_complete':voltage_valid,'technical_review':bool(voltage_valid and final>100)})
        full=any(f['pack_soc_percent']>=100 for t,f in window)
        status='ZIEL_ERREICHT' if full and all(m['status']=='ZIEL_ERREICHT' for m in output) else 'NICHT_BEURTEILBAR' if not full or any(m['status']=='NICHT_BEURTEILBAR' for m in output) else 'WEITERER_ABGLEICH_PRUEFEN'
        stale=(now-end).total_seconds()>86400
        return {'version':'V1.1','status':status,'window_start':iso(window[0][0]),'window_end':iso(end),'historical_result':True,'maintenance':maintenance,
                'full_charge_observed':full,'modules':output,'device_actions':False,
                'recommendation':'Technische Prüfung statt endloser Ladeversuche.' if any(m['technical_review'] for m in output) else 'Erneutes qualifiziertes Vollladefenster prüfen; bevorzugt Solar, gegebenenfalls manuelle Ladeergänzung nach Haltezeitplanung.' if status=='WEITERER_ABGLEICH_PRUEFEN' else maintenance['recommendation'],
                'latest_window_older_than_24h':stale}
    except (KeyError,TypeError,ValueError,InputError):
        return {**unknown,'reason':'Unvollständige oder ungültige V1.1-Eingangsdaten'}


def balancing(history, now):
    result=_balancing(history,now)
    if not history:return result
    evidence=history.get('maintenance_evidence')
    if evidence:result['maintenance']=evidence
    result['acquisition']=history.get('acquisition',{})
    result['latest_modules']=history.get('latest_modules',[])
    reasons=[]
    if result.get('maintenance',{}).get('more_than_14_days_without_full_charge_proven'):
        reasons.append('Mehr als 14 belegte Tage ohne Vollladung.')
    if result.get('full_charge_observed'):
        if any(m.get('technical_review') for m in result['modules']):
            result['full_charge_need']={'status':'TECHNISCHE_PRUEFUNG','reasons':['Über 100 mV Endmedian im belegten oberen Fenster; keine endlosen Wiederholungsladungen.'],'device_actions':False}
            return result
        if any(m.get('voltage_evidence_complete') and m['end_max_mv']>30 for m in result['modules']):
            reasons.append('Zellspannungsziel nach belegtem oberen Ladefenster nicht erreicht.')
    if reasons:
        result['full_charge_need']={'status':'VOLLLADUNG_PRUEFEN','reasons':reasons,'device_actions':False}
        result['recommendation']='Vollladung anhand der historischen Befunde prüfen; kein automatischer Ladeauftrag.'
    else:
        result['full_charge_need']={'status':'KEIN_BEDARF_BELEGT' if result.get('status')=='ZIEL_ERREICHT' else 'NICHT_BEURTEILBAR','reasons':['Nur historische Befunde; fehlende Abdeckung belegt keine ausgebliebene Vollladung.'],'device_actions':False}
    return result
