from maintenance_ui import render_maintenance_html


def test_serial_field_uses_simple_label_and_unknown_option():
    html = render_maintenance_html(configuration_path="/")
    assert "<label>Seriennummer <select" in html
    assert "Physisches Modul / Seriennummer" not in html
    assert "Seriennummer unbekannt" in html
    assert "nur Position / keine eindeutige Zuordnung" not in html


def test_serial_selection_prioritizes_stored_then_effective_identity():
    html = render_maintenance_html(configuration_path="/")
    assert "event?.module_serial||effective||''" in html
    assert "Zum Ereigniszeitpunkt: " in html
    assert "Früher an dieser Position: " in html
    assert "Später an dieser Position: " in html
