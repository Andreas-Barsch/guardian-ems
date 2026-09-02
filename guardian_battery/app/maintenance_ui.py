"""Guardian Maintenance Logbook UI for the existing HA Ingress application."""

from __future__ import annotations

from html import escape
from urllib.parse import quote

from guardian_header import render_guardian_header


def maintenance_deep_link(event_id: str) -> str:
    """Return the central ingress-relative target for one stable event ID."""

    return f"maintenance?event_id={quote(event_id, safe='')}"


def render_maintenance_html(*, configuration_path: str, timeline_path: str = "timeline",
                            history_path: str = "history") -> str:
    config_href = escape(configuration_path, quote=True)
    timeline_href = escape(timeline_path, quote=True)
    history_href = escape(history_path, quote=True)
    header = render_guardian_header(
        active="maintenance",
        paths={"modules": "./", "configuration": configuration_path,
               "maintenance": "maintenance", "timeline": timeline_path,
               "history": history_path, "diagnostics": "diagnostics"},
    )
    return f'''<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Guardian Battery · Maintenance-Logbuch</title>
  <style>
    :root{{--blue:#0d47a1;--accent:#1976d2;--warn:#ef6c00;--danger:#b3261e;--ok:#2e7d32;--line:#8884;color-scheme:light dark}}
    *{{box-sizing:border-box}} body{{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--primary-background-color,#f5f6f8);color:var(--primary-text-color,#202124)}}
    header{{background:var(--blue);color:#fff;padding:18px clamp(16px,4vw,34px)}} header h1{{margin:0 0 10px;font-size:clamp(21px,4vw,30px)}}
    nav{{display:flex;gap:8px;flex-wrap:wrap}} nav a{{color:#fff;text-decoration:none;padding:8px 12px;border:1px solid #ffffff66;border-radius:8px}} nav a.active{{background:#fff;color:var(--blue)}}
    main{{max-width:1180px;margin:auto;padding:16px}} .panel{{background:var(--card-background-color,#fff);border-radius:12px;padding:16px;margin:0 0 14px;box-shadow:0 1px 5px #0002}}
    .toolbar,.actions{{display:flex;gap:9px;align-items:center;flex-wrap:wrap}} .toolbar h2{{margin:0 auto 0 0}} button,.button{{border:0;border-radius:8px;padding:10px 14px;cursor:pointer;background:#e3e8ef;color:#17202a;font:inherit}} button.primary{{background:var(--accent);color:#fff}} button.danger{{background:var(--danger);color:#fff}} button:disabled{{opacity:.55;cursor:not-allowed}}
    .filters,.form-grid,.facts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .filters{{grid-template-columns:repeat(4,minmax(0,1fr));margin-top:14px}} label{{display:flex;flex-direction:column;gap:5px;font-weight:650}} label span.hint,.hint{{font-size:12px;font-weight:400;opacity:.72}}
    input,select,textarea{{width:100%;padding:9px 10px;border:1px solid #8888;border-radius:7px;background:var(--card-background-color,#fff);color:inherit;font:inherit}} textarea{{min-height:92px;resize:vertical}} .wide{{grid-column:1/-1}}
    .events{{display:grid;gap:10px;margin-top:14px}} .event-card{{display:grid;grid-template-columns:minmax(160px,.75fr) minmax(220px,2fr) minmax(160px,1fr);gap:12px;padding:13px;border:1px solid var(--line);border-radius:10px;text-decoration:none;color:inherit}} .event-card:hover{{border-color:var(--accent);background:#1976d208}} .event-title{{font-size:16px;font-weight:700}} .muted{{opacity:.7;font-size:13px}} .badge{{display:inline-block;border-radius:99px;padding:3px 8px;background:#1976d21a;font-size:12px;font-weight:700}} .archived{{background:#b3261e1a;color:var(--danger)}}
    .facts div{{padding:9px;background:#8881;border-radius:8px}} .facts dt{{font-size:12px;opacity:.72}} .facts dd{{margin:3px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}} .description{{white-space:pre-wrap;overflow-wrap:anywhere}}
    .notice,.error,.conflict{{padding:11px 13px;border-radius:8px;margin:10px 0}} .notice{{background:#1976d214}} .error{{background:#b3261e18;color:var(--danger)}} .conflict{{background:#ef6c0018;border-left:4px solid var(--warn)}}
    .history-list{{display:grid;gap:7px;margin-top:8px}} .history-row{{padding:9px;border:1px solid var(--line);border-radius:8px}} [hidden]{{display:none!important}}
    @media(max-width:800px){{.filters{{grid-template-columns:repeat(2,minmax(0,1fr))}}.event-card{{grid-template-columns:1fr 1fr}}.event-card .event-main{{grid-column:1/-1;grid-row:1}}}}
    @media(max-width:560px){{main{{padding:10px}}.panel{{padding:13px}}.filters,.form-grid,.facts,.event-card{{grid-template-columns:1fr}}.wide,.event-card .event-main{{grid-column:1}}button,.button{{width:100%}}.toolbar h2{{width:100%}}}}
  </style>
</head>
<body>
{header}
<main>
  <div id="message" role="status" aria-live="polite"></div>
  <section id="list-view" class="panel">
    <div class="toolbar"><h2>Maintenance-Logbuch</h2><button id="new-button" class="primary" type="button">Neuer Eintrag</button></div>
    <form id="filter-form" class="filters">
      <label>Von <input id="filter-from" type="datetime-local"><span class="hint">lokale Browserzeit</span></label>
      <label>Bis <input id="filter-to" type="datetime-local"><span class="hint">lokale Browserzeit</span></label>
      <label>Kategorie <input id="filter-category" placeholder="z. B. inspection"></label>
      <label>Modul <select id="filter-module"><option value="">Alle</option></select></label>
      <label>Zelle <select id="filter-cell"><option value="">Alle</option></select></label>
      <label><span>Sortierung</span><select id="filter-sort"><option value="true">Neueste zuerst</option><option value="false">Älteste zuerst</option></select></label>
      <label><span>Aktivität</span><select id="filter-active"><option value="true">Nur aktive</option><option value="all">Alle</option><option value="false">Nur nicht aktive</option></select></label>
      <div class="actions"><button class="primary" type="submit">Filter anwenden</button><button id="filter-reset" type="button">Zurücksetzen</button></div>
    </form>
    <div id="events" class="events"></div>
    <div class="actions"><button id="page-prev" type="button">Zurück</button><span id="page-status" class="muted"></span><button id="page-next" type="button">Weiter</button></div>
  </section>

  <section id="detail-view" class="panel" hidden>
    <div class="toolbar"><button id="detail-back" type="button">Zum Logbuch</button><h2 id="detail-title"></h2><span id="detail-status" class="badge"></span></div>
    <p id="detail-description" class="description"></p><dl id="detail-facts" class="facts"></dl>
    <div class="actions"><button id="edit-button" class="primary" type="button">Bearbeiten</button><button id="active-toggle" type="button"></button></div>
    <details id="history"><summary>Änderungsverlauf anzeigen</summary><div id="history-list" class="history-list"></div></details>
  </section>

  <section id="form-view" class="panel" hidden>
    <div class="toolbar"><h2 id="form-heading">Neuer Eintrag</h2><button id="form-cancel" type="button">Abbrechen</button></div>
    <div class="notice">Ereigniszeitpunkt und Erfassungszeit sind getrennt. Die Eingabe erfolgt in <strong id="timezone-name"></strong> und wird als UTC gespeichert. <span id="utc-preview"></span></div>
    <div id="conflict-box" class="conflict" hidden><strong>Dieser Eintrag wurde zwischenzeitlich geändert.</strong><p>Ihre ungespeicherten Eingaben bleiben im Formular erhalten. Laden Sie die aktuelle Version erst neu, wenn Sie diese Eingaben bewusst verwerfen möchten.</p><button id="conflict-reload" type="button">Aktuelle Version neu laden</button></div>
    <form id="event-form" class="form-grid">
      <label>Ereigniszeitpunkt * <input id="occurred-at" type="datetime-local" required></label>
      <label>Kategorie * <select id="category" required></select></label>
      <label class="wide">Titel * <input id="title" maxlength="200" required></label>
      <label>Betroffenes System * <input id="affected-system" maxlength="200" required value="Pylontech Stack"></label>
      <label>Modul <select id="module-number"><option value="">Kein Modulbezug</option></select></label>
      <label>Seriennummer <select id="module-serial"><option value="">Seriennummer unbekannt</option></select><span class="hint">Nur historisch nachweisbare Identitäten; ohne Beleg bleibt die Seriennummer unbekannt.</span></label>
      <label>Zelle <select id="cell-number"><option value="">Alle Zellen / Modulebene</option></select></label>
      <label class="wide">Beschreibung <textarea id="description" maxlength="10000"></textarea></label>
      <label class="wide">Durchgeführte Maßnahme <textarea id="action-taken" maxlength="10000"></textarea></label>
      <label class="wide">Vorheriger Zustand <textarea id="previous-state" maxlength="10000"></textarea></label>
      <label class="wide">Ergebnis <textarea id="result" maxlength="10000"></textarea></label>
      <label class="wide">Grund / Anlass <textarea id="reason" maxlength="10000"></textarea></label>
      <div class="actions wide"><button id="save-button" class="primary" type="submit">Speichern</button></div>
    </form>
  </section>
</main>
<script>
const CATEGORIES=['maintenance','inspection','repair','module_identification','module_position_change','module_replacement','module_added','module_removed','battery_cell_test','firmware_change','configuration_change','wiring_connection','troubleshooting','other_technical'];
const CATEGORY_LABELS={{maintenance:'Wartung',inspection:'Inspektion',repair:'Reparatur',module_identification:'Erstidentifikation / Initialzuordnung',module_position_change:'Positionsänderung',module_replacement:'Modultausch',module_added:'Modul hinzugefügt',module_removed:'Modul entfernt',battery_cell_test:'Batterie-/Zellprüfung',firmware_change:'Firmwareänderung',configuration_change:'Konfigurationsänderung',wiring_connection:'Verkabelung / Anschluss',troubleshooting:'Fehlerbehebung',other_technical:'Sonstiges technisches Ereignis'}};
const state={{current:null,editing:false,offset:0,limit:25,total:0}};
const byId=id=>document.getElementById(id);
function apiTarget(suffix=''){{return 'api/maintenance/events'+suffix;}}
function maintenanceTarget(eventId){{return 'maintenance?event_id='+encodeURIComponent(eventId);}}
function localZone(){{return Intl.DateTimeFormat().resolvedOptions().timeZone||'lokale Browser-Zeitzone';}}
function formatLocal(iso){{if(!iso)return '—';return new Intl.DateTimeFormat('de-DE',{{dateStyle:'medium',timeStyle:'medium'}}).format(new Date(iso));}}
function utcToLocalInput(iso){{if(!iso)return '';const d=new Date(iso),p=n=>String(n).padStart(2,'0');return `${{d.getFullYear()}}-${{p(d.getMonth()+1)}}-${{p(d.getDate())}}T${{p(d.getHours())}}:${{p(d.getMinutes())}}`;}}
function localInputToUtc(value){{const m=/^([0-9]{{4}})-([0-9]{{2}})-([0-9]{{2}})T([0-9]{{2}}):([0-9]{{2}})$/.exec(value);if(!m)throw new Error('Bitte einen vollständigen lokalen Ereigniszeitpunkt eingeben.');const parts=m.slice(1).map(Number),d=new Date(parts[0],parts[1]-1,parts[2],parts[3],parts[4],0,0);if(d.getFullYear()!==parts[0]||d.getMonth()!==parts[1]-1||d.getDate()!==parts[2]||d.getHours()!==parts[3]||d.getMinutes()!==parts[4])throw new Error('Diese lokale Uhrzeit existiert wegen einer Zeitumstellung nicht.');return d.toISOString();}}
function setMessage(text,kind='notice'){{const box=byId('message');box.replaceChildren();if(text){{const node=document.createElement('div');node.className=kind;node.textContent=text;box.append(node);}}}}
function show(view){{for(const id of ['list-view','detail-view','form-view'])byId(id).hidden=id!==view;window.scrollTo({{top:0,behavior:'smooth'}});}}
async function apiFetch(target,options={{}}){{const response=await fetch(target,options);let payload;try{{payload=await response.json();}}catch{{payload={{error:{{code:'internal_error',message:'Ungültige Serverantwort',details:{{}}}}}};}}if(!response.ok){{const error=new Error(payload.error?.message||'Anfrage fehlgeschlagen');error.status=response.status;error.payload=payload;throw error;}}return payload;}}
function friendlyError(error){{const code=error.payload?.error?.code;return {{invalid_request:'Ungültige Eingabe.',validation_error:'Bitte prüfen Sie die eingegebenen Werte.',not_found:'Maintenance Event nicht gefunden.',conflict:'Dieser Eintrag wurde zwischenzeitlich geändert.',request_too_large:'Die Eingabe ist zu groß.',history_error:'Die Maintenance-History ist derzeit nicht zuverlässig lesbar.',internal_error:'Unerwarteter interner Fehler.'}}[code]||error.message||'Die Anfrage konnte nicht verarbeitet werden.';}}
function addOptions(select,start,end){{for(let n=start;n<=end;n++){{const option=document.createElement('option');option.value=String(n);option.textContent=String(n);select.append(option);}}}}
function categoryLabel(key){{return CATEGORY_LABELS[key]||key;}}
function filtersQuery(){{const q=new URLSearchParams();q.set('active',byId('filter-active').value);q.set('newest_first',byId('filter-sort').value);q.set('limit',String(state.limit));q.set('offset',String(state.offset));const mapping=[['filter-category','category'],['filter-module','module_number'],['filter-cell','cell_number']];for(const [id,key] of mapping)if(byId(id).value)q.set(key,byId(id).value);if(byId('filter-from').value)q.set('occurred_from',localInputToUtc(byId('filter-from').value));if(byId('filter-to').value)q.set('occurred_to',localInputToUtc(byId('filter-to').value));return q;}}
function eventCard(event){{const link=document.createElement('a');link.className='event-card';link.href=maintenanceTarget(event.maintenance_event_id);const main=document.createElement('div');main.className='event-main';const title=document.createElement('div');title.className='event-title';title.textContent=event.title;const category=document.createElement('span');category.className='badge';category.textContent=categoryLabel(event.category);main.append(title,category);const time=document.createElement('div');time.textContent=formatLocal(event.occurred_at);const context=document.createElement('div');context.textContent=[event.affected_system,event.module_number?'Position '+event.module_number:null,event.module_serial?'Seriennummer '+event.module_serial:null,event.cell_number?'Zelle '+event.cell_number:null].filter(Boolean).join(' · ');if(!event.active){{const inactive=document.createElement('div');inactive.className='badge archived';inactive.textContent='Nicht aktiv';context.append(document.createElement('br'),inactive);}}link.append(main,time,context);return link;}}
async function loadList(){{show('list-view');setMessage('');try{{const payload=await apiFetch(apiTarget('?'+filtersQuery().toString()));state.total=payload.pagination.total;const list=byId('events');list.replaceChildren();if(!payload.events.length){{const empty=document.createElement('p');empty.className='muted';empty.textContent='Keine Maintenance Events für diese Filter.';list.append(empty);}}else for(const event of payload.events)list.append(eventCard(event));byId('page-status').textContent=`${{state.offset+1}}–${{Math.min(state.offset+payload.pagination.returned,state.total)}} von ${{state.total}}`;byId('page-prev').disabled=state.offset===0;byId('page-next').disabled=state.offset+payload.pagination.returned>=state.total;}}catch(error){{setMessage(friendlyError(error),'error');}}}}
function fact(label,value){{if(value===null||value===undefined||value==='')return null;const wrap=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=String(value);wrap.append(dt,dd);return wrap;}}
async function loadEvent(eventId){{setMessage('');try{{const payload=await apiFetch(apiTarget('/'+encodeURIComponent(eventId)));renderDetail(payload.event);byId('history-list').replaceChildren();show('detail-view');historyData(eventId);}}catch(error){{show('detail-view');byId('detail-title').textContent=error.status===404?'Eintrag nicht gefunden':'Maintenance Event nicht verfügbar';byId('detail-description').textContent=friendlyError(error);byId('detail-facts').replaceChildren();byId('edit-button').hidden=true;byId('active-toggle').hidden=true;byId('history').hidden=true;}}}}
function renderDetail(event){{state.current=event;byId('detail-title').textContent=event.title;byId('detail-description').textContent=event.description||'';const status=byId('detail-status');status.textContent=event.active?'Aktiv':'Nicht aktiv';status.className='badge'+(event.active?'':' archived');const facts=byId('detail-facts');facts.replaceChildren();const rows=[['Ereigniszeitpunkt',formatLocal(event.occurred_at)],['Kategorie',categoryLabel(event.category)],['Betroffenes System',event.affected_system],['Stackposition zum Ereigniszeitpunkt',event.module_number],['Seriennummer',event.module_serial||'Seriennummer unbekannt'],['Zellnummer',event.cell_number||'Alle Zellen / Modulebene'],['Durchgeführte Maßnahme',event.action_taken],['Vorheriger Zustand',event.previous_state],['Ergebnis',event.result],['Grund / Anlass',event.reason],['Maintenance Event ID',event.maintenance_event_id],['Revision',event.revision],['Erfasst am',formatLocal(event.created_at)],['Zuletzt geändert',event.updated_at?formatLocal(event.updated_at):'Noch nicht geändert'],['Nicht aktiv seit',event.archived_at?formatLocal(event.archived_at):null]];for(const row of rows){{const node=fact(...row);if(node)facts.append(node);}}byId('edit-button').hidden=!event.active;const toggle=byId('active-toggle');toggle.hidden=false;toggle.textContent=event.active?'Auf Nicht aktiv setzen':'Aktivieren';toggle.className=event.active?'danger':'';byId('history').hidden=false;byId('history-list').replaceChildren();}}
async function historyData(eventId){{try{{const payload=await apiFetch(apiTarget('/'+encodeURIComponent(eventId)+'/history'));const list=byId('history-list');list.replaceChildren();for(const item of payload.history){{const row=document.createElement('div');row.className='history-row';row.textContent=`Revision ${{item.revision}} · Ereignis: ${{formatLocal(item.occurred_at)}} · geändert: ${{item.updated_at?formatLocal(item.updated_at):'Ersterfassung'}} · ${{item.active?'aktiv':'nicht aktiv'}}`;list.append(row);}}}}catch(error){{byId('history-list').textContent=friendlyError(error);}}}}
function formValue(id,value=''){{byId(id).value=value??'';}}
async function setSerialOptions(event){{const select=byId('module-serial');select.replaceChildren();const none=document.createElement('option');none.value='';none.textContent='Seriennummer unbekannt';select.append(none);let effective='';const module=byId('module-number').value,at=byId('occurred-at').value;if(module&&at){{try{{const q=new URLSearchParams({{module_number:module,at:localInputToUtc(at)}}),data=await apiFetch('api/position-history/known-serials?'+q);effective=data.effective_serial||'';const labels={{effective:'Zum Ereigniszeitpunkt: ',earlier:'Früher an dieser Position: ',later:'Später an dieser Position: ',earlier_and_later:'Früher und später an dieser Position: '}};for(const item of data.serial_options||[]){{const option=document.createElement('option');option.value=item.serial;option.textContent=(labels[item.relationship]||'Dokumentiert: ')+item.serial;select.append(option);}}}}catch{{}}}}if(event?.module_serial&&![...select.options].some(o=>o.value===event.module_serial)){{const known=document.createElement('option');known.value=event.module_serial;known.textContent='Im Eintrag gespeichert: '+event.module_serial;select.append(known);}}select.value=event?.module_serial||effective||'';}}
function openForm(event=null){{state.editing=Boolean(event);state.current=event;byId('form-heading').textContent=event?'Maintenance Event bearbeiten':'Neuer Maintenance-Eintrag';formValue('occurred-at',event?utcToLocalInput(event.occurred_at):utcToLocalInput(new Date().toISOString()));formValue('category',event?.category||'maintenance');formValue('title',event?.title);formValue('description',event?.description);formValue('affected-system',event?.affected_system||'Pylontech Stack');formValue('module-number',event?.module_number);setSerialOptions(event);formValue('cell-number',event?.cell_number);formValue('action-taken',event?.action_taken);formValue('previous-state',event?.previous_state);formValue('result',event?.result);formValue('reason',event?.reason);byId('conflict-box').hidden=true;updateUtcPreview();show('form-view');}}
function optionalText(id){{const value=byId(id).value.trim();return value||null;}} function optionalInt(id){{return byId(id).value?Number(byId(id).value):null;}}
function formPayload(){{return {{occurred_at:localInputToUtc(byId('occurred-at').value),category:byId('category').value,title:byId('title').value.trim(),description:optionalText('description'),affected_system:byId('affected-system').value.trim(),module_number:optionalInt('module-number'),module_serial:optionalText('module-serial'),cell_number:optionalInt('cell-number'),action_taken:optionalText('action-taken'),previous_state:optionalText('previous-state'),result:optionalText('result'),reason:optionalText('reason')}};}}
async function saveForm(event){{event.preventDefault();setMessage('');try{{const payload=formPayload();let response;if(state.editing)response=await apiFetch(apiTarget('/'+encodeURIComponent(state.current.maintenance_event_id)),{{method:'PATCH',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{expected_revision:state.current.revision,changes:payload}})}});else response=await apiFetch(apiTarget(),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});byId('history-list').replaceChildren();window.history.replaceState(null,'',maintenanceTarget(response.event.maintenance_event_id));renderDetail(response.event);show('detail-view');historyData(response.event.maintenance_event_id);}}catch(error){{if(error.status===409)byId('conflict-box').hidden=false;setMessage(friendlyError(error),'error');}}}}
function updateUtcPreview(){{try{{const utc=localInputToUtc(byId('occurred-at').value);byId('utc-preview').textContent='Speicherwert: '+utc;}}catch{{byId('utc-preview').textContent='';}}}}
async function toggleActive(){{const active=!state.current.active,action=active?'activate':'deactivate';try{{const payload=await apiFetch(apiTarget('/'+encodeURIComponent(state.current.maintenance_event_id)+'/'+action),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{expected_revision:state.current.revision}})}});renderDetail(payload.event);historyData(payload.event.maintenance_event_id);}}catch(error){{setMessage(friendlyError(error),'error');}}}}
function initialize(){{byId('timezone-name').textContent=localZone();for(const key of CATEGORIES){{const option=document.createElement('option');option.value=key;option.textContent=categoryLabel(key);byId('category').append(option);}}addOptions(byId('module-number'),1,6);addOptions(byId('filter-module'),1,6);addOptions(byId('cell-number'),1,15);addOptions(byId('filter-cell'),1,15);byId('new-button').onclick=()=>openForm();byId('detail-back').onclick=()=>{{window.history.replaceState(null,'','maintenance');state.offset=0;loadList();}};byId('edit-button').onclick=()=>openForm(state.current);byId('form-cancel').onclick=()=>state.current?loadEvent(state.current.maintenance_event_id):loadList();byId('event-form').onsubmit=saveForm;byId('occurred-at').onchange=()=>{{updateUtcPreview();setSerialOptions(state.current);}};byId('module-number').onchange=()=>setSerialOptions(state.current);byId('active-toggle').onclick=toggleActive;byId('conflict-reload').onclick=()=>loadEvent(state.current.maintenance_event_id);byId('filter-form').onsubmit=event=>{{event.preventDefault();state.offset=0;loadList();}};byId('filter-reset').onclick=()=>{{byId('filter-form').reset();state.offset=0;loadList();}};byId('page-prev').onclick=()=>{{state.offset=Math.max(0,state.offset-state.limit);loadList();}};byId('page-next').onclick=()=>{{state.offset+=state.limit;loadList();}};const eventId=new URLSearchParams(window.location.search).get('event_id');if(eventId)loadEvent(eventId);else loadList();}}
const initializeWithReturn=initialize;initialize=function(){{initializeWithReturn();const target=new URLSearchParams(window.location.search).get('return');if(target)byId('detail-back').onclick=()=>{{window.location.href=target;}};}};
document.addEventListener('DOMContentLoaded',initialize);
</script>
</body></html>'''
