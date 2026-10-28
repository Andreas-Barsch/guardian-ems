"""Measured power integration with explicit gap rejection, never gap filling."""
import csv
import json
from pathlib import Path
from datetime import timedelta, datetime, timezone
from .model import timestamp, iso, number, InputError


def observations_from_history(history, destination, max_gap_minutes=35):
    gap=number(max_gap_minutes,'max_gap_minutes',.1,120)
    samples={'hycube':{},'secondary':{}}
    keys={'hycube':'hycube_pv_power','secondary':'secondary_pv_power'}
    now=datetime.now(timezone.utc)
    for path in sorted(Path(history).glob('*/input.json')):
        snapshot=json.loads(path.read_text())
        if snapshot.get('kind')!='live': continue
        captured=timestamp(snapshot['captured_at'])
        for plant,key in keys.items():
            value=snapshot.get('observations',{}).get(key)
            if not value or value.get('unit')!='kW': continue
            t=timestamp(value['observed_at'])
            # Stale state values are not treated as new physical samples.
            if abs((captured-t).total_seconds())>gap*60 or t>now: continue
            power=number(value['value'],key)
            samples[plant][(t,value['source_entity'])]=power
    rows=[]
    for plant,points in samples.items():
        by_source={}
        for (t,source),v in points.items(): by_source.setdefault(source,[]).append((t,v))
        if len(by_source)>1:
            raise InputError('Messquelle im Archiv gewechselt: getrennte Archive auswerten')
        ordered=sorted(next(iter(by_source.values()),[]))
        for (a,va),(b,vb) in zip(ordered,ordered[1:]):
            if timedelta(0)<b-a<=timedelta(minutes=gap):
                rows.append({'start':iso(a),'end':iso(b),plant+'_kwh':(va+vb)/2*(b-a).total_seconds()/3600})
    with open(destination,'w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=['start','end','hycube_kwh','secondary_kwh'])
        writer.writeheader();writer.writerows(sorted(rows,key=lambda x:x['start']))
    return {'interval_count':len(rows),'method':'trapezoidal estimate, no bridging gaps','max_gap_minutes':gap}
