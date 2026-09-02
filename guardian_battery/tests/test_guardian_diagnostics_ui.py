import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from guardian_diagnostics_ui import render_guardian_diagnostics_html


def test_diagnostics_ui_structure_semantics_and_escaping():
    html = render_guardian_diagnostics_html(
        api_path='api/diagnostics?<script>', modules_path='"><script>alert(1)</script>')
    for text in ("Guardian Diagnostics", "Gesamtübersicht", "Tagesdiagnosen",
                 "Datumsauswahl", "BMS Management Evidence", "Kausalität",
                 "Info und Hilfe", "Seriennummer", "complete", "partial",
                 "Nicht genügend Historie", "CCL", "DCL", "0x44"):
        assert text in html
    assert "@media(max-width:800px)" in html
    assert "range(1,7)" not in html
    assert '"><script>alert(1)</script>' not in html
    assert "textContent" in html or "esc(" in html
    assert 'aria-current="page" href="diagnostics">Guardian Diagnostics</a>' in html
    default_html = render_guardian_diagnostics_html()
    assert 'href="./">Module &amp; Stack</a>' in default_html
    for target in ("configuration", "maintenance", "timeline", "history", "diagnostics"):
        assert f'href="{target}"' in default_html
    assert "/api/hassio_ingress/" not in html
    assert "3195b09a_guardian_battery" not in html


def test_home_assistant_source_has_no_static_diagnostics_ingress_dashboard():
    root = Path(__file__).resolve().parents[2]
    loader = type("HomeAssistantLoader", (yaml.SafeLoader,), {})
    loader.add_multi_constructor("!", lambda instance, _suffix, node:
                                 instance.construct_scalar(node))
    configuration = yaml.load((root / "homeassistant/configuration.yaml").read_text(),
                              Loader=loader)
    dashboards = configuration["lovelace"]["dashboards"]
    assert "guardian-diagnostics" not in dashboards
    assert "guardian-cell-diagnostics" in dashboards
    assert "guardian-module-information" in dashboards
    assert not (root / "homeassistant/dashboards/guardian_diagnostics.yaml").exists()


def test_source_never_persists_addon_slug_as_ingress_session_token():
    root = Path(__file__).resolve().parents[2]
    forbidden = "/api/hassio_ingress/3195b09a_guardian_battery/"
    checked = [root / "guardian_battery/app", root / "homeassistant"]
    matches = []
    for directory in checked:
        for path in directory.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".md", ".js"}:
                if forbidden in path.read_text(encoding="utf-8"):
                    matches.append(path.relative_to(root))
    assert matches == []
