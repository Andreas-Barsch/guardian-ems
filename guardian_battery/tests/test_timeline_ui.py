from timeline_ui import render_timeline_html


def test_navigation_filters_timeline_and_responsive_layout():
    html = render_timeline_html(configuration_path="/ingress/x/", maintenance_path="/ingress/x/maintenance")
    assert 'href="/ingress/x/"' in html
    assert 'href="/ingress/x/maintenance"' in html
    assert ">Verlauf<" in html
    for element_id in ("from", "to", "event-types", "category", "module", "cell", "archived", "timeline"):
        assert f'id="{element_id}"' in html
    assert "@media(max-width:760px)" in html
    assert "@media(max-width:520px)" in html
    assert "<table" not in html


def test_ui_uses_api_deep_link_and_safe_dom_text_only():
    html = render_timeline_html(configuration_path="/", maintenance_path="maintenance")
    assert "fetch('api/timeline?'" in html
    assert "a.href=e.deep_link" in html
    assert "maintenance?event_id=" not in html
    assert ".innerHTML" not in html
    assert "title.textContent=e.title" in html
    assert "summary.textContent=e.summary" in html
    assert "box.textContent=error.message" in html


def test_navigation_paths_are_escaped():
    html = render_timeline_html(configuration_path='"<script>', maintenance_path='" onmouseover="x')
    assert '"<script>' not in html
    assert 'onmouseover="x' not in html
    assert "&quot;" in html and "&lt;script&gt;" in html
