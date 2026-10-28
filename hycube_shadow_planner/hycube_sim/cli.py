"""Standalone observation runner. No device commands or service endpoints."""
import argparse
import csv
import html
import json
import math
import threading
import time
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .model import Policy, InputError, iso, timestamp, simulate, resample
from .sources import collect, SourceError, load_profile


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+"\n")


def display_values(result):
    duration='Nicht berechenbar'
    scenarios=result.get('scenarios',[])
    if scenarios and not result.get('issues'):
        a=scenarios[0].get('autonomy',{})
        if a.get('hours') is not None:
            duration=('Mindestens ' if a.get('forecast_limited') else '')+f"{a['hours']:.1f}".replace('.',',')+' Stunden'
    reserve='Nicht berechenbar'
    advice=([result['rolling_reserve']] if result.get('rolling_reserve') else result.get('reserve_advice',{}).get('scenarios',[]))
    nominal=result.get('battery',{}).get('nominal_capacity_kwh')
    if advice and nominal and advice[0].get('target_feasible') and not result.get('issues'):
        # Hycube emergency_pct is an energy share, not an absolute SOC boundary.
        # required_now_kwh already excludes the technical protection floor.
        percentage=math.ceil(1000*advice[0]['required_now_kwh']/nominal)/10
        settings=result.get('reserve_settings',{})
        maximum=100-settings.get('passive_percent',0)-result['battery'].get('technical_floor_percent',0)
        reserve=f"{percentage:.1f}".replace('.',',')+' %' if percentage<=maximum else '24 Stunden nicht erreichbar'
    if result.get('kind')=='synthetic_demo':
        duration='Beispiel: '+duration
        reserve='Beispiel: '+reserve
    return [('Aktuelle Notstromreservedauer',duration),('Notstrom – für mindestens 24 Stunden',reserve)]


def report(result):
    cards=''.join('<section><h2>'+html.escape(label)+'</h2><p>'+html.escape(value)+'</p></section>' for label,value in display_values(result))
    return ("<!doctype html><html lang='de'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='refresh' content='60'><title>Notstrom</title>"
        "<style>body{font:20px system-ui;background:#f3f6f8;color:#18313d;max-width:800px;margin:60px auto;padding:24px}"
        "section{background:white;border-radius:16px;padding:28px;margin-bottom:24px}h2{font-size:20px;font-weight:500;margin:0}p{font-size:44px;font-weight:650;margin:16px 0 0}</style>"
        +cards+'</html>')


def markdown_report(result):
    return '\n\n'.join('## '+label+'\n\n**'+value+'**' for label,value in display_values(result))+'\n'


def archive(output, snapshot, result):
    output = Path(output)
    folder = output/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder/'input.json', snapshot)
    write_json(folder/'result.json', result)
    rendered = report(result)
    (folder/'report.html').write_text(rendered)
    markdown=markdown_report(result)
    (folder/'report.md').write_text(markdown)
    for filename, content in [('latest.json',json.dumps(result,ensure_ascii=False,indent=2)),('index.html',rendered),('index.md',markdown)]:
        tmp = output/(filename+'.tmp')
        tmp.write_text(content)
        tmp.replace(output/filename)
    return folder


def demo(now):
    p = Policy.parse({'capacity_kwh':12, 'minimum_reserve_kwh':2, 'terminal_reserve_kwh':1,
                      'safety_margin_kwh':1, 'profile_confirmed':True,'power_limits_confirmed':True})
    forecasts = {}
    for key, peak in [('hycube',2.4),('secondary',1.8)]:
        rows = []
        for i in range(72):
            a = now+timedelta(hours=i)
            hour = a.astimezone(__import__('zoneinfo').ZoneInfo(p.timezone)).hour+.5
            power = peak*max(0,math.sin((hour-8)/9*math.pi)) if 8 <= hour <=17 else 0
            rows.append({'start':iso(a),'end':iso(a+timedelta(hours=1)),'energy_kwh':power*(.45 if i//24==1 else .8)})
        forecasts[key]={'generated_at':iso(now),'intervals':rows,'source':'synthetic_demo'}
    s = {'kind':'synthetic_demo','captured_at':iso(now),'battery':{'energy_kwh':8,'observed_at':iso(now),'mapping_confirmed':True},
         'forecasts':forecasts,'loads':{'minimum':load_profile(now,p,.18,'demo'), 'normal':load_profile(now,p,.55,'demo')}}
    return p,s


def verify(run, observations, output):
    """Compare only complete, realized intervals; CSV contains measured interval energies."""
    run = Path(run)
    snapshot=json.loads((run/'input.json').read_text())
    result=json.loads((run/'result.json').read_text())
    now=datetime.now(timezone.utc)
    issued=timestamp(result['issued_at'])
    with open(observations, newline='') as f:
        measured=list(csv.DictReader(f))
    answer={'issued_at':iso(now),'forecast_run':str(run),'comparisons':{},
            'note':'Vergleich mit ursprünglicher Rohprognose; kein Nachweis der Versorgung bei realem Netzausfall.'}
    for name in ('hycube','secondary'):
        errors=[]
        selected=sorted((row for row in measured if row.get(name+'_kwh','')!=''),key=lambda row:timestamp(row['start']))
        previous_end=None
        for row in selected:
            a,b=timestamp(row['start']),timestamp(row['end'])
            if previous_end is not None and a<previous_end:
                raise InputError('Überlappende Messintervalle')
            previous_end=b
            value=row.get(name+'_kwh','')
            if a<issued or b>now or b<=a or value=='':
                continue
            value=float(value)
            if not math.isfinite(value) or value<0:
                raise InputError('Messenergie muss endlich und nicht negativ sein')
            prediction=resample(snapshot.get('forecasts',{}).get(name,{}).get('intervals',[]),[a,b],name)[0]
            if prediction is not None:
                errors.append({'start':iso(a),'end':iso(b),'forecast_kwh':prediction,'measured_kwh':value,'error_kwh':prediction-value})
        answer['comparisons'][name]={'count':len(errors),'mae_kwh':sum(abs(x['error_kwh']) for x in errors)/len(errors) if errors else None,
                                    'bias_kwh':sum(x['error_kwh'] for x in errors)/len(errors) if errors else None,'intervals':errors}
    write_json(Path(output),answer)
    return answer


def main():
    parser=argparse.ArgumentParser(description='Hycube Notstrom-Simulator: ausschließlich lesend')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ('demo','run','serve','observe'):
        p=sub.add_parser(command)
        p.add_argument('--output',default='runs')
        if command!='demo': p.add_argument('--config',required=True)
        if command=='run': p.add_argument('--snapshot')
        if command in ('serve','observe'):
            p.add_argument('--interval',type=int,default=900)
            p.add_argument('--duration-hours',type=float,default=0,help='0: unbounded; otherwise stop after this many hours')
        if command=='serve':
            p.add_argument('--host',default='127.0.0.1')
            p.add_argument('--port',type=int,default=8091)
    p=sub.add_parser('export-observations');p.add_argument('--history',required=True);p.add_argument('--output',required=True);p.add_argument('--max-gap-minutes',type=float,default=35)
    p=sub.add_parser('verify');p.add_argument('--run',required=True);p.add_argument('--observations',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if args.command=='export-observations':
            from .evaluation import observations_from_history
            print(json.dumps(observations_from_history(args.history,args.output,args.max_gap_minutes)));return
        if args.command=='verify':
            answer=verify(args.run,args.observations,args.output)
            print(json.dumps({k:v['count'] for k,v in answer['comparisons'].items()}));return
        if args.command in ('serve','observe') and (not math.isfinite(args.duration_hours) or args.duration_hours<0 or args.duration_hours>744):
            raise InputError('Beobachtungsdauer muss zwischen 0 und 744 Stunden liegen')
        config={} if args.command=='demo' else json.loads(Path(args.config).read_text())
        last_snapshot=None;last_result=None
        def publish_display(result,runtime_config):
            from .ha_display import publish
            try:publish(result,runtime_config)
            except (OSError,ValueError):print('HA-Anzeige nicht aktualisiert; keine Geräteaktion.',flush=True)
        def live_files(result):
            output=Path(args.output)
            for name,content in [('latest.json',json.dumps(result,ensure_ascii=False,indent=2)),('index.html',report(result)),('index.md',markdown_report(result))]:
                tmp=output/(name+'.tmp');tmp.write_text(content);tmp.replace(output/name)
        def cycle(reasons=None):
            nonlocal last_snapshot,last_result
            now=datetime.now(timezone.utc)
            if args.command=='demo': policy,snapshot=demo(now)
            else:
                runtime_config=json.loads(Path(args.config).read_text())
                policy=Policy.parse(runtime_config.get('policy',{}))
                snapshot=json.loads(Path(args.snapshot).read_text()) if getattr(args,'snapshot',None) else collect(runtime_config,policy,now)
            if args.command!='demo' and runtime_config.get('solar_learning',{}).get('enabled'):
                from .solar_learning import update_learning
                import sqlite3
                try:snapshot['solar_learning']=update_learning(snapshot,runtime_config['solar_learning'],now,policy.solar_correction_factor)
                except (InputError,OSError,ValueError,KeyError,sqlite3.Error):
                    snapshot['solar_learning']={plant:{'factor':policy.solar_correction_factor,'status':'LEARNING_READ_ERROR_INITIAL_FACTOR'} for plant in ('hycube','secondary')}
            result=simulate(snapshot,policy,now)
            if args.command!='demo' and runtime_config.get('rolling',{}).get('enabled'):
                from .rolling import prepare
                prepare(snapshot,result,runtime_config,now)
                result['planning_triggers']=reasons or ['START']
            last_snapshot,last_result=snapshot,result
            folder=archive(args.output,snapshot,result)
            if args.command!='demo':publish_display(result,runtime_config)
            print(result['status']+' · '+str(folder),flush=True)
            return result
        from .rolling import Triggers,refresh
        trigger=Triggers()
        def checked_cycle(current_config):
            if not current_config.get('rolling',{}).get('enabled'):return cycle(),True
            live,reasons=refresh(last_snapshot,last_result,current_config,datetime.now(timezone.utc),trigger)
            if reasons:return cycle(reasons),True
            live_files(live);publish_display(live,current_config)
            return live,False
        cycle()
        if args.command=='observe':
            if args.interval<60:
                raise InputError('Beobachtung benötigt Intervall >=60 Sekunden')
            session={'pid':os.getpid(),'started_at':iso(datetime.now(timezone.utc)),
                'planned_end':iso(datetime.now(timezone.utc)+timedelta(hours=args.duration_hours)) if args.duration_hours else None,
                'interval_seconds':args.interval,'state':'RUNNING','completed_cycles':1,'failure_count':0}
            session_path=Path(args.output)/'session.json'
            write_json(session_path,session)
            deadline=time.monotonic()+args.duration_hours*3600 if args.duration_hours else None
            try:
                while deadline is None or time.monotonic()<deadline:
                    current_config=json.loads(Path(args.config).read_text())
                    interval=int(current_config.get('rolling',{}).get('poll_seconds',60)) if current_config.get('rolling',{}).get('enabled') else args.interval
                    if interval<60:raise InputError('Messprüfung benötigt mindestens 60 Sekunden')
                    time.sleep(interval if deadline is None else min(interval,max(0,deadline-time.monotonic())))
                    if deadline is not None and time.monotonic()>=deadline: break
                    try:
                        current_config=json.loads(Path(args.config).read_text())
                        result,full=checked_cycle(current_config)
                        session['completed_cycles']+=int(full)
                        session['poll_seconds']=interval
                        session['last_check_at']=iso(datetime.now(timezone.utc))
                        session['last_status']=result['status'];session['last_cycle_at']=result['issued_at']
                    except (InputError,SourceError,ValueError,KeyError,OSError,TypeError):
                        session['failure_count']+=1
                        if current_config.get('rolling',{}).get('enabled'):
                            trigger.invalid=True
                            invalid=dict(last_result);invalid['issues']=['Aktuelle Prüfung fehlgeschlagen'];live_files(invalid);publish_display(invalid,current_config)
                        print('Leselauf fehlgeschlagen; keine Geräteaktion.',flush=True)
                    write_json(session_path,session)
            except KeyboardInterrupt:
                session['state']='INTERRUPTED'
            finally:
                if session['state']=='RUNNING': session['state']='FINISHED'
                session['ended_at']=iso(datetime.now(timezone.utc));write_json(session_path,session)
            return
        if args.command!='serve': return
        if args.interval<60: raise InputError('Leseintervall mindestens 60 Sekunden')
        stop=threading.Event()
        def worker():
            while True:
                current_config=json.loads(Path(args.config).read_text())
                interval=int(current_config.get('rolling',{}).get('poll_seconds',60)) if current_config.get('rolling',{}).get('enabled') else args.interval
                if stop.wait(max(60,interval)):break
                try:checked_cycle(current_config)
                except (InputError,SourceError,ValueError,KeyError,OSError,TypeError):
                    trigger.invalid=True
                    invalid=dict(last_result);invalid['issues']=['Aktuelle Prüfung fehlgeschlagen']
                    live_files(invalid);publish_display(invalid,current_config)
                    print('Leseprüfung fehlgeschlagen; keine Geräteaktion.',flush=True)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths={'/':'index.html','/index.html':'index.html','/latest.json':'latest.json'}
                if self.path not in paths: self.send_error(404);return
                data=(Path(args.output)/paths[self.path]).read_bytes()
                self.send_response(200);self.send_header('Content-Type','application/json' if self.path.endswith('.json') else 'text/html; charset=utf-8')
                self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
            def log_message(self,*args): pass
        threading.Thread(target=worker,daemon=True).start()
        server=ThreadingHTTPServer((args.host,args.port),Handler)
        def deadline():
            if not stop.wait(args.duration_hours*3600):
                stop.set();server.shutdown()
        if args.duration_hours:
            threading.Thread(target=deadline,daemon=True).start()
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: stop.set();server.server_close()
    except (InputError,SourceError,ValueError,KeyError,OSError,TypeError) as exc:
        parser.exit(2,'Simulation abgebrochen: '+str(exc)+'\n')
