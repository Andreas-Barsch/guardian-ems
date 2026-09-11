import json
from pathlib import Path
import shutil
import subprocess

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
    if(selector==='path.series') return [];
    return [];
  }}
  get lastChild() {{ return this.children[this.children.length-1]; }}
  setAttribute(key,value) {{ this.attrs[key]=value; }}
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
relabelSocLegend(root,{{soc_timeline:{{}}}});
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
    assert "data.series.points.map" in html
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
    for text in ("AKTUELL", "AUSWAHL GEÄNDERT", "WIRD GELADEN",
                 "Wird geladen …", "Daten werden geladen …"):
        assert text in html
    assert "queryStatus==='LOADING'" in html
    assert "submitButton.disabled=active" in html
    assert "const generation=++requestGeneration" in html
    assert "if(generation!==requestGeneration)return" in html
    assert "Zeitverlauf konnte nicht geladen werden. Bitte erneut versuchen." in html
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
