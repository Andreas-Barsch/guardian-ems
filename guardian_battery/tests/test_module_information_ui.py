from module_information_ui import render_module_information_html


def test_module_information_exposes_stack_centred_change_matrix():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "Stack-Positionshistorie" in html
    assert 'id="matrix-head"' in html and 'id="matrix-body"' in html
    assert "Position ${position}" in html
    assert ".slice().reverse().slice(0,matrixLimit)" in html and "matrixLimit+=20" in html
    assert "history.map(item=>`<th" in html
    assert '<th class="current"' not in html
    assert "matrixSnapshots=allSnapshots.filter" in html
    assert "shortDate" in html and "title=" in html
    assert "item.positions" in html and "'leer'" in html
    assert "maintenance?event_id=" in html


def test_rs485_management_is_module_and_serial_based_and_noncausal():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "RS485 / BMS-Management" in html
    assert "api/rs485/status" in html
    assert "<th>Modul</th><th>Seriennummer</th>" in html
    assert "Modul nicht zugeordnet" in html
    assert "Seriennummer nicht verfügbar" in html
    assert "<th>ADR</th>" not in html and "Identität nicht zugeordnet" not in html
    assert "keine bestätigte Ursache" in html
    assert "CCL" in html and "DCL" in html
    assert "STOP REQUEST" in html and "ENABLED" in html
    assert "Aktualität:" in html and "AKTUELL" in html and "VERALTET" in html
    assert "management_freshness_seconds||600" in html
    assert "Identität aus Evidence wiederhergestellt" in html
    assert "stale?'nicht verfügbar'" not in html
    assert "item.discharge_current_limit_a" in html
    assert "enabled(item.discharge_enable)" in html


def test_position_history_headers_include_local_time():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "hour:'2-digit'" in html and "minute:'2-digit'" in html


def test_current_assignment_and_history_use_physical_top_to_bottom_order():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "for(let number=6;number>=1;number--)" in html
    assert "for(let position=6;position>=1;position--)" in html
    assert "Status / Aktualität" in html
    assert "fehlt / entfernt" in html
    assert "nicht erwartet" in html
    assert "vorhanden, aber nicht erwartet" in html
    assert "current.documented?.[number]||'Seriennummer unbekannt'" in html
    assert "presence.observed_serial||'nicht verfügbar'" in html


def test_history_matrix_is_structurally_scrollable_for_twenty_or_more_states():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert ".table-wrap{overflow-x:auto}" in html
    assert "position:sticky;left:0" in html
    assert "matrixLimit=20" in html
    assert "matrixLimit+=20" in html


def test_module_information_escapes_navigation_paths():
    html = render_module_information_html(configuration_path='"><script>', maintenance_path='" onclick="x')
    assert '"><script>' not in html and 'onclick="x' not in html


def test_collector_timing_uses_existing_status_snapshot_and_german_labels():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "Collector Timing" in html
    assert "Prozesslokale Laufzeit-Evidence" in html
    assert "data.collector_timing||{}" in html
    assert "fetch('api/rs485/status')" in html
    assert "Hauptpoll Ziel" in html and "Effektives Hauptpoll-Intervall" in html
    assert "Letzte Zyklusdauer" in html
    assert "Letzte Cell-Zyklusdauer" in html and "Cell Deadline Lateness" in html
    assert "Stack Sample Spread" in html
    assert "keinen Batterie-, Modul- oder Alarmstatus" in html
    assert "beginnen nach einem Neustart neu" in html


def test_collector_timing_preserves_missing_values_and_zero_overruns():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "value===null||value===undefined" in html
    assert "!Number.isFinite(Number(value))?'nicht verfügbar'" in html
    assert "Number(value).toLocaleString('de-DE'" in html
    assert "timingCount(timing.cycle_overrun_count)" in html
    assert "timingCount(timing.cell_overrun_count)" in html
    assert "timing.cycle_overrun_count||" not in html
    assert "timing.cell_overrun_count||" not in html


def test_collector_timing_renders_full_partial_and_empty_snapshots():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    for key in (
        "poll_target_s", "cycle_duration_seconds", "effective_poll_interval_seconds",
        "cycle_overrun_max_seconds", "cell_target_s", "cell_cycle_duration_seconds",
        "effective_cell_sampling_interval_seconds", "cell_deadline_lateness_seconds",
        "cell_overrun_max_seconds", "cell_stack_sample_spread_seconds",
    ):
        assert f"timing.{key}" in html
    assert "renderCollectorTiming(data)" in html
    assert "renderCollectorTiming({})" in html
    assert "nicht verfügbar" in html


def test_collector_component_and_bat_details_are_bounded():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    for label in (
        "PWR Request", "PWR Processing", "STAT / INFO", "BAT Requests gesamt",
        "Identity Resolution", "Cell History Write", "Cell Analysis",
        "Diagnostic Store Save", "Aggregate Write", "MQTT Projection",
        "Topology / Position", "Remaining Other",
    ):
        assert label in html
    assert "timing.rolling?.bat_request||{}" in html
    assert "BAT Count" in html and "BAT Median" in html and "BAT Maximum" in html
    assert "bat.count" in html and "bat.median_seconds" in html and "bat.max_seconds" in html
    assert "Object.entries(timing.rolling" not in html


def test_collector_cell_intervals_sort_numerically_by_position_then_serial():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "effective_cell_intervals_by_serial||{}" in html
    assert "positionBySerial.set(item.serial_string,Number(item.position))" in html
    assert "(a.position??99)-(b.position??99)||a.serial.localeCompare" in html
    assert "Modul ${item.position}" in html
    assert "values?.last_seconds" in html


def test_collector_timing_is_responsive_without_changing_management_projection():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert ".timing-grid{display:grid;grid-template-columns:repeat(2" in html
    assert ".grid,.timing-grid{grid-template-columns:1fr}" in html
    assert "item.discharge_current_limit_a" in html
    assert "enabled(item.discharge_enable)" in html
    assert "management_freshness_seconds||600" in html
