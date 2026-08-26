from history_ui import render_history_html


def test_generic_soc_and_cell_chart_with_touch_markers_and_responsive_layout():
    html = render_history_html(configuration_path="/x/", maintenance_path="/x/maintenance", timeline_path="/x/timeline")
    assert "SOC [%]" in html and "Zellspannung [V]" in html
    assert "api/history/series" in html
    assert "class:'marker'" in html
    assert "showMarker(marker)" in html
    assert "Maintenance-Eintrag öffnen" in html
    assert "@media(max-width:390px)" in html
    assert "overflow:hidden" in html


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
    for layer in ("phase-layer", "grid-layer", "series-layer", "marker-layer", "interaction-layer"):
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
    assert '<option value="single">Einzel</option><option value="combined">Gemeinsam</option>' in html
    assert '<fieldset id="metric-picker" hidden>' in html
    assert "Alle Messgrößen" in html and 'id="metrics-none"' in html
    for metric in ("soc", "current", "cell_voltage", "cell_temperature"):
        assert metric in html
    assert "voltage_cell_numbers" in html and "temperature_cell_numbers" in html
    assert 'id="combined-charts" class="track-stack" hidden' in html
    assert "for(const series of data.series)renderTrack(series,data)" in html
    assert "syncTracks(time)" in html and "class:'sync-cursor'" in html
    assert "unit=UNITS[metric]" in html
