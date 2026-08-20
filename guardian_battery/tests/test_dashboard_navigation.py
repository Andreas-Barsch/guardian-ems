from pathlib import Path

import yaml


def test_dashboard_has_prominent_guardian_analysis_navigation():
    path = Path(__file__).resolve().parents[1] / "dashboards" / "guardian_cell_diagnostics.yaml"
    dashboard = yaml.safe_load(path.read_text(encoding="utf-8"))
    cards = dashboard["views"][0]["cards"]
    card = next(item for item in cards if item.get("name") == "Zeitverläufe & Analyse")
    assert card["type"] == "button"
    assert card["tap_action"] == {
        "action": "navigate",
        "navigation_path": "/hassio/ingress/3195b09a_guardian_battery/history",
    }


def test_dashboard_has_all_direct_guardian_navigation_without_portal():
    path = Path(__file__).resolve().parents[1] / "dashboards" / "guardian_cell_diagnostics.yaml"
    cards = yaml.safe_load(path.read_text(encoding="utf-8"))["views"][0]["cards"]
    targets = {item.get("name"): item.get("tap_action", {}).get("navigation_path") for item in cards}
    assert targets["Module & Stack"].endswith("/module-information")
    assert targets["Maintenance & Verlauf"].endswith("/timeline")
    assert targets["Konfiguration"].endswith("/configuration")
    assert targets["Zeitverläufe & Analyse"].endswith("/history")


def test_home_assistant_deployment_dashboard_has_same_direct_targets():
    path = Path(__file__).resolve().parents[2] / "homeassistant" / "dashboards" / "guardian_cell_diagnostics.yaml"
    cards = yaml.safe_load(path.read_text(encoding="utf-8"))["views"][0]["cards"]
    targets = {item.get("name"): item.get("tap_action", {}).get("navigation_path") for item in cards}
    assert targets["Module & Stack"].endswith("/module-information")
    assert targets["Zeitverläufe & Analyse"].endswith("/history")
    assert targets["Maintenance & Verlauf"].endswith("/timeline")
    assert targets["Konfiguration"].endswith("/configuration")
