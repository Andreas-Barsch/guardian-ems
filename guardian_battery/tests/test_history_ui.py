import json
from pathlib import Path
import shutil
import subprocess
import re

import pytest

from history_ui import render_history_html


def _javascript_function(source, name):
    start = source.index(f"function {name}")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function {name}")


def _node_binary():
    executable = shutil.which("node")
    if executable:
        return executable
    candidates = sorted((Path.home() / ".cache/codex-runtimes").glob(
        "*/dependencies/node/bin/node"))
    if candidates:
        return str(candidates[-1])
    pytest.skip("Node runtime is unavailable")


def _run_node(script):
    return subprocess.run([_node_binary(), "-e", script], check=True,
                          text=True, capture_output=True).stdout


def test_generic_soc_and_cell_chart_with_touch_markers_and_responsive_layout():
    html = render_history_html(configuration_path="/x/", maintenance_path="/x/maintenance", timeline_path="/x/timeline")
    assert "SOC [%]" in html and "Zellspannung [V]" in html
    assert "api/history/series" in html
    assert "class:'marker'" in html
    assert "showMarker(marker)" in html
    assert "Maintenance-Eintrag öffnen" in html
    assert "@media(max-width:390px)" in html
    assert "overflow:hidden" in html


def test_soc_projection_distinguishes_module_hycube_and_policy_sources():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "soc_timeline" in html and "module_series" in html and "hycube_series" in html
    assert "Hycube Battery Capacity" in html
    assert "Quelle Hycube" in html
    assert "Aggregationsregel gegenüber den Pylontech-Modul-SOCs ist nicht bestätigt" in html
    for label in ("Bereichsgrenze Normalbetrieb / Passiv",
                  "Bereichsgrenze Passiv / Notstrom",
                  "Bereichsgrenze Notstrom / Batterieschutz"):
        assert label in html
    assert "GET /Bat/getCustomBat/" in html
    assert "kumulative Übergänge" in html
    assert "keine Abschaltursache" in html
    assert "stroke-dasharray','8 4" in html
    assert "Stack-SOC" not in html
    assert "data.phase_analysis?.intervals" in html
    assert ".legend{display:flex" in html and "flex-wrap:wrap" in html
    assert "timeline.policy_series.flatMap" in html
    assert "{timestamp:segment.from" in html and "{timestamp:segment.to" in html
    assert "Entladegrenze" not in html
    assert "Normalbetrieb: ${policy.normal_operation_pct}" in html
    assert "Batterieschutz: ${policy.battery_protection_pct}" in html
    assert "Kausalität nicht bestimmt" in html
    assert "series.source==='pylontech'?'module_soc'" in html
    assert "series.source==='hycube'?'hycube_capacity':series.source==='hycube_policy'?'policy_boundary':'soc_source_unknown'" in html


def test_module_and_metric_selections_are_orthogonal():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "selected_modules" in html and "selectedModules()" in html
    assert "Mindestens ein Modul auswählen" in html
    assert "Alle Module sind in der Einzelansicht nur für SOC verfügbar" in html
    assert "modulePicker.hidden=!combined" in html
    assert '<label id="module-control">Modul<select id="module"></select></label>' in html
    assert "module_number" in html
    assert "byId('view-mode').onchange=updateControlState" in html


def test_soc_tooltips_use_series_semantics_not_synthetic_cell_numbers():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "function pointTooltip(point,metric,step)" in html
    assert "point._series_type==='module_soc'" in html
    assert "Modul ${moduleNumber}\\nSOC:" in html
    assert "point._series_type==='hycube_capacity'" in html
    assert "Hycube Battery Capacity\\n${value} %\\nQuelle: Hycube /data_row/" in html
    assert "point._series_type==='policy_boundary'" in html
    assert "${point._series_label}" in html
    assert "Hycube-Konfiguration:" in html
    assert "Kausalität nicht bestimmt" in html
    assert "point._series_type==='cell_value'" in html
    assert "Modul ${moduleNumber} · Zelle ${cell}" in html
    assert "metric==='current'?'Strom'" in html
    assert "p.cell_number?' · Zelle '" not in html
    assert "point.cell_number?' · Zelle '" not in html


def test_soc_legend_is_grouped_and_uses_compact_labels():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "legendGroup('Pylontech')" in html
    assert "legendGroup('Hycube')" in html
    assert "legendGroup('Hycube-Bereichsgrenzen')" in html
    assert "`Modul ${label.module_number}`" in html
    assert "label.source==='hycube'?'Battery Capacity':label.source==='hycube_policy'?label.label.replace('Bereichsgrenze ','')" in html
    assert ".legend-group{display:flex" in html
    assert "flex-wrap:wrap" in html


def test_soc_legend_executes_for_api_sources_and_neutral_unknown_source():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance",
                               timeline_path="timeline")
    functions = "\n".join(_javascript_function(html, name) for name in
                            ("legendGroup", "relabelSocLegend"))
    script = f"""
class Item {{
  constructor(tag, text=null) {{ this.tag=tag; this.textContent=text; this.children=[]; this.attrs={{}}; }}
  append(...items) {{ this.children.push(...items); }}
  replaceChildren(...items) {{ this.children=[...items]; }}
  querySelector(selector) {{
    if(selector==='.legend') return this.legend;
    if(selector==='button') return this.children.find(item=>item.tag==='button')||null;
    return null;
  }}
  querySelectorAll(selector) {{
    if(selector==='button') return this.children.filter(item=>item.tag==='button');
    if(selector==='path.series') return this.paths||[];
    return [];
  }}
  get lastChild() {{ return this.children[this.children.length-1]; }}
  setAttribute(key,value) {{ this.attrs[key]=value; }}
  getAttribute(key) {{ return this.attrs[key]; }}
}}
const node=(tag,text)=>new Item(tag,text);
const labels=[
  {{source:'pylontech',module_number:1,label:'Modul 1 SOC'}},
  {{source:'hycube',label:'Hycube BatteryCapacity'}},
  {{source:'hycube_policy',label:'Bereichsgrenze Normalbetrieb / Passiv'}},
  {{source:'future_source',label:'Forensische Zusatzquelle'}}
];
const socLabels=()=>labels;
{functions}
const legend=new Item('div');
for(let index=0;index<labels.length;index++){{const button=new Item('button');button.append(new Item('span'),new Item('text','old'));legend.append(button)}}
const root=new Item('root');root.legend=legend;
root.paths=labels.map(()=>{{const path=new Item('path');path.attrs.stroke='#1565c0';path._isolatedPoints=[new Item('circle')];return path}});
relabelSocLegend(root,{{soc_timeline:{{}}}});
const assert=require('node:assert/strict');
assert.deepEqual(root.paths.map(path=>path._isolatedPoints[0].attrs.fill),['#1565c0','#111111','#f57c00','#1565c0']);
console.log(JSON.stringify(legend.children.map(group=>[group.children[0].textContent,group.children.slice(1).map(button=>button.lastChild.textContent)])));
"""
    groups = json.loads(_run_node(script))
    assert groups == [["Pylontech", ["Modul 1"]],
                      ["Hycube", ["Battery Capacity"]],
                      ["Hycube-Bereichsgrenzen", ["Normalbetrieb / Passiv"]],
                      ["Weitere Quellen", ["Forensische Zusatzquelle"]]]


def test_policy_tooltip_executes_without_historical_policy_evidence():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance",
                               timeline_path="timeline")
    function = _javascript_function(html, "pointTooltip")
    script = f"""
const exact=()=> '02.09.2026, 09:35:00';
const decimals=()=>1;
const METRIC_LABELS={{soc:'SOC [%]'}},UNITS={{soc:'%'}};
{function}
const point={{time:0,display:25,_series_type:'policy_boundary',_series_label:'Bereichsgrenze'}};
console.log(pointTooltip(point,'soc',1));
"""
    output = _run_node(script)
    assert "Policy: nicht verfügbar (keine historische Evidence)" in output
    assert "Kausalität nicht bestimmt" in output


def test_complete_soc_contract_executes_with_hycube_policy_phase_and_marker():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance",
                               timeline_path="timeline")
    functions = "\n".join(_javascript_function(html, name) for name in
                            ("policyBoundarySeries", "socProjection", "pointTooltip"))
    script = f"""
const POLICY_BOUNDARIES=[['boundary_normal_passive','Normal / Passiv']];
const exact=()=> '02.09.2026, 09:35:00',decimals=()=>1;
const METRIC_LABELS={{soc:'SOC [%]'}},UNITS={{soc:'%'}};
const timeline={{
 module_series:[{{source:'pylontech',module_number:1,label:'Modul 1 SOC',points:[{{timestamp:'2026-09-02T09:35:00Z',value:75}}]}}],
 hycube_series:{{source:'hycube',label:'Hycube BatteryCapacity',points:[{{timestamp:'2026-09-02T09:35:00Z',value:80}}]}},
 policy_series:[{{from:'2026-09-02T09:30:00Z',to:'2026-09-02T10:00:00Z',boundary_normal_passive:18,normal_operation_pct:82,passive_pct:3,emergency_pct:10,battery_protection_pct:5}}]
}};
const data={{series:{{metric:'soc',points:[]}},soc_timeline:timeline,
 phase_analysis:{{intervals:[{{phase:'charge'}}]}},overlays:[{{title:'Maintenance'}}]}};
const socLabels=value=>[...value.soc_timeline.module_series,value.soc_timeline.hycube_series,...policyBoundarySeries(value.soc_timeline)];
{functions}
const projected=socProjection(data);
const tooltips=projected.points.map(point=>pointTooltip({{...point,time:Date.parse(point.timestamp),display:point.value}},'soc',1));
console.log(JSON.stringify({{types:projected.points.map(point=>point._series_type),tooltips,
 phases:data.phase_analysis.intervals.length,markers:data.overlays.length}}));
"""
    rendered = json.loads(_run_node(script))
    assert rendered["types"] == ["module_soc", "hycube_capacity", "policy_boundary",
                                  "policy_boundary"]
    assert any("Hycube Battery Capacity" in item for item in rendered["tooltips"])
    assert any("Normalbetrieb: 82 %" in item for item in rendered["tooltips"])
    assert rendered["phases"] == rendered["markers"] == 1


def test_overlay_uses_server_deep_link_and_safe_text_rendering():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "marker.deep_link" in html
    assert "maintenance?event_id=" not in html
    assert ".innerHTML" not in html
    assert "marker-title').textContent=marker.title" in html
    assert "marker-meta').textContent=parts.join" in html
    assert "marker.metadata?.category||marker.event_type" in html


def test_navigation_paths_are_escaped():
    html = render_history_html(configuration_path='"<script>', maintenance_path='" onmouseover="x', timeline_path='"><img>')
    assert '"<script>' not in html and 'onmouseover="x' not in html and '"><img>' not in html
    assert "&quot;" in html and "&lt;script&gt;" in html


def test_svg_chart_has_real_axes_units_layers_resize_and_local_tooltip():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    for layer in ("phase-layer", "grid-layer", "day-layer", "series-layer", "marker-layer", "interaction-layer"):
        assert f'id="{layer}"' in html
    assert "ResizeObserver" in html
    assert "Intl.DateTimeFormat('de-DE'" in html
    assert "niceStep" in html and "tickCount" in html
    assert 'id="voltage-cell-picker"' in html
    assert 'id="temperature-cell-picker"' in html
    assert 'id="voltage-cells-all"' in html and 'id="temperature-cells-none"' in html
    assert "cell_numbers" in html and "selectedCells" in html
    assert "grid-template-columns:minmax(135px" in html
    assert "picker.hidden=!visible" in html and "picker.disabled=!visible" in html
    assert 'id="cell-label" hidden' not in html
    assert "@media(max-width:980px)" in html
    assert "@media(max-width:620px)" in html
    assert ".innerHTML" not in html


def test_local_midnight_boundaries_are_inner_dst_safe_and_shared_by_all_views():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    helper = html.split("function localDayBoundaries", 1)[1].split("function dayLabel", 1)[0]
    assert "cursor.setHours(0,0,0,0)" in helper
    assert "cursor.setDate(cursor.getDate()+1)" in helper
    assert "cursor.getTime()<end" in helper
    assert "86400000" not in helper and "864e5" not in helper
    assert "getUTC" not in helper and "setUTC" not in helper
    assert "function renderDayBoundaries" in html
    assert "renderDayBoundaries(byId('day-layer'),start,end,x" in html
    assert "renderDayBoundaries(ui.layers.day,start,end,x" in html
    assert "['phase','grid','day','axis','series','marker','interaction']" in html
    assert html.index('id="phase-layer"') < html.index('id="day-layer"') < html.index('id="series-layer"') < html.index('id="marker-layer"')
    assert "class:'day-boundary'" in html and "class','day-label'" in html
    assert "stroke-dasharray:2 4" in html and "pointer-events:none" in html


def test_midnight_projection_does_not_change_measurement_phase_or_maintenance_data():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "orderedPoints(data.series,metric)" in html
    assert "data.phase_analysis?.intervals" in html
    assert "for(const marker of data.overlays)" in html
    assert "class:'marker'" in html and "class:'wrench'" in html
    assert "api/history/series?" in html
    assert "localDayBoundaries(start,end)" in html


def test_rs485_metrics_are_available_in_single_combined_and_boolean_step_views():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    for metric in ("rs485_ccl", "rs485_dcl", "rs485_charge_enable",
                   "rs485_discharge_enable", "rs485_cvl", "rs485_dvl"):
        assert metric in html
    assert 'id="adr"' not in html and "RS485 ADR<select" not in html
    assert "result.set('adr'" not in html
    assert "STOP REQUEST" in html and "ENABLED" in html
    assert "applyBooleanSteps" in html and "MutationObserver" in html
    assert "Object.assign(METRIC_LABELS,{rs485_ccl" in html
    assert "byId('metric-options').replaceChildren();addMetrics()" in html
    assert "renderDayBoundaries" in html and "for(const marker of data.overlays)" in html


def test_empty_phase_wrench_and_cell_identification_are_explicit():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "Keine Messdaten in diesem Zeitfenster" in html
    assert "Es werden keine Ersatzwerte erzeugt" in html
    assert "🔧" in html
    assert "phase_analysis?.intervals" in html
    assert "Zelle '+key" in html
    assert "COLORS=[" in html
    assert "emptyMarkerTop" in html
    assert "pointer-events:none" in html
    assert "if(!points.length){byId('tooltip').hidden=true;return}" not in html


def test_visual_areas_are_permanent_german_and_help_is_dynamic_and_focused():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert 'id="show-phases" type="hidden" value="on"' in html
    assert "Phasen anzeigen" not in html and ">EIN<" not in html and ">AUS<" not in html
    assert "data.phase_analysis?.intervals||[]" in html
    assert html.index('id="phase-layer"') < html.index('id="series-layer"') < html.index('id="marker-layer"')
    assert "byId('show-phases').onchange" not in html
    assert 'id="help-open"' in html and "fetch('api/config')" in html
    assert ">Bereiche erklären</button>" in html
    assert "Farbige Bereiche im Zeitverlauf" in html
    labels = ["1. Entladung", "2. Tiefbereich", "3. Ladung", "4. Hochbereich"]
    assert [html.index(label) for label in labels] == sorted(html.index(label) for label in labels)
    for key in (
        "cell_diag_discharge_current_a",
        "cell_diag_low_soc_percent",
        "cell_diag_charge_current_a",
        "cell_diag_high_soc_percent",
    ):
        assert key in html
    assert "Entladung + Hochbereich" in html
    assert "Ruhe und Unbekannt" in html
    for unrelated in (
        "Diagnostic Phase:",
        "Visual Phase Projection:",
        "Bewertungsstufen",
        "Evidenz und Confidence",
        "Hersteller-SOH",
        "Cycle Count",
        "confidence-help",
    ):
        assert unrelated not in html


def test_multicell_controls_are_wired_to_dom_and_query():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert '<fieldset id="voltage-cell-picker" hidden>' in html
    assert '<fieldset id="temperature-cell-picker" hidden>' in html
    assert "input.onchange=updateControlState" in html
    assert "byId(kind+'-cells-all').onclick=()=>setCells(kind,true)" in html
    assert "byId(kind+'-cells-none').onclick=()=>setCells(kind,false)" in html
    assert "q.set('cell_numbers',cells.join(','))" in html
    assert "q.set(param,cells.join(','))" in html
    assert "selectedCells(kind).length" in html
    assert "Aktivitätsfilter" not in html


def test_single_is_default_and_combined_mode_has_independent_stacked_tracks():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert '<option value="single">Einzel</option><option value="combined">Vergleich</option>' in html
    assert '<fieldset id="metric-picker" hidden>' in html
    assert "Alle Messgrößen" in html and 'id="metrics-none"' in html
    for metric in ("soc", "current", "cell_voltage", "cell_temperature"):
        assert metric in html
    assert "voltage_cell_numbers" in html and "temperature_cell_numbers" in html
    assert 'id="combined-charts" class="track-stack" hidden' in html
    assert "for(const series of data.series)renderTrack(series,data)" in html
    assert "syncTracks(time)" in html and "class:'sync-cursor'" in html
    assert "unit=UNITS[metric]" in html


def test_explicit_apply_loading_dirty_and_error_states_preserve_charts():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    for text in ("ZEITFENSTER GELADEN", "AUSWAHL GEÄNDERT", "WIRD GELADEN",
                 "Wird geladen …", "Daten werden geladen …"):
        assert text in html
    assert "if(!e||activeRequest)return" in html
    assert "submitButton.disabled=active" in html
    assert "const generation=++requestGeneration" in html
    assert "if(generation!==requestGeneration||controller.signal.aborted)return" in html
    assert "Zeitverlauf konnte nicht geladen werden: " in html
    assert "currentData=null" not in html.split("load=async function", 1)[1]


def test_comparison_module_picker_and_responsive_grouped_controls():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert "module-picker" in html and "module-options" in html
    assert "modules-all" in html and "modules-none" in html
    assert "grid-template-columns:repeat(3,minmax(150px,1fr))" in html
    assert "@media(max-width:620px){.metric-options{grid-template-columns:1fr}" in html
    assert "moduleAware" in html and "_actual_cell_number" in html
    assert "Modul ${point.module_number} · Zelle ${point.cell_number}" in html
    assert "relabelModuleLegend" in html


def test_progressive_loading_full_window_busy_resume_cancel_and_limits_execute():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert ";load();" not in html
    helpers = "\n".join(_javascript_function(html, name) for name in
                         ("evidenceKey", "cancelHistory", "markDirty", "setLoading", "renderLoaded"))
    loader = html[html.index("// Explicit user-started, serial pagination."):html.index("socLabels=function")]
    script = r"""
const assert=require('node:assert/strict');
let requestGeneration=0,queryStatus='UNLOADED',activeRequest=null,loadedQuery=null,loadedMode=null,currentData=null,load;
const start='2026-10-20T18:30:00Z',end='2026-10-25T18:30:00Z';
let selected='from='+start+'&to='+end+'&metric=soc',drawn=[],calls=[],timers=[],waiters=[];
const elements=new Map();
const byId=id=>{if(!elements.has(id))elements.set(id,{value:id==='view-mode'?'single':'soc',hidden:false,selectedOptions:[{textContent:'SOC'}],append(){}});return elements.get(id)};
const submitButton={},loadSpinner={},loadLabel={},loadingOverlay={},queryState={},historyStop={},historyNext={};
const query=()=>({toString:()=>selected}),clear=()=>{},node=()=>({}),history={replaceState(){}},location={pathname:'/history'},exact=t=>new Date(t).toISOString();
// Real base renderer replaces currentData with a projection: raw buffer must survive.
const render=d=>{drawn.push(d);currentData={...d,series:{metric:'soc',points:[{value:999}]}}},renderCombined=render;
const setTimeout=(fn,ms)=>{timers.push(ms);queueMicrotask(fn);return 1},clearTimeout=()=>{};
let answer;
const fetch=async(url,options)=>{calls.push({url,options});return answer(url,options)};
const event={preventDefault(){}};
function page(index,total=120){const from=new Date(Date.parse(start)+index*3600000).toISOString(),to=new Date(Math.min(Date.parse(end),Date.parse(from)+3600000)).toISOString();const points=[{timestamp:from,value:27,module_serial:'A'},{timestamp:new Date(Date.parse(from)+1000).toISOString(),value:index===60?5:28,module_serial:'A'}];return {series:{metric:'soc',points},window:{from,to},overlays:[],unavailable_intervals:index===70?[{source:'hycube',from,to,reason:'missing_index'}]:[],soc_timeline:{module_series:[{metric:'soc',module_number:1,source:'pylontech',points}],hycube_series:null,policy_series:[]},phase_analysis:{available:false,unavailable_reason:'indexed_projection_unavailable',intervals:[]},pagination:{page_range:{from,to},next_cursor:index+1<total?'page-'+(index+1):null}}}
const ok=data=>({ok:true,json:async()=>data});
""" + helpers + "\n" + loader + r"""
(async()=>{
 await load();assert.equal(calls.length,0);
 answer=async url=>{const cursor=new URL('http://test/'+url).searchParams.get('cursor');return ok(page(cursor?Number(cursor.split('-')[1]):0))};
 await load(event);
 assert.equal(calls.length,120);assert.equal(timers.length,119);assert.ok(timers.every(ms=>ms===1000));
 assert.equal(historyPages,120);assert.equal(currentData.window.from,start);assert.equal(currentData.window.to,end);
 assert.equal(currentData.series.points.length,240);assert.equal(currentData.soc_timeline.module_series[0].points.length,240);
 assert.equal(Math.min(...currentData.series.points.map(p=>p.value)),5);assert.equal(currentData.series.points[2]._segment_start,true);
 assert.equal(queryState.textContent,'ZEITFENSTER GELADEN · EINGESCHRÄNKT');assert.equal(historyNext.hidden,true);
 assert.equal(currentData.unavailable_intervals.length,1);assert.equal(currentData.phase_analysis.available,false);
 assert.equal(new Date(end)-new Date(start),120*3600000); // local 20:30 -> 19:30 crosses DST
 // Six-module five-day volume, with 600 Hycube points/hour, fits the explicit cap.
 let wide=null;
 for(let i=0;i<120;i++){
  const q=page(i);const modules=Array.from({length:6},(_,m)=>({metric:'soc',source:'pylontech',module_number:m+1,points:Array.from({length:60},(_,j)=>({timestamp:new Date(Date.parse(start)+i*3600000+j*60000).toISOString(),module_number:m+1,value:j===30?m:27}))}));
  q.series={metric:'soc',points:modules.flatMap(s=>s.points)};q.soc_timeline.module_series=modules;q.soc_timeline.hycube_series={metric:'soc',source:'hycube',points:Array.from({length:600},(_,j)=>({timestamp:new Date(Date.parse(start)+i*3600000+j*6000).toISOString(),value:34}))};
  wide=historyMergePage(wide,q,{from:start,to:end});
 }
 assert.equal(historyPointCount(wide),158400);assert.equal(wide.soc_timeline.module_series.length,6);assert.equal(wide.soc_timeline.module_series[5].points.length,7200);assert.equal(wide.soc_timeline.hycube_series.points.length,72000);
 assert.equal(wide.window.to,end);assert.equal(Math.min(...wide.soc_timeline.module_series[5].points.map(p=>p.value)),5);
 // Combined tracks retain metric boundaries instead of mixing values.
 const ca=page(0),cb=page(1);ca.series=[ca.series,{metric:'current',points:[{timestamp:start,value:-7}]}];cb.series=[cb.series,{metric:'current',points:[{timestamp:end,value:8}]}];
 const combined=historyMergePage(historyMergePage(null,ca,{from:start,to:end}),cb,{from:start,to:end});assert.equal(combined.series.length,2);assert.deepEqual(combined.series[1].points.map(p=>p.value),[-7,8]);
 // Error stops serial loader without retry; resume uses the unconsumed cursor.
 let n=0;answer=async()=>++n===1?ok(page(0,3)):{ok:false,json:async()=>({error:{message:'history_busy'}})};
 const before=calls.length;await load(event);assert.equal(calls.length-before,2);assert.equal(historyPages,1);assert.equal(historyNext.hidden,false);assert.ok(queryState.textContent.includes('TEILWEISE'));
 answer=async url=>{const i=Number(new URL('http://test/'+url).searchParams.get('cursor').split('-')[1]);return ok(page(i,3))};
 await load({...event,currentTarget:historyNext});assert.equal(historyPages,3);assert.equal(currentData.series.points.length,6);
 // Cancellation and changed selection cannot accept a late response or load another page.
 answer=()=>new Promise(resolve=>waiters.push(resolve));const pending=load(event);assert.equal(waiters.length,1);const count=calls.length;cancelHistory();waiters.shift()(ok(page(0)));await pending;assert.equal(calls.length,count);assert.equal(activeRequest,null);assert.equal(submitButton.disabled,false);
 const stale=load(event);selected+='&selected_modules=1,2';markDirty();waiters.shift()(ok(page(0)));await stale;assert.notEqual(loadedQuery,selected);
 // Bounded browser memory: fail before replacing accepted buffer, no thinning.
 const huge=page(0);huge.series.points=Array.from({length:HISTORY_POINT_LIMIT+1},()=>({timestamp:start,value:1}));
 assert.throws(()=>historyMergePage(null,huge,{from:start,to:end}),/Darstellungsgrenze/);
 // Empty page and policy gaps remain gaps; extrema and conflict metadata survive.
 const first=page(0,3),empty=page(1,3),last=page(2,3);empty.series.points=[];empty.soc_timeline.module_series[0]={...empty.soc_timeline.module_series[0],points:[]};last.series.points[0].conflict=true;
 const merged=historyMergePage(historyMergePage(historyMergePage(null,first,{from:start,to:end}),empty,{from:start,to:end}),last,{from:start,to:end});
 assert.equal(merged.series.points.length,4);assert.equal(merged.series.points[2]._segment_start,true);assert.equal(merged.series.points[2].conflict,true);
 console.log('ok');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    assert _run_node(script).strip() == "ok"


def test_render_path_separates_identity_source_epoch_gap_and_conflict_execute():
    html=render_history_html(configuration_path="configuration",maintenance_path="maintenance",timeline_path="timeline")
    start=html.index("function evidenceKey(")
    end=html.index("function render(data)",start)
    helpers=html[start:end]
    script="const COLORS=['blue','red'];\n"+helpers+"""
const assert=require('node:assert/strict');
const p=(time,serial,extra={})=>({time,display:time,module_serial:serial,...extra});
const path=seriesPath([p(3,'B'),p(2,'A'),p(0,'A'),p(1,'B')],v=>v,v=>v);
assert.equal(path,'M1.00,1.00 L3.00,3.00 M0.00,0.00 L2.00,2.00');
for(const extra of [{_segment_start:true},{conflict:true},{position_history_id:'new'}, {source:'hycube'}]){
 const path=seriesPath([p(0,'A'),p(1,'A',extra)],v=>v,v=>v);
 assert.equal((path.match(/M/g)||[]).length,2);
 assert.equal((path.match(/L/g)||[]).length,0);
}
assert.ok(seriesColor(90).startsWith('hsl('));
assert.equal(seriesColor(90),seriesColor(90));
console.log('ok');
"""
    assert _run_node(script).strip()=="ok"


def test_url_module_restore_occurs_after_dynamic_control_creation():
    html=render_history_html(configuration_path="/",maintenance_path="maintenance",timeline_path="timeline")
    reset=html.index("byId('module').value='1';addModules()")
    restored=html.index("restore();const selectionQuery")
    assert reset<restored
    assert "load();" not in html[restored:]


def test_responsive_grid_rules_do_not_span_nested_metric_labels():
    html=render_history_html(configuration_path='/',maintenance_path='maintenance',timeline_path='timeline')
    assert '.controls>label:nth-of-type(4)' in html
    assert '.controls label:nth-of-type(4)' not in html
    assert '.controls .metric-options input{min-height:auto;flex:none}' in html


def test_all_selection_fields_bind_final_dirty_and_abort_handler():
    html=render_history_html(configuration_path='/',maintenance_path='maintenance',timeline_path='timeline')
    match=re.search(r"for\(const element of document.querySelectorAll\('#controls select[^']+'\)\)element.onchange=updateControlState;",html)
    assert match
    script="""
const assert=require('node:assert/strict');let changed=0,selector;
const elements=Array.from({length:5},()=>({onchange:()=>{throw Error('stale handler')}}));
const document={querySelectorAll:s=>{selector=s;return elements}};
const updateControlState=()=>changed++;
"""+match.group()+"""
for(const element of elements)element.onchange();
assert.equal(changed,5);
for(const field of ['#module-picker input','#voltage-cell-picker input','#temperature-cell-picker input'])assert.ok(selector.includes(field));
console.log('ok');
"""
    assert _run_node(script).strip()=='ok'


@pytest.mark.parametrize('combined', [False, True])
def test_actual_renderers_show_isolated_observations_without_bridging(combined):
    html = render_history_html(configuration_path='/', maintenance_path='maintenance', timeline_path='timeline')
    helpers = html[html.index('function evidenceKey('):html.index('function render(data)')]
    renderer = _javascript_function(html, 'renderTrack(series,data)' if combined else 'render(data)')
    script = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(tag='div',attrs={}){this.tag=tag;this.attrs=attrs;this.children=[];this.style={};this.clientWidth=640;this.clientHeight=380;this.classList={add(){},remove(){}}}
 append(...items){this.children.push(...items)}
 prepend(...items){this.children.unshift(...items)}
 setAttribute(k,v){this.attrs[k]=String(v)}
 getAttribute(k){return this.attrs[k]}
}
const svg=(tag,attrs)=>new Element(tag,attrs),node=tag=>new Element(tag),text=()=>node('text');
const document={createElement:node,createTextNode:()=>node('text')};
let elements={},lastTrack,currentData,trackContexts=[];
const byId=id=>elements[id]||(elements[id]=new Element());
const clear=id=>byId(id).children=[];
function trackShell(){lastTrack={wrap:node(),plot:node(),empty:node(),legend:node(),tooltip:node(),layers:Object.fromEntries(['phase','grid','day','axis','series','marker','interaction'].map(k=>[k,node()]))};return lastTrack}
const renderDayBoundaries=()=>{},timeLabel=()=>'',decimals=()=>1,niceStep=()=>1,hideTrackCursors=()=>{};
const COLORS=['#1565c0'],UNITS={soc:'%'};
const annotatePoint=p=>({...p});
""" + helpers + renderer + '\nconst combined=' + json.dumps(combined) + r""";
const p=(time,extra={})=>({time,display:50,module_serial:'A',...extra});
function draw(points){
 elements={};const data={series:{metric:'soc',points},window:{from:'1970-01-01T00:00:00Z',to:'1970-01-01T00:00:01Z'},overlays:[]};
 if(combined)renderTrack(data.series,data);else render(data);
 const layer=combined?lastTrack.layers.series:byId('series-layer');
 return {paths:layer.children.filter(e=>e.tag==='path'),dots:layer.children.filter(e=>e.tag==='circle')};
}
function check(points,count,lines){
 const before=JSON.stringify(points),{paths,dots}=draw(points);
 assert.equal(dots.length,count);assert.equal(paths.reduce((n,p)=>n+(p.attrs.d.match(/L/g)||[]).length,0),lines);
 for(const dot of dots){assert.equal(dot.attrs.fill,'#1565c0');assert.ok(Number(dot.attrs.r)>0);assert.ok(Number.isFinite(Number(dot.attrs.cx)));assert.ok(Number.isFinite(Number(dot.attrs.cy)));assert.ok(paths.some(path=>path.attrs.d.includes('M'+dot.attrs.cx+','+dot.attrs.cy)))}
 assert.equal(JSON.stringify(points),before);
}
check([p(100)],1,0);
check([p(100),p(100)],1,0);
check([p(100),p(200)],0,1);
for(const boundary of [{_segment_start:true},{conflict:true},{module_serial:'B'},{position_history_id:'new'},{source:'hycube'}])check([p(100),p(200,boundary)],2,0);
check([p(100),p(200),p(300,{_segment_start:true})],1,1);
check([p(100,{display:NaN})],0,0);
console.log('ok');
"""
    assert _run_node(script).strip() == 'ok'
