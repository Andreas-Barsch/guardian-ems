from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = (
    ROOT / "guardian_battery" / "dashboards" / "guardian_cell_diagnostics.yaml",
    ROOT / "homeassistant" / "dashboards" / "guardian_cell_diagnostics.yaml",
)
UNWANTED_NAVIGATION = {
    "Zeitverläufe & Analyse",
    "Konfiguration",
    "Module & Stack",
    "Maintenance & Verlauf",
}


def _overview(path: Path) -> dict:
    dashboard = yaml.safe_load(path.read_text(encoding="utf-8"))
    return next(view for view in dashboard["views"] if view.get("path") == "overview")


@pytest.mark.parametrize("path", DASHBOARDS)
def test_dashboard_overview_keeps_six_ordered_module_entries(path):
    cards = _overview(path)["cards"]
    module_cards = [
        card["card"]
        for card in cards
        if card.get("type") == "conditional"
        and str(card.get("card", {}).get("name", "")).startswith("Modul ")
    ]

    assert [card["name"] for card in module_cards] == [f"Modul {n}" for n in range(1, 7)]
    assert [card["tap_action"]["navigation_path"] for card in module_cards] == [
        f"/guardian-cell-diagnostics/module-{n}" for n in range(1, 7)
    ]


@pytest.mark.parametrize("path", DASHBOARDS)
def test_dashboard_overview_has_no_guardian_function_or_ingress_tiles(path):
    overview = _overview(path)
    cards = overview["cards"]
    names = {card.get("name") for card in cards}

    assert names.isdisjoint(UNWANTED_NAVIGATION)
    assert "Konfigurationshinweis" not in {card.get("title") for card in cards}
    assert "/hassio/ingress/" not in yaml.safe_dump(overview, allow_unicode=True)


@pytest.mark.parametrize("path", DASHBOARDS)
def test_existing_cell_diagnostics_navigation_remains_local(path):
    cards = _overview(path)["cards"]
    logic = next(card for card in cards if card.get("name") == "Bewertungslogik")

    assert logic["tap_action"]["navigation_path"] == "/guardian-cell-diagnostics/bewertungslogik"
