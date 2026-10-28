"""Publish only two owned display sensors. No actions, services or settings."""
import json
import os
from urllib.request import Request, build_opener, ProxyHandler
from .sources import NoRedirect, ReadClient

ENTITIES=('sensor.hycube_planung_notstromreservedauer','sensor.hycube_planung_notstrom_24h')


def payloads(result):
    from .cli import display_values
    values=display_values(result)
    duration=result.get('scenarios',[{}])[0].get('autonomy',{}) if result.get('scenarios') else {}
    reserve=result.get('rolling_reserve') or next(iter(result.get('reserve_advice',{}).get('scenarios',[])),{})
    import math
    nominal=result.get('battery',{}).get('nominal_capacity_kwh')
    percentage=math.ceil(1000*reserve['required_now_kwh']/nominal)/10 if nominal and reserve.get('required_now_kwh') is not None else None
    states=[round(duration['hours'],1) if duration.get('hours') is not None and 'berechenbar' not in values[0][1] else 'unavailable',
            percentage if percentage is not None and values[1][1].endswith('%') else 'unavailable']
    common={'source':'hycube_shadow_planner','device_actions':False,'calculated_at':result.get('rolling_planned_at',result.get('issued_at')),'observed_at':result.get('live_observed_at',result.get('issued_at'))}
    factors={}
    for plant,label in [('hycube','AB1'),('secondary','AB2')]:
        learning=result.get('solar_learning',{}).get(plant,{})
        predicted=learning.get('raw_forecast_kwh');actual=learning.get('actual_kwh')
        calculated=100*actual/predicted if isinstance(predicted,(int,float)) and predicted>0 and isinstance(actual,(int,float)) else None
        effective=result.get('effective_solar_factors',{}).get(plant)
        factors[label]={'calculated_percent':round(calculated,2) if calculated is not None else None,
            'effective_percent':round(100*effective,2) if effective is not None else None,
            'status':learning.get('status','UNKNOWN'),'valid_hours':learning.get('valid_hours',0),
            'provisional':not bool(learning.get('last_learned_at')),
            'training_until':learning.get('training_until')}
    common['solar_factors']=factors
    return [{'state':str(state),'attributes':{**common,'friendly_name':values[i][0],'unit_of_measurement':('h','%')[i],
            'icon':('mdi:home-battery','mdi:battery-lock')[i],'display_value':values[i][1],
            **({'forecast_limited':duration.get('forecast_limited',False)} if i==0 else {'passive_and_protection_unchanged':True})}} for i,state in enumerate(states)]


def publish(result,config,opener=None):
    if not config.get('ha_display',{}).get('enabled'):return
    ha=config['sources']['home_assistant']
    # Reuse the strict base URL validation; entity IDs are constants, never config.
    client=ReadClient(ha['base_url'],'ha')
    token=os.environ.get(ha.get('token_env','HA_TOKEN'),'')
    if not token:raise ValueError('HA-Anzeige benötigt vorhandenen Zugriff')
    opener=opener or build_opener(ProxyHandler({}),NoRedirect())
    for entity,body in zip(ENTITIES,payloads(result)):
        request=Request(client.base+'/api/states/'+entity,data=json.dumps(body,allow_nan=False).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
        with opener.open(request,timeout=12) as response:
            if response.status not in (200,201):raise OSError('HA-Anzeigewert nicht gespeichert')
