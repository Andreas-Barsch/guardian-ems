from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from version import GUARDIAN_VERSION, DIAGNOSTIC_ENGINE_VERSION

OPTIONS_FILE = Path('/data/options.json')
CONFIG_HISTORY_FILE = Path('/share/guardian_battery/config_history.jsonl')
SUPERVISOR = 'http://supervisor'
TOKEN = os.environ.get('SUPERVISOR_TOKEN', '')

DEFAULTS = {
 'serial_port':'auto','baudrate':115200,'poll_interval_seconds':10,'module_count':6,'command':'pwr','command_timeout_seconds':5,
 'mqtt_topic_prefix':'guardian','publish_discovery':True,'warning_cell_delta_mv':30,'critical_cell_delta_mv':80,
 'warning_soc_deviation_pct':10,'critical_soc_deviation_pct':30,'missing_module_is_critical':True,'raw_log':False,'detailed_log':True,
 'trend_window_minutes':60,'trend_min_change_mv':10,'incident_hold_minutes':30,'cell_diagnostics_enabled':True,
 'cell_diagnostics_interval_seconds':60,'cell_diag_low_soc_percent':30,'cell_diag_high_soc_percent':80,'cell_diag_charge_current_a':0.8,
 'cell_diag_discharge_current_a':0.8,'cell_diag_min_phase_samples':30,'cell_diag_confidence_medium_samples':120,
 'cell_diag_confidence_high_samples':600,'cell_diag_observe_deviation_mv':10,'cell_diag_warning_deviation_mv':20,
 'cell_diag_critical_deviation_mv':40,'cell_diag_history_max_samples':8640,'bms_stat_interval_seconds':3600,
 'cell_diag_discharge_observe_deviation_mv':10,'cell_diag_discharge_warning_deviation_mv':20,'cell_diag_discharge_critical_deviation_mv':40,
 'cell_diag_low_observe_deviation_mv':10,'cell_diag_low_warning_deviation_mv':20,'cell_diag_low_critical_deviation_mv':40,
 'cell_diag_charge_observe_deviation_mv':10,'cell_diag_charge_warning_deviation_mv':20,'cell_diag_charge_critical_deviation_mv':40,
 'cell_diag_high_observe_deviation_mv':10,'cell_diag_high_warning_deviation_mv':20,'cell_diag_high_critical_deviation_mv':40,
}

# group, label, unit, min, max, step, consequence, level
META = {
 'module_count':('Anlage','Installierte Batteriemodule','Module',1,6,1,'Ändert die Soll-Topologie. Nur Module 1 bis zur eingestellten Anzahl werden erwartet; höhere Modulnummern dürfen keine Missing-/Unavailable-Warnung erzeugen.','normal'),
 'poll_interval_seconds':('Anlage','BMS-Abfrageintervall','s',5,3600,1,'Kleinere Werte erhöhen Aktualität und serielle Last; größere Werte verzögern Status-, Alarm- und Trendreaktionen.','normal'),
 'warning_cell_delta_mv':('Bewertungsgrenzen','Stack-Warnschwelle Zellspreizung','mV',1,1000,1,'Niedrigere Werte erzeugen früher Warnungen aus der allgemeinen Zellspreizung; höhere Werte machen diese Bewertung toleranter.','normal'),
 'critical_cell_delta_mv':('Bewertungsgrenzen','Stack-Kritischschwelle Zellspreizung','mV',1,1000,1,'Niedrigere Werte führen früher zu kritischen Alarmen; muss oberhalb der Warnschwelle liegen.','normal'),
 'warning_soc_deviation_pct':('Bewertungsgrenzen','SOC-Abweichung Warnung','%',1,100,1,'Bestimmt, ab welcher Modul-SOC-Abweichung Guardian warnt.','normal'),
 'critical_soc_deviation_pct':('Bewertungsgrenzen','SOC-Abweichung kritisch','%',1,100,1,'Bestimmt, ab welcher Modul-SOC-Abweichung Guardian kritisch bewertet; muss oberhalb der Warnschwelle liegen.','normal'),
 'missing_module_is_critical':('Bewertungsgrenzen','Fehlendes erwartetes Modul kritisch','bool',None,None,None,'Aktiviert: fehlende konfigurierte Module werden kritisch bewertet. Deaktiviert: die Abweichung bleibt relevant, eskaliert aber nicht kritisch.','normal'),
 'cell_diagnostics_enabled':('Zelldiagnostik','Zelldiagnostik aktiv','bool',None,None,None,'Deaktivieren stoppt neue Zell-Samples und phasenbezogene Evidenz; vorhandene History bleibt erhalten.','normal'),
 'cell_diagnostics_interval_seconds':('Zelldiagnostik','Zell-Diagnoseintervall','s',10,3600,1,'Kleinere Werte liefern mehr Samples und schnellere Evidenz, erhöhen aber serielle Last und Datenmenge.','normal'),
 'cell_diag_history_max_samples':('History & Datenerfassung','Maximale Diagnose-Samples','Samples',100,100000,100,'Begrenzt den Diagnose-Ringpuffer. Ein kleinerer Wert verkürzt die verfügbare Evidenzbasis; Roh-History-Dateien werden dadurch nicht rückwirkend umgeschrieben.','normal'),
 'bms_stat_interval_seconds':('History & Datenerfassung','BMS-Statistikintervall','s',60,86400,60,'Kleinere Werte lesen BMS-Statistik häufiger; größere Werte reduzieren Last, aktualisieren Herstellerwerte aber seltener.','normal'),
 'trend_window_minutes':('History & Datenerfassung','Trendfenster','min',10,1440,10,'Ändert den Zeitraum für Zellspreizungs-Trends. Kürzer reagiert schneller, länger glättet stärker.','normal'),
 'trend_min_change_mv':('History & Datenerfassung','Minimale Trendänderung','mV',1,500,1,'Bestimmt, ab welcher Änderung ein Trend als steigend/fallend statt stabil gilt.','normal'),
 'incident_hold_minutes':('History & Datenerfassung','Incident-Haltezeit','min',1,1440,1,'Bestimmt, wie lange ein Incident nach dem letzten Alarm offen bleibt.','normal'),
 'cell_diag_low_soc_percent':('Phasenerkennung','Low-SOC-Grenze','%',0,50,1,'Eine höhere Grenze ordnet mehr Samples der Low-SOC-Phase zu und verändert deren Evidenz und Ranking.','normal'),
 'cell_diag_high_soc_percent':('Phasenerkennung','High-SOC-Grenze','%',50,100,1,'Eine niedrigere Grenze ordnet mehr Samples der High-SOC-Phase zu und verändert deren Evidenz und Ranking.','normal'),
 'cell_diag_discharge_current_a':('Phasenerkennung','Entlade-Stromgrenze','A',0.1,20,0.1,'Eine niedrigere Schwelle klassifiziert mehr Betriebszustände als Entladung; eine höhere verlangt stärkere Entladung.','normal'),
 'cell_diag_charge_current_a':('Phasenerkennung','Lade-Stromgrenze','A',0.1,20,0.1,'Eine niedrigere Schwelle klassifiziert mehr Betriebszustände als Ladung; eine höhere verlangt stärkere Ladung.','normal'),
 'cell_diag_min_phase_samples':('Zelldiagnostik','Mindest-Samples pro Phase','Samples',5,10000,1,'Höhere Werte verzögern die erste belastbare Phasenbewertung, erhöhen aber die Mindest-Evidenz.','normal'),
 'cell_diag_confidence_medium_samples':('Zelldiagnostik','Confidence MEDIUM ab','Samples',10,100000,10,'Höhere Werte verzögern MEDIUM-Confidence. Muss oberhalb der Mindest-Samples und unterhalb HIGH liegen.','normal'),
 'cell_diag_confidence_high_samples':('Zelldiagnostik','Confidence HIGH ab','Samples',10,100000,10,'Höhere Werte verlangen mehr Evidenz für HIGH-Confidence. Muss oberhalb MEDIUM liegen.','normal'),
 'cell_diag_high_observe_deviation_mv':('Bewertungsgrenzen','High-SOC · Beobachten','mV',1,100,1,'Niedriger = empfindlichere High-SOC-Beobachtung. Muss unter Warnung und Kritisch liegen.','normal'),
 'cell_diag_high_warning_deviation_mv':('Bewertungsgrenzen','High-SOC · Warnung','mV',1,200,1,'Niedriger = frühere High-SOC-Warnung.','normal'),
 'cell_diag_high_critical_deviation_mv':('Bewertungsgrenzen','High-SOC · Kritisch','mV',1,500,1,'Niedriger = frühere kritische High-SOC-Bewertung.','normal'),
 'cell_diag_discharge_observe_deviation_mv':('Bewertungsgrenzen','Entladung · Beobachten','mV',1,100,1,'Niedriger = empfindlichere Beobachtung während Entladung.','normal'),
 'cell_diag_discharge_warning_deviation_mv':('Bewertungsgrenzen','Entladung · Warnung','mV',1,200,1,'Niedriger = frühere Warnung während Entladung.','normal'),
 'cell_diag_discharge_critical_deviation_mv':('Bewertungsgrenzen','Entladung · Kritisch','mV',1,500,1,'Niedriger = frühere kritische Bewertung während Entladung.','normal'),
 'cell_diag_low_observe_deviation_mv':('Bewertungsgrenzen','Low-SOC · Beobachten','mV',1,100,1,'Niedriger = empfindlichere Low-SOC-Beobachtung.','normal'),
 'cell_diag_low_warning_deviation_mv':('Bewertungsgrenzen','Low-SOC · Warnung','mV',1,200,1,'Niedriger = frühere Low-SOC-Warnung.','normal'),
 'cell_diag_low_critical_deviation_mv':('Bewertungsgrenzen','Low-SOC · Kritisch','mV',1,500,1,'Niedriger = frühere kritische Low-SOC-Bewertung.','normal'),
 'cell_diag_charge_observe_deviation_mv':('Bewertungsgrenzen','Ladung · Beobachten','mV',1,100,1,'Niedriger = empfindlichere Beobachtung während Ladung.','normal'),
 'cell_diag_charge_warning_deviation_mv':('Bewertungsgrenzen','Ladung · Warnung','mV',1,200,1,'Niedriger = frühere Warnung während Ladung.','normal'),
 'cell_diag_charge_critical_deviation_mv':('Bewertungsgrenzen','Ladung · Kritisch','mV',1,500,1,'Niedriger = frühere kritische Bewertung während Ladung.','normal'),
 'serial_port':('Erweitert / System','Serielle Schnittstelle','Text',None,None,None,'Falsche Auswahl unterbricht die BMS-Kommunikation. „auto“ nutzt die automatische Erkennung.','advanced'),
 'baudrate':('Erweitert / System','Baudrate','Bd',None,None,None,'Eine falsche Baudrate verhindert die Kommunikation mit dem BMS.','advanced'),
 'command':('Erweitert / System','Pylontech Poll-Kommando','Text',None,None,None,'Änderung kann Parser und Datenerfassung vollständig außer Funktion setzen.','advanced'),
 'command_timeout_seconds':('Erweitert / System','Kommando-Timeout','s',1,30,1,'Zu kurz kann gültige Antworten abbrechen; zu lang verzögert Fehlererkennung.','advanced'),
 'mqtt_topic_prefix':('Erweitert / System','MQTT Topic Prefix','Text',None,None,None,'Änderung verschiebt MQTT-Themen und kann bestehende Home-Assistant-Entities entkoppeln.','advanced'),
 'publish_discovery':('Erweitert / System','MQTT Discovery veröffentlichen','bool',None,None,None,'Deaktivieren verhindert neue Discovery-Publikationen; bestehende Entities können erhalten bleiben.','advanced'),
 'raw_log':('Erweitert / System','Raw-PWR-Log','bool',None,None,None,'Aktivieren schreibt die letzte rohe PWR-Antwort und erhöht Schreibzugriffe.','advanced'),
 'detailed_log':('Erweitert / System','Detailliertes Laufzeitlog','bool',None,None,None,'Aktivieren erhöht Logumfang; Diagnoseberechnung selbst bleibt unverändert.','advanced'),
 'cell_diag_observe_deviation_mv':('Erweitert / System','Legacy/Fallback · Beobachten','mV',1,100,1,'Fallback-Grenze für Diagnosepfade ohne phasenspezifischen Wert. Änderungen können Bewertungen beeinflussen.','advanced'),
 'cell_diag_warning_deviation_mv':('Erweitert / System','Legacy/Fallback · Warnung','mV',1,200,1,'Fallback-Warnschwelle. Muss über Beobachten liegen.','advanced'),
 'cell_diag_critical_deviation_mv':('Erweitert / System','Legacy/Fallback · Kritisch','mV',1,500,1,'Fallback-Kritischschwelle. Muss über Warnung liegen.','advanced'),
}

GROUP_ORDER=['Anlage','Zelldiagnostik','Phasenerkennung','Bewertungsgrenzen','History & Datenerfassung','Erweitert / System']
PHASE_KEYS=[
 ('High-SOC','cell_diag_high_observe_deviation_mv','cell_diag_high_warning_deviation_mv','cell_diag_high_critical_deviation_mv'),
 ('Entladung','cell_diag_discharge_observe_deviation_mv','cell_diag_discharge_warning_deviation_mv','cell_diag_discharge_critical_deviation_mv'),
 ('Low-SOC','cell_diag_low_observe_deviation_mv','cell_diag_low_warning_deviation_mv','cell_diag_low_critical_deviation_mv'),
 ('Ladung','cell_diag_charge_observe_deviation_mv','cell_diag_charge_warning_deviation_mv','cell_diag_charge_critical_deviation_mv'),
]

def _read_options():
    data=json.loads(OPTIONS_FILE.read_text(encoding='utf-8'))
    return {**DEFAULTS, **data}

def _last_record():
    if not CONFIG_HISTORY_FILE.exists(): return {}
    try:
        lines=[x for x in CONFIG_HISTORY_FILE.read_text(encoding='utf-8').splitlines() if x.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception: return {}

def _api(method,path,payload=None):
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(SUPERVISOR+path,data=body,method=method,headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=10) as r: return json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode(errors='replace') or str(e))

def validate(cfg):
    errors=[]
    for key,meta in META.items():
        if key not in cfg: errors.append(f'{key}: fehlt'); continue
        unit,lo,hi=meta[2],meta[3],meta[4]
        if unit=='bool' and not isinstance(cfg[key],bool): errors.append(f'{meta[1]}: muss boolesch sein')
        if lo is not None:
            try: v=float(cfg[key])
            except Exception: errors.append(f'{meta[1]}: ungültige Zahl'); continue
            if v<lo or v>hi: errors.append(f'{meta[1]}: zulässig {lo} bis {hi} {unit}')
    if cfg.get('warning_cell_delta_mv',0)>=cfg.get('critical_cell_delta_mv',0): errors.append('Stack-Zellspreizung: Warnung muss kleiner als Kritisch sein.')
    if cfg.get('warning_soc_deviation_pct',0)>=cfg.get('critical_soc_deviation_pct',0): errors.append('SOC-Abweichung: Warnung muss kleiner als Kritisch sein.')
    if cfg.get('cell_diag_low_soc_percent',0)>=cfg.get('cell_diag_high_soc_percent',100): errors.append('Low-SOC-Grenze muss kleiner als High-SOC-Grenze sein.')
    if not (cfg.get('cell_diag_min_phase_samples',0) < cfg.get('cell_diag_confidence_medium_samples',0) < cfg.get('cell_diag_confidence_high_samples',0)):
        errors.append('Confidence-Reihenfolge muss Mindest-Samples < MEDIUM < HIGH sein.')
    for name,o,w,c in PHASE_KEYS+[('Fallback','cell_diag_observe_deviation_mv','cell_diag_warning_deviation_mv','cell_diag_critical_deviation_mv')]:
        if not (cfg.get(o,0)<cfg.get(w,0)<cfg.get(c,0)): errors.append(f'{name}: Beobachten < Warnung < Kritisch ist erforderlich.')
    return errors

def _html():
    return r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Guardian Konfiguration</title>
<style>:root{color-scheme:light dark;--b:#1976d2;--warn:#ef6c00}body{font:14px system-ui;margin:0;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#222)}header{padding:18px 22px;background:#0d47a1;color:white}main{max-width:1100px;margin:auto;padding:16px}.intro,.group{background:var(--card-background-color,#fff);border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 4px #0002}.group h2{margin-top:0}.row{display:grid;grid-template-columns:minmax(240px,1fr) minmax(160px,260px);gap:12px;padding:12px 0;border-top:1px solid #8883}.label{font-weight:650}.meta{font-size:12px;opacity:.72;margin-top:3px}.impact{font-size:12px;margin-top:6px;padding:7px 9px;border-left:3px solid var(--warn);background:#ff980012}input,select{width:100%;box-sizing:border-box;padding:9px;border:1px solid #8888;border-radius:7px;background:transparent;color:inherit}.advanced{border-left:4px solid #777}.actions{position:sticky;bottom:0;background:var(--card-background-color,#fff);padding:12px 16px;border-radius:12px;box-shadow:0 -2px 8px #0002;display:flex;gap:10px;align-items:center}button{padding:10px 16px;border:0;border-radius:8px;cursor:pointer}button.primary{background:var(--b);color:white}.status{margin-left:auto;font-weight:600}.changed{outline:2px solid #ff980088}.sys{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}.sys div{padding:8px;background:#8881;border-radius:7px}@media(max-width:650px){.row{grid-template-columns:1fr}.actions{flex-wrap:wrap}.status{width:100%;margin:0}}</style></head>
<body><header><h1>Guardian Battery · Konfiguration</h1><div>Diagnoseparameter kontrolliert, validiert und nachvollziehbar ändern</div></header><main>
<div class="intro"><b>Wirkung von Änderungen</b><p>Änderungen verändern die zukünftige Erfassung und/oder Bewertung. Historische Rohdaten werden nicht umgeschrieben. Nach erfolgreichem Übernehmen wird Guardian neu gestartet; diagnostisch relevante Änderungen erzeugen einen neuen Provenienz-Eintrag.</p><div class="sys" id="sys"></div></div>
<form id="form"></form><div class="actions"><button type="button" onclick="resetDefaults()">Auf Standard zurücksetzen</button><button type="button" onclick="reloadCfg()">Änderungen verwerfen</button><button class="primary" type="button" onclick="save()">Validieren & Übernehmen</button><span class="status" id="status"></span></div></main>
<script>let model,current={};const esc=s=>String(s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){let r=await fetch('api/config');model=await r.json();current=structuredClone(model.current);render();}
function render(){document.getElementById('sys').innerHTML=`<div><b>Guardian</b><br>${esc(model.guardian_version)}</div><div><b>Diagnostic Engine</b><br>${esc(model.engine_version)}</div><div><b>Config-ID</b><br>${esc(model.config_id||'—')}</div>`;let out='';for(const g of model.groups){out+=`<section class="group ${g.startsWith('Erweitert')?'advanced':''}"><h2>${esc(g)}</h2>`;for(const k of model.order.filter(k=>model.meta[k].group===g)){const m=model.meta[k],v=current[k],d=model.defaults[k];let ctl;if(m.unit==='bool')ctl=`<select data-k="${k}" onchange="chg(this)"><option value="true" ${v===true?'selected':''}>Aktiv</option><option value="false" ${v===false?'selected':''}>Inaktiv</option></select>`;else ctl=`<input data-k="${k}" value="${esc(v)}" ${m.min!=null?`type="number" min="${m.min}" max="${m.max}" step="${m.step}"`:'type="text"'} oninput="chg(this)">`;out+=`<div class="row"><div><div class="label">${esc(m.label)}</div><div class="meta">Standard: ${esc(d)}${m.unit&&m.unit!=='Text'&&m.unit!=='bool'?' '+esc(m.unit):''}${m.min!=null?' · Bereich: '+m.min+'–'+m.max+' '+esc(m.unit):''}</div><div class="impact">⚠ ${esc(m.consequence)}</div></div><div>${ctl}</div></div>`;}out+='</section>';}document.getElementById('form').innerHTML=out;document.getElementById('status').textContent='';}
function chg(el){const k=el.dataset.k,m=model.meta[k];let v=el.value;if(m.unit==='bool')v=v==='true';else if(m.min!=null)v=Number(v);current[k]=v;el.classList.toggle('changed',JSON.stringify(v)!==JSON.stringify(model.current[k]));}
function resetDefaults(){current=structuredClone(model.defaults);render();document.getElementById('status').textContent='Standardwerte vorgemerkt – noch nicht gespeichert.';}
function reloadCfg(){current=structuredClone(model.current);render();}
async function save(){const s=document.getElementById('status');s.textContent='Validierung…';let r=await fetch('api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(current)}),j=await r.json();if(!r.ok){s.textContent='Nicht übernommen: '+(j.errors||[j.error]).join(' | ');return} s.textContent=j.changed?'Übernommen. Guardian startet neu…':'Keine Änderung – nichts gespeichert.';if(j.changed)setTimeout(()=>location.reload(),7000);}
load().catch(e=>document.getElementById('status').textContent='Fehler: '+e);</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def _ingress_allowed(self):
        return self.client_address[0] == '172.30.32.2'
    def log_message(self,*_): pass
    def _send(self,code,body,ctype='application/json'):
        raw=body.encode() if isinstance(body,str) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type',ctype+'; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if self.path.rstrip('/').endswith('/api/config'):
            rec=_last_record(); meta={k:{'group':v[0],'label':v[1],'unit':v[2],'min':v[3],'max':v[4],'step':v[5],'consequence':v[6],'level':v[7]} for k,v in META.items()}
            self._send(200,{'current':_read_options(),'defaults':DEFAULTS,'meta':meta,'groups':GROUP_ORDER,'order':list(META),'config_id':rec.get('config_id'),'guardian_version':rec.get('guardian_version',GUARDIAN_VERSION),'engine_version':rec.get('diagnostic_engine_version',DIAGNOSTIC_ENGINE_VERSION)}); return
        self._send(200,_html(),'text/html')
    def do_POST(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if not self.path.rstrip('/').endswith('/api/config'): self._send(404,{'error':'not found'}); return
        try:
            n=int(self.headers.get('Content-Length','0')); proposed=json.loads(self.rfile.read(n) or b'{}')
            cfg={**_read_options(),**{k:proposed[k] for k in DEFAULTS if k in proposed}}
            errors=validate(cfg)
            if errors: self._send(400,{'errors':errors}); return
            if cfg==_read_options(): self._send(200,{'changed':False}); return
            validated=_api('POST','/addons/self/options/validate',cfg)
            data=validated.get('data',validated)
            if data.get('valid') is not True: self._send(400,{'errors':[data.get('message','Supervisor-Validierung fehlgeschlagen')]}); return
            saved=_api('POST','/addons/self/options',{'options':cfg})
            if isinstance(saved,dict) and saved.get('result') not in (None,'ok'):
                self._send(502,{'error':saved.get('message','Supervisor hat die Konfiguration nicht übernommen.')}); return
            info=_api('GET','/addons/self/info')
            info_data=info.get('data',info) if isinstance(info,dict) else {}
            stored=info_data.get('options') if isinstance(info_data,dict) else None
            if stored is not None and stored != cfg:
                self._send(502,{'error':'Supervisor-Bestätigung stimmt nicht mit der angeforderten Konfiguration überein.'}); return
            self._send(200,{'changed':True})
            def restart():
                time.sleep(1.0)
                try:_api('POST','/addons/self/restart',{})
                except Exception: pass
            threading.Thread(target=restart,daemon=True).start()
        except Exception as exc: self._send(500,{'error':str(exc)})

def start_config_server(port=8099):
    server=ThreadingHTTPServer(('0.0.0.0',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True,name='guardian-config-ui').start()
    return server
