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


def test_home_assistant_dashboard_is_standalone_sidebar_source():
    root = Path(__file__).resolve().parents[2]
    loader = type("HomeAssistantLoader", (yaml.SafeLoader,), {})
    loader.add_multi_constructor("!", lambda instance, _suffix, node:
                                 instance.construct_scalar(node))
    configuration = yaml.load((root / "homeassistant/configuration.yaml").read_text(),
                              Loader=loader)
    dashboard = configuration["lovelace"]["dashboards"]["guardian-diagnostics"]
    assert dashboard == {"mode": "yaml", "title": "Guardian Diagnostics",
                         "icon": "mdi:chart-box-outline", "show_in_sidebar": True,
                         "filename": "dashboards/guardian_diagnostics.yaml"}
    source = yaml.safe_load((root / "homeassistant/dashboards/guardian_diagnostics.yaml").read_text())
    assert source["title"] == "Guardian Diagnostics"
    card = source["views"][0]["cards"][0]
    assert card["type"] == "iframe"
    assert card["url"].endswith("/diagnostics")
    assert "multi-ingress" not in str(source).lower()
