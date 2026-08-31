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


def test_rs485_management_is_compact_adr_based_and_noncausal():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "RS485 / BMS-Management" in html
    assert "api/rs485/status" in html
    assert "Identität nicht zugeordnet" in html
    assert "keine bestätigte Ursache" in html
    assert "CCL" in html and "DCL" in html
    assert "STOP REQUEST" in html and "ENABLED" in html
    assert "Aktualität:" in html and "AKTUELL" in html and "VERALTET" in html
    assert "management_freshness_seconds||600" in html
    assert "stale?'nicht verfügbar'" not in html


def test_current_assignment_and_history_use_physical_top_to_bottom_order():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "for(let number=6;number>=1;number--)" in html
    assert "for(let position=6;position>=1;position--)" in html


def test_history_matrix_is_structurally_scrollable_for_twenty_or_more_states():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert ".table-wrap{overflow-x:auto}" in html
    assert "position:sticky;left:0" in html
    assert "matrixLimit=20" in html
    assert "matrixLimit+=20" in html


def test_module_information_escapes_navigation_paths():
    html = render_module_information_html(configuration_path='"><script>', maintenance_path='" onclick="x')
    assert '"><script>' not in html and 'onclick="x' not in html
