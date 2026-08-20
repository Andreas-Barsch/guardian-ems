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
    assert "Alle Zellen / Modulebene" in html
    assert "grid-template-columns:minmax(135px" in html
    assert "byId('cell').disabled=!cell" in html
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


def test_visual_phase_toggle_is_central_and_layers_are_ordered():
    html = render_history_html(configuration_path="/", maintenance_path="maintenance", timeline_path="timeline")
    assert 'id="show-phases"' in html and ">EIN<" in html and ">AUS<" in html
    assert html.index('id="phase-layer"') < html.index('id="series-layer"') < html.index('id="marker-layer"')
    assert "byId('show-phases').onchange" in html
