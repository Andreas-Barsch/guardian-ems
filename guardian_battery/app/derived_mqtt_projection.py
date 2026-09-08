"""Single authoritative MQTT projection for derived-only cell diagnostics."""
from cell_diagnostics import DIAGNOSTIC_PARAMETER_META
from mqtt_projection import compact_cell_attributes


def _diag_meta(metric, phase=None):
    meta = dict(DIAGNOSTIC_PARAMETER_META.get(metric, {}))
    if phase:
        labels = {"low": "Tiefbereich", "discharge": "Entladung",
                  "charge": "Ladung", "high": "Hochbereich", "rest": "Ruhe"}
        meta["betriebsphase"] = labels.get(phase, phase)
    meta["method"] = "Phase-Resolved Cell Voltage Consistency"
    meta["soh_hinweis"] = "Guardian-Zellkonsistenzdiagnostik; nicht mit Pylontech BMS SOH verrechnen."
    return meta


def publish_derived_module(sink, position, diag):
    """Publish one module in the legacy topic/payload/order contract."""
    base = f"module_{int(position)}"
    values = {
        f"{base}_cell_median": diag.get("current_median_mv"),
        f"{base}_cell_diag_status": diag.get("status"),
        f"{base}_cell_diag_confidence": diag.get("confidence"),
        f"{base}_cell_diag_samples": diag.get("sample_count"),
        f"{base}_cell_diag_worst_cell": diag.get("evidence_worst_cell"),
        f"{base}_cell_diag_evidence": diag.get("evidence_deviation_mv"),
        f"{base}_cell_diag_trend": diag.get("trend"),
        f"{base}_cell_diag_maintenance_risk": diag.get("maintenance_risk"),
        f"{base}_cell_diag_trend_risk_confidence": diag.get("trend_risk_confidence"),
    }
    for key, value in values.items():
        sink.state(key, value)
    if diag.get("current_median_mv") is not None:
        sink.attributes(f"{base}_cell_median", {
            "label": "Modul-Zellmedian", "unit": "mV",
            "source": "Guardian-Berechnung aus Pylontech bat <module>",
            "definition": "Median der 15 gleichzeitig erfassten Zellspannungen des Moduls.",
            "interpretation": "Referenzlinie für die relative Zellkonsistenz. Einzelne Zellspannungen werden relativ zu diesem Median betrachtet.",
            "method": "Median(V1…V15)",
            "soh_hinweis": "Kein SOH-Wert und keine eigenständige Zellgesundheitsbewertung.",
        })
    for cell in diag.get("cells", []):
        cp = f"{base}_cell_{cell['cell']}"
        sink.state(f"{cp}_status", cell.get("status")); sink.state(f"{cp}_confidence", cell.get("confidence"))
        sink.state(f"{cp}_voltage", cell.get("current_voltage_mv")); sink.state(f"{cp}_deviation", cell.get("current_deviation_mv")); sink.state(f"{cp}_evidence", cell.get("evidence_deviation_mv"))
        phases = cell.get("phases", {})
        for phase in ("low", "discharge", "charge", "high"):
            sink.state(f"{cp}_{phase}_deviation", phases.get(phase, {}).get("median_deviation_mv"))
        sink.state(f"{cp}_low_lowest", phases.get("low", {}).get("lowest_percent")); sink.state(f"{cp}_discharge_lowest", phases.get("discharge", {}).get("lowest_percent"))
        for phase in ("low", "discharge", "charge", "high"):
            sink.state(f"{cp}_{phase}_rank", phases.get(phase, {}).get("mean_rank"))
        sink.state(f"{cp}_charge_highest", phases.get("charge", {}).get("highest_percent")); sink.state(f"{cp}_high_highest", phases.get("high", {}).get("highest_percent"))
        for phase in ("low", "discharge", "charge", "high"):
            sink.state(f"{cp}_{phase}_samples", phases.get(phase, {}).get("samples"))
        status_meta = _diag_meta("status") | compact_cell_attributes(
            cell, diag.get("advanced_diagnostics", {}))
        status_meta["physical_group"] = ((int(cell["cell"]) - 1) // 5) + 1
        status_meta["physical_group_cells"] = {1: "1-5", 2: "6-10", 3: "11-15"}[status_meta["physical_group"]]
        status_meta["evidence_phase"] = cell.get("evidence_phase")
        status_meta["evidence_deviation_mv"] = cell.get("evidence_deviation_mv")
        sink.attributes(f"{cp}_status", status_meta)
        sink.attributes(f"{cp}_confidence", _diag_meta("confidence")); sink.attributes(f"{cp}_voltage", _diag_meta("voltage")); sink.attributes(f"{cp}_deviation", _diag_meta("deviation")); sink.attributes(f"{cp}_evidence", _diag_meta("evidence"))
        for phase in ("low", "discharge", "charge", "high"):
            meta = _diag_meta("deviation", phase); phase_result = phases.get(phase, {})
            meta["status"] = phase_result.get("status", "LERNPHASE"); meta["thresholds_mv"] = phase_result.get("thresholds_mv", {}); meta["samples"] = phase_result.get("samples", 0)
            sink.attributes(f"{cp}_{phase}_deviation", meta); sink.attributes(f"{cp}_{phase}_rank", _diag_meta("rank", phase)); sink.attributes(f"{cp}_{phase}_samples", _diag_meta("samples", phase))
        sink.attributes(f"{cp}_low_lowest", _diag_meta("lowest", "low")); sink.attributes(f"{cp}_discharge_lowest", _diag_meta("lowest", "discharge")); sink.attributes(f"{cp}_charge_highest", _diag_meta("highest", "charge")); sink.attributes(f"{cp}_high_highest", _diag_meta("highest", "high"))


def publish_derived_results(sink, positions, results):
    for position in positions:
        publish_derived_module(sink, position, results.get(position, {}))
