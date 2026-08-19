from maintenance_ui import maintenance_deep_link, render_maintenance_html


def test_navigation_contains_configuration_and_maintenance_without_timeline():
    html = render_maintenance_html(configuration_path="/dynamic/ingress/token/")

    assert 'href="/dynamic/ingress/token/"' in html
    assert 'href="maintenance"' in html
    assert "Konfiguration" in html
    assert "Maintenance-Logbuch" in html
    assert "Timeline" not in html
    assert "3195b09a_guardian_battery" not in html


def test_deep_link_uses_only_stable_encoded_event_identity():
    event_id = "MEV-12345678-1234-4123-8123-123456789abc"

    assert maintenance_deep_link(event_id) == f"maintenance?event_id={event_id}"
    assert maintenance_deep_link("MEV-value with spaces") == "maintenance?event_id=MEV-value%20with%20spaces"


def test_ui_has_list_filters_crud_archive_restore_history_and_pagination():
    html = render_maintenance_html(configuration_path="/")

    required_ids = (
        "filter-from",
        "filter-to",
        "filter-category",
        "filter-module",
        "filter-cell",
        "filter-archived",
        "filter-sort",
        "new-button",
        "event-form",
        "edit-button",
        "archive-button",
        "restore-button",
        "history-list",
        "page-prev",
        "page-next",
    )
    for element_id in required_ids:
        assert f'id="{element_id}"' in html
    assert "expected_revision:state.current.revision" in html
    assert "window.confirm('Diesen Maintenance-Eintrag archivieren?" in html


def test_time_ui_distinguishes_event_capture_and_update_semantics():
    html = render_maintenance_html(configuration_path="/")

    assert "Ereigniszeitpunkt" in html
    assert "Erfasst am" in html
    assert "Zuletzt geändert" in html
    assert "lokale Browserzeit" in html
    assert "resolvedOptions().timeZone" in html
    assert "return d.toISOString()" in html
    assert "Diese lokale Uhrzeit existiert wegen einer Zeitumstellung nicht" in html
    assert "Speicherwert:" in html


def test_user_data_is_rendered_with_dom_text_apis_not_html_injection():
    html = render_maintenance_html(configuration_path="/")

    assert ".innerHTML" not in html
    assert "history.replaceChildren()" not in html
    assert "byId('history-list').replaceChildren()" in html
    assert "title.textContent=event.title" in html
    assert "dd.textContent=String(value)" in html
    assert "detail-description').textContent" in html
    assert "row.textContent=`Revision" in html
    assert "<script>alert(1)</script>" not in html


def test_server_supplied_navigation_path_is_html_escaped():
    html = render_maintenance_html(
        configuration_path='/" onmouseover="alert(1)<script>'
    )

    assert 'onmouseover="alert(1)' not in html
    assert "<script><script>" not in html
    assert "&quot;" in html
    assert "&lt;script&gt;" in html


def test_conflict_keeps_form_and_requires_explicit_reload():
    html = render_maintenance_html(configuration_path="/")

    assert "Dieser Eintrag wurde zwischenzeitlich geändert." in html
    assert "Ihre ungespeicherten Eingaben bleiben im Formular erhalten." in html
    assert "conflict-box').hidden=false" in html
    assert "conflict-reload" in html
    assert "Aktuelle Version neu laden" in html


def test_responsive_layout_has_mobile_breakpoints_without_table_dependency():
    html = render_maintenance_html(configuration_path="/")

    assert "@media(max-width:800px)" in html
    assert "@media(max-width:560px)" in html
    assert "class=\"event-card\"" not in html  # cards are created safely via DOM
    assert "document.createElement('a')" in html
    assert "<table" not in html
