from pathlib import Path

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


def test_all_cell_views_separate_momentary_values_and_historic_evidence():
    for path in DASHBOARDS:
        views = _cell_views(path)
        assert len(views) == 90
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
            evidence = view["cards"][1]["content"]
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
            help_card = view["cards"][2]
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
