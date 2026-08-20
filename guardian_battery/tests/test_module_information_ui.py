from module_information_ui import render_module_information_html


def test_module_information_exposes_physical_identity_and_position_intervals():
    html = render_module_information_html(configuration_path="/", maintenance_path="maintenance")
    assert "Physische Module" in html
    assert "Seriennummer ist die dauerhafte Identität" in html
    assert 'id="physical-history"' in html
    assert "serial_histories" in html
    assert "valid_from" in html and "valid_to" in html
    assert "maintenance?event_id=" in html


def test_module_information_escapes_navigation_paths():
    html = render_module_information_html(configuration_path='"><script>', maintenance_path='" onclick="x')
    assert '"><script>' not in html and 'onclick="x' not in html
