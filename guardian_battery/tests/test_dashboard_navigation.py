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


def test_dashboard_has_direct_stack_and_module_information_navigation():
    path = Path(__file__).resolve().parents[1] / "dashboards" / "guardian_cell_diagnostics.yaml"
    cards = yaml.safe_load(path.read_text(encoding="utf-8"))["views"][0]["cards"]
    targets = {item.get("name"): item.get("tap_action", {}).get("navigation_path") for item in cards}
    assert targets["Stack & Module"].endswith("/module-information#stack-and-modules")
    assert targets["Modulinformationen"].endswith("/module-information#module-information")
