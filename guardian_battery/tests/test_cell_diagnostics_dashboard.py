from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS = (
    ROOT / "guardian_battery" / "dashboards" / "guardian_cell_diagnostics.yaml",
    ROOT / "homeassistant" / "dashboards" / "guardian_cell_diagnostics.yaml",
)


def _cell_views(path: Path) -> list[dict]:
    dashboard = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        view
        for view in dashboard["views"]
        if str(view.get("path", "")).startswith("module-")
        and "-cell-" in str(view.get("path", ""))
    ]


def _card(view: dict, title: str) -> dict:
    return next(card for card in view["cards"] if card.get("title") == title)


def test_all_cell_views_separate_momentary_values_and_historic_evidence():
    for path in DASHBOARDS:
        views = _cell_views(path)
        assert len(views) == 90
        assert len({view["path"] for view in views}) == 90
        assert {view["path"] for view in views} == {
            f"module-{module}-cell-{cell}"
            for module in range(1, 7)
            for cell in range(1, 16)
        }
        for view in views:
            summary = view["cards"][0]["content"]
            assert "Gesamtbewertung: {{ s }}" in summary
            assert "### Momentanwerte" in summary
            assert "Aktuelle Abweichung zum Modulmedian" in summary
            assert "### Diagnostische Evidenz" in summary
            assert "**Confidence:** {{ conf }}" in summary
            assert "Maßgeblicher Diagnosebereich" in summary
            assert "Betrag der Evidenzabweichung" in summary
            assert "state_attr(status_entity, 'evidence_phase')" in summary
            assert "else '🟠' if s == 'AUFFÄLLIG' else '🔴'" in summary


def test_all_cell_views_use_engine_results_and_consistent_phase_order_and_colors():
    expected = (
        ("1. Entladung", "#ef5350"),
        ("2. Tiefbereich", "#ffb300"),
        ("3. Ladung", "#42a5f5"),
        ("4. Hochbereich", "#ab47bc"),
    )
    for path in DASHBOARDS:
        for view in _cell_views(path):
            evidence = _card(view, "Phasenbezogene Evidenz")["content"]
            positions = [evidence.index(label) for label, _ in expected]
            assert positions == sorted(positions)
            for label, color in expected:
                assert f'<span style="color:{color}">●</span> {label}' in evidence
            assert "state_attr(discharge_entity, 'status')" in evidence
            assert "state_attr(discharge_entity, 'thresholds_mv')" in evidence
            assert "**Noch keine ausreichenden Messdaten**" in evidence
            assert "0 gültige Messpunkte – noch keine Bewertung möglich." in evidence
            assert "Noch keine statuswirksame Bewertung" in evidence
            assert "Confidence" not in evidence
            assert evidence.count("**Erforderliche Mindestanzahl:** {{ min_n }}") == 8
            assert "Lowest-Anteil" in evidence and "Highest-Anteil" in evidence
            assert "Mittlerer Rang" in evidence
            assert "Bereichsfarbe kennzeichnet den Betriebsbereich" in evidence
            assert "'NORMAL':'🟢'" in evidence
            assert "'BEOBACHTEN':'🟡'" in evidence
            assert "'AUFFÄLLIG':'🟠'" in evidence
            assert "'KRITISCH':'🔴'" in evidence


def test_all_cell_views_explain_the_actual_aggregation_semantics():
    for path in DASHBOARDS:
        for view in _cell_views(path):
            help_card = _card(view, "ⓘ Bewertung erklären")
            assert help_card["title"] == "ⓘ Bewertung erklären"
            content = help_card["content"]
            assert "schlechtesten ausreichend belegten Bereich" in content
            assert "größere absolute Medianabweichung" in content
            assert "Zellspannung − Median der 15 Zellspannungen" in content
            assert "+43 mV und −43 mV" in content
            assert "min_phase_samples" in content
            assert "confidence_medium_samples" in content
            assert "confidence_high_samples" in content
            assert "Confidence ist kein Gesundheitsprozentwert" in content
            labels = ["1. **Entladung:**", "2. **Tiefbereich:**", "3. **Ladung:**", "4. **Hochbereich:**"]
            assert [content.index(label) for label in labels] == sorted(content.index(label) for label in labels)
            for phase in ("discharge", "low", "charge", "high"):
                for threshold in ("observe", "warning", "critical"):
                    assert f"'{phase}_{threshold}_mv'" in content
            assert "unter {{ state_attr" in content
            assert "= NORMAL" in content
            assert "= BEOBACHTEN" in content
            assert "= AUFFÄLLIG" in content
            assert "= KRITISCH" in content


def test_threshold_help_changes_with_active_configuration_and_has_no_fixed_defaults():
    content = _card(_cell_views(DASHBOARDS[0])[0], "ⓘ Bewertung erklären")["content"]
    pattern = re.compile(
        r"{{ state_attr\('sensor\.guardian_battery_zelldiagnostik_konfiguration', '([^']+)'\) }}"
    )

    def render_thresholds(values):
        return pattern.sub(lambda match: str(values[match.group(1)]), content)

    keys = {
        f"{phase}_{threshold}_mv"
        for phase in ("discharge", "low", "charge", "high")
        for threshold in ("observe", "warning", "critical")
    }
    first = render_thresholds({key: index + 51 for index, key in enumerate(sorted(keys))})
    changed = render_thresholds({key: index + 81 for index, key in enumerate(sorted(keys))})
    assert first != changed
    assert all(f"{value} mV" in first for value in range(51, 63))
    threshold_section = content.split("**Aktuell wirksame phasenspezifische Grenzen:**", 1)[1]
    assert not re.search(r"\b(?:10|20|40) mV\b", threshold_section)


def test_all_cell_views_show_separate_predictive_dimensions_and_methods():
    for path in DASHBOARDS:
        for view in _cell_views(path):
            overview = _card(view, "Diagnose, Trend & Maintenance Risk")["content"]
            methods = _card(view, "Zusätzliche Diagnoseverfahren")["content"]
            contexts = _card(view, "Balancing, Maintenance & ICA/DVA")["content"]
            content = overview + methods + contexts
            assert "Current Condition" in content
            assert "Trend:" in content
            assert "Maintenance Risk:" in content
            assert "Trend/Risk Confidence:" in content
            assert "Current-Condition-Confidence:" in content
            assert "Evidenzfamilien" in content
            assert "unabhängige qualifizierte Familien" in content
            assert "Historische Ranking-Drift" in content
            assert "Dynamic Resistance" in content
            assert "Capacity Consistency" in content
            assert "Curve Analysis" in content
            assert "Rest / Relaxation / Drift" in content
            assert "Balancing-Kontext" in content
            assert "Maintenance-Kontext" in content
            assert "ICA/DVA" in content
            assert "Kein Health Score" in content
            assert "Korrelation/Assoziation" in content
            assert "keine Kausalitätsaussage" in content
