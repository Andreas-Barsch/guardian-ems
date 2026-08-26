from module_information_ui import render_module_information_html


def test_module_information_exposes_stack_centred_change_matrix():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "Stack-Positionshistorie" in html
    assert 'id="matrix-head"' in html and 'id="matrix-body"' in html
    assert "Position ${position}" in html and "aktuell" in html
    assert ".slice().reverse().slice(0,matrixLimit)" in html and "matrixLimit+=20" in html
    assert html.index('<th class="current"') < html.index("history.map")
    assert "matrixSnapshots=allSnapshots.filter" in html
    assert "shortDate" in html and "title=" in html
    assert "item.positions" in html and "'leer'" in html
    assert "maintenance?event_id=" in html


def test_module_information_escapes_navigation_paths():
    html = render_module_information_html(configuration_path='"><script>', maintenance_path='" onclick="x')
    assert '"><script>' not in html and 'onclick="x' not in html
