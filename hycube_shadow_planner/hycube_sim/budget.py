"""Conditional reserve/charge budgets, not a forecast of normal house consumption.

F[i] = max(R[i], F[i+1]-C[i]): lowest stock that can meet all following
24-hour reserve requirements if the displayed PV allocation is actually delivered.
C is a physical forecast ceiling, not an observed/guaranteed surplus.
"""
from .model import required_energy,iso
from math import ceil
from datetime import timedelta


def plan_budget(pv,minimum,energy,policy,edges,battery,balancing):
    dt=policy.step_minutes/60;count=int(policy.outage_hours/dt)
    end=len(pv)-count
    reserve=[];possible=True
    for i in range(end+1):
        req,peak,power_failure=required_energy(pv[i:i+count],minimum[i:i+count],policy,dt)
        reserve.append(req)
        possible &= max(req,peak)<=policy.capacity_kwh+1e-8 and not power_failure
    charge=[min(max(0,pv[i]),policy.charge_limit_kw)*policy.charge_efficiency*dt for i in range(end)]
    historical_need=balancing.get('full_charge_need',{}).get('status','NICHT_BEURTEILBAR')
    targets=reserve[:];full_slot=None;hold_steps=int(2/dt)
    if historical_need=='VOLLLADUNG_PRUEFEN' and end>=hold_steps:
        # Keep maintenance energy in the same stock account as emergency reserve.
        # Prefer the first reachable solar full-charge point; otherwise show the
        # explicit additional-charge requirement at the end of this planning window.
        stock=energy
        for j in range(end-hold_steps+1):
            if stock>=policy.capacity_kwh-1e-8:full_slot=j;break
            if j<end:stock=min(policy.capacity_kwh,stock+charge[j])
        if full_slot is None:full_slot=end-hold_steps
        for j in range(full_slot,min(end,full_slot+hold_steps)+1):targets[j]=policy.capacity_kwh
    floor=targets[:]
    for i in range(end-1,-1,-1):floor[i]=max(targets[i],floor[i+1]-charge[i])
    available=max(0,energy-floor[0]) if possible else 0
    nominal=battery.get('nominal_capacity_kwh');technical=battery.get('technical_floor_percent')
    def percent(e):
        return ceil((technical+100*e/nominal)*10)/10 if nominal and technical is not None and e<=policy.capacity_kwh+1e-8 else None
    rows=[]
    for i in range(end):
        # Target trajectory, not a normal-house stock prediction. No load is invented.
        need=max(0,floor[i+1]-floor[i])
        rows.append({'start':iso(edges[i]),'end':iso(edges[i+1]),
            'reserve_24h_dc_kwh':round(reserve[i],6),'conditional_floor_dc_kwh':round(floor[i],6),
            'conditional_floor_soc_percent':percent(floor[i]),
            'required_stored_solar_dc_kwh':round(need,6),
            'required_solar_to_battery_ac_kwh':round(need/policy.charge_efficiency,6),
            'maximum_forecast_charge_dc_kwh':round(charge[i],6),
            'additional_release_from_falling_floor_dc_kwh':round(max(0,floor[i]-floor[i+1]),6),
            'solar_phase':pv[i]>1e-6})
    phases=[]
    for row in rows:
        kind='PV-Ertragsphase' if row['solar_phase'] else 'Phase ohne PV-Ertrag'
        if not phases or phases[-1]['phase']!=kind:
            phases.append({'phase':kind,'start':row['start'],'end':row['end'],
                'required_stored_solar_dc_kwh':0,'required_solar_to_battery_ac_kwh':0,
                'maximum_forecast_charge_dc_kwh':0,'additional_release_from_falling_floor_dc_kwh':0,
                'floor_at_start_soc_percent':row['conditional_floor_soc_percent']})
        phase=phases[-1];phase['end']=row['end']
        for key in ('required_stored_solar_dc_kwh','required_solar_to_battery_ac_kwh','maximum_forecast_charge_dc_kwh','additional_release_from_falling_floor_dc_kwh'):phase[key]+=row[key]
    if phases and available:
        phases[0]['initial_release_dc_kwh']=round(available,6)
    deficit=max(0,floor[0]-energy)
    # Earliest deadline if none of the freeable stock is used and all modeled PV
    # can be allocated. This is a lower bound, never a promised actual grid bill.
    stock=energy;deadline=None;extra=0
    for i in range(end+1):
        gap=max(0,reserve[i]-stock)
        if gap>1e-8:
            if deadline is None:deadline=iso(edges[i])
            extra+=gap;stock+=gap
        if i<end:stock=min(policy.capacity_kwh,stock+charge[i])
    maintenance={'historical_need':historical_need,'status':'KEIN_LADEAUFTRAG','device_actions':False,
        'reason':'Vollladung nur bei historischem Bedarf priorisieren; unbekannter Bedarf ist kein Ladeauftrag.'}
    if historical_need=='VOLLLADUNG_PRUEFEN':
        maintenance.update({'status':'SOLARE_VOLLLADUNG_BEDINGT_MOEGLICH' if deficit<=1e-8 else 'MANUELLE_ERGAENZUNG_PRUEFEN',
            'model_full_target_at':iso(edges[full_slot]) if full_slot is not None else None,'hold_hours':2,
            'missing_to_full_now_dc_kwh':max(0,policy.capacity_kwh-energy),
            'grid_charge_lower_bound_dc_kwh':max(0,deficit),
            'reason':'Volllade- und Halteziel im selben Energiebudget wie die Reserve; keine doppelte Freigabe. PV-Zuteilung erforderlich. Hauslast/Netzbedarf während Haltezeit unbekannt; kein Balancing-Erfolgsnachweis.'})
    return {'status':'BEDINGTER_ENERGIEPLAN' if possible else '24H_ZIEL_NICHT_DURCHGAENGIG_ERREICHBAR',
        'device_actions':False,'normal_load_simulation':False,'global_grid_optimum_claimed':False,
        'outage_target_hours':policy.outage_hours,'planning_until':iso(edges[end]),
        'now':{'reserve_24h_dc_kwh':round(reserve[0],6),'conditional_floor_dc_kwh':round(floor[0],6),
            'conditional_floor_soc_percent':percent(floor[0]) if possible else None,
            'maximum_release_now_dc_kwh':round(available,6),'maximum_release_now_house_ac_kwh':round(available*policy.discharge_efficiency,6),
            'additional_charge_needed_dc_kwh':round(deficit,6)},
        'phases':phases,'target_curve':rows,
        'manual_reserve_charge':{'lower_bound_dc_kwh':round(extra,6),'lower_bound_ac_kwh':round(extra/policy.charge_efficiency,6),'first_deadline':deadline,'device_actions':False},
        'balancing_charge':maintenance,
        'conditions':['PV-Ladeobergrenze aus AB1 mit konfiguriertem Solarfaktor; keine behauptete freie Solarenergie nach Hausverbrauch.',
          'Absenkung ist an die ausgewiesene rechtzeitige PV-Zuteilung gebunden; vor weiterer Entladung mit aktuellen Werten neu prüfen.',
          'Budget ist maximal freigebbare Energie, keine Vorhersage tatsächlich benötigter Hausenergie.',
          'Keine doppelte Nutzung: der bedingte Plan beginnt nach Freigabe des ausgewiesenen Überschusses auf seiner Sollkurve.',
          'Netz-Nachladung nur als Untergrenze bei maximaler PV-Zuteilung; tatsächlicher Netzverbrauch ohne Hauslastprofil nicht berechenbar.',
          'Reserve zuerst, historisch begründeter Zellabgleich danach; PV bevorzugen, keine vorsorgliche Netz-Vollladung ohne Bedarf.']}
