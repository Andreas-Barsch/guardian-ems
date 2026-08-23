from datetime import datetime, timezone

from cell_diagnostics import CellDiagnosticStore, CellSample
from evidence_diagnostics import EvidenceDiagnostics


def options(**changes):
    value = {
        "cell_diag_low_soc_percent": 30, "cell_diag_high_soc_percent": 80,
        "cell_diag_charge_current_a": .8, "cell_diag_discharge_current_a": .8,
        "cell_diag_min_phase_samples": 2, "cell_diag_confidence_medium_samples": 4,
        "cell_diag_confidence_high_samples": 8, "cell_diag_observe_deviation_mv": 10,
        "cell_diag_warning_deviation_mv": 20, "cell_diag_critical_deviation_mv": 40,
        "cell_diag_trend_min_days": 3, "cell_diag_trend_min_rank_change": .2,
        "cell_diag_trend_min_deviation_change_mv": 1,
        "cell_diag_resistance_min_delta_current_a": 3,
        "cell_diag_resistance_max_step_seconds": 90, "cell_diag_resistance_min_events": 3,
        "cell_diag_resistance_window_samples": 2, "cell_diag_resistance_max_current_span_a": .5,
        "cell_diag_resistance_max_relative_mad": .3,
        "cell_diag_quality_max_temperature_change_c": 2,
        "cell_diag_sequence_max_gap_seconds": 90, "cell_diag_sequence_min_samples": 5,
        "cell_diag_sequence_min_duration_seconds": 180,
        "cell_diag_sequence_min_charge_ah": .01, "cell_diag_sequence_min_segments": 2,
        "cell_diag_rest_max_current_a": .3,
        "cell_diag_rest_min_duration_seconds": 180,
        "cell_diag_balancing_min_active_samples": 2, "cell_diag_ica_min_samples": 5,
        "cell_diag_ica_max_current_cv": .1, "cell_diag_ica_min_voltage_steps": 5,
    }
    for phase in ("discharge", "low", "charge", "high"):
        value[f"cell_diag_{phase}_observe_deviation_mv"] = 10
        value[f"cell_diag_{phase}_warning_deviation_mv"] = 20
        value[f"cell_diag_{phase}_critical_deviation_mv"] = 40
    value.update(changes)
    return value


def add(store, timestamp, voltages=None, current=-2, balancing=None, temperatures=None, soc=50,
        module_serial=None):
    store.add(CellSample(timestamp, 1, voltages or [3300] * 15, current, soc,
                         temperatures or [25.0] * 15, balancing or [False] * 15,
                         module_serial))


def test_ranking_drift_is_phase_specific_and_uses_daily_aggregates(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    day = 86400
    for index, deviation in enumerate((2, 8, 16)):
        for offset in range(3):
            values = [3300] * 15; values[0] -= deviation
            add(store, index * day + offset, values, current=-2)
            charge = [3300] * 15; charge[0] += 2
            add(store, index * day + 100 + offset, charge, current=2)
    cell = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["ranking_drift"]
    assert cell["status"] == "BEWERTBAR"
    assert cell["phases"]["discharge"]["daily_aggregates"] == 3
    assert cell["phases"]["discharge"]["trend"] == "verschlechternd"
    assert cell["phases"]["charge"]["trend"] == "stabil"


def test_ranking_drift_rejects_insufficient_days(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for timestamp in range(10): add(store, timestamp)
    value = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["ranking_drift"]
    assert value["status"] == "NICHT BEWERTBAR"
    assert "Tagesaggregaten" in value["reason"]


def test_dynamic_resistance_accepts_reproducible_relative_events(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    timestamp = 0
    for _ in range(3):
        add(store, timestamp, [3300] * 15, current=0)
        add(store, timestamp + 30, [3300] * 15, current=0)
        changed = [3290] * 15; changed[0] = 3280
        add(store, timestamp + 60, changed, current=10)
        add(store, timestamp + 90, changed, current=10)
        timestamp += 180
    method = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["dynamic_resistance"]
    assert method["status"] == "BEWERTBAR"
    assert method["relative_resistance_index"] == 2
    assert "mΩ" in method["explanation"]


def test_dynamic_resistance_rejects_small_steps_and_temperature_change(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    add(store, 0, current=0)
    add(store, 60, current=1)
    add(store, 120, current=10, temperatures=[30.0] * 15)
    method = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["dynamic_resistance"]
    assert method["status"] == "NICHT BEWERTBAR"
    assert "Stromsprünge" in method["reason"]


def test_capacity_and_curve_are_relative_and_quality_gated(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for sequence in range(2):
        for index in range(6):
            values = [3200 + 10 * index] * 15; values[0] += index
            add(store, sequence * 1000 + index * 60, values, current=5, soc=40 + index)
    methods = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]
    assert methods["capacity_consistency"]["status"] == "BEWERTBAR"
    assert "keine absolute Zellkapazität" in methods["capacity_consistency"]["explanation"]
    assert set(methods["capacity_consistency"]["phases"]) == {"charge"}
    assert methods["curve_analysis"]["status"] == "BEWERTBAR"
    assert methods["curve_analysis"]["phases"]["charge"]["unit"] == "mV"


def test_capacity_and_curve_reject_fragmented_sequence(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for index in range(4): add(store, index * 500, current=5)
    methods = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]
    assert methods["capacity_consistency"]["status"] == "NICHT BEWERTBAR"
    assert methods["curve_analysis"]["status"] == "NICHT BEWERTBAR"


def test_capacity_and_curve_do_not_mix_charge_and_discharge_sequences(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for sequence, current in enumerate((5, -5)):
        for index in range(6):
            add(store, sequence * 1000 + index * 60,
                [3200 + index] * 15, current=current)
    methods = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]
    assert methods["capacity_consistency"]["status"] == "NICHT BEWERTBAR"
    assert "charge=1" in methods["capacity_consistency"]["reason"]
    assert "discharge=1" in methods["curve_analysis"]["reason"]


def test_capacity_detects_reproducibly_earlier_upper_and_lower_region(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    timestamp = 0
    for current, start, direction in ((5, 3200, 1), (5, 3200, 1),
                                      (-5, 3400, -1), (-5, 3400, -1)):
        for index in range(11):
            values = [start + direction * 10 * index] * 15
            values[0] = start + direction * 13 * index
            add(store, timestamp + index * 60, values, current=current,
                module_serial="SN-A")
        timestamp += 2000
    method = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["capacity_consistency"]
    assert method["status"] == "BEWERTBAR"
    assert method["phases"]["charge"]["crossing_q_fraction"] < 0.9
    assert method["phases"]["charge"]["delta_to_module_median_q_fraction"] < 0
    assert method["phases"]["discharge"]["crossing_q_fraction"] < 0.9
    assert method["phases"]["discharge"]["earlier_than_median_percent"] == 100
    assert method["phases"]["charge"]["unit"] == "normalisierter Q-Anteil"


def test_curve_uses_common_q_axis_interpolation_median_and_no_extrapolation(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    timestamp = 0
    for slope in (13, 13):
        for index in range(11):
            values = [3200 + 10 * index] * 15
            values[0] = 3200 + slope * index
            add(store, timestamp + index * 60, values, current=5,
                module_serial="SN-A")
        timestamp += 2000
    methods = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]
    curve = methods["curve_analysis"]["phases"]["charge"]
    assert curve["q_common_from"] == 0 and curve["q_common_to"] == 1
    assert curve["q_grid_points"] == 21
    assert curve["reference_curve"] == "punktweiser Median der 15 Zellkurven"
    assert curve["rms_deviation_mv"] > 0
    assert "keine Extrapolation" in curve["interpolation"]


def test_capacity_rejects_non_reproducible_crossing_order(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    timestamp = 0
    for slope in (14, 10.5):
        for index in range(11):
            values = [3200 + 10 * index] * 15
            values[0] = round(3200 + slope * index)
            add(store, timestamp + index * 60, values, current=5,
                module_serial="SN-A")
        timestamp += 2000
    capacity = store.analyse(1, options(
        cell_diag_capacity_max_crossing_mad_fraction=.02,
    ))["cells"][0]["diagnostics"]["methods"]["capacity_consistency"]
    assert capacity["status"] == "NICHT BEWERTBAR"
    assert "Reproduzierbarkeit" in capacity["reason"] or "reproduzierbar" in str(capacity["phases"])


def test_rest_drift_uses_relative_voltage_and_does_not_claim_self_discharge(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for index in range(5):
        values = [3300] * 15; values[0] -= index
        add(store, index * 60, values, current=0)
    method = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["rest_drift"]
    assert method["status"] == "BEWERTBAR"
    assert method["relative_drift_mv_per_hour"] < 0
    assert "keine automatische Aussage zur Selbstentladung" in method["explanation"]


def test_rest_drift_rejects_short_or_thermally_unstable_windows(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    add(store, 0, current=0, temperatures=[20.0] * 15)
    add(store, 200, current=0, temperatures=[30.0] * 15)
    method = store.analyse(1, options())["cells"][0]["diagnostics"]["methods"]["rest_drift"]
    assert method["status"] == "NICHT BEWERTBAR"


def test_balancing_requires_real_active_bms_samples(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for index in range(3):
        balancing = [False] * 15; balancing[0] = index < 2
        add(store, index, balancing=balancing)
    methods = store.analyse(1, options())["cells"]
    balancing = methods[0]["diagnostics"]["methods"]["balancing_context"]
    assert balancing["status"] == "BEWERTBAR"
    assert "context_classification" in balancing
    unknown = methods[1]["diagnostics"]["methods"]["balancing_context"]
    assert unknown["status"] == "NICHT BEWERTBAR"
    assert "Herstellerkriterien" in unknown["reason"]


def test_ica_dva_is_readiness_only_even_with_suitable_data(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for index in range(6):
        add(store, index * 60, [3200 + index] * 15, current=5)
    readiness = store.analyse(1, options())["cells"][0]["diagnostics"]["ica_dva_readiness"]
    assert readiness["status"] == "DATEN GEEIGNET"
    assert "keine ICA/DVA-Berechnung" in readiness["explanation"]


def test_condition_trend_risk_and_confidence_remain_separate(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    for day, deviation in enumerate((10, 20, 50)):
        values = [3300] * 15; values[0] -= deviation
        for offset in range(3): add(store, day * 86400 + offset, values)
    cell = store.analyse(1, options())["cells"][0]
    diagnostic = cell["diagnostics"]
    assert diagnostic["current_condition"] == cell["status"]
    assert diagnostic["trend"] in {"stabil", "verbessernd", "verschlechternd", "unklar"}
    assert diagnostic["maintenance_risk"] in {"kein Hinweis", "beobachten", "Wartung empfohlen", "Service dringend"}
    assert diagnostic["confidence"] == cell["confidence"]
    assert "health_score" not in diagnostic
    assert "rul" not in diagnostic
    assert "failure_date" not in diagnostic


def test_maintenance_context_is_association_only(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    add(store, 0, module_serial="SN-A"); add(store, 100, module_serial="SN-A")
    event = {"maintenance_event_id": "MEV-test", "occurred_at": datetime.fromtimestamp(50, timezone.utc).isoformat(),
             "category": "maintenance", "title": "Manuelles Balancing", "module_number": 1,
             "cell_number": 1, "archived_at": None,
             "resolved_module_serial": "SN-A", "identity_status": "explicit"}
    context = store.analyse(1, options(), [event])["cells"][0]["diagnostics"]["maintenance_context"]
    assert context["event_count"] == 1
    assert context["events"][0]["association_only"] is True
    assert context["events"][0]["before"]["samples"] == 1
    assert context["events"][0]["after"]["samples"] == 1
    assert context["events"][0]["elapsed_to_first_after_seconds"] == 50
    assert "keine Kausalitätsaussage" in context["explanation"]


def test_module_swap_separates_physical_cell_history_and_events(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    add(store, 0, module_serial="SN-X")
    add(store, 100, module_serial="SN-X")
    add(store, 200, module_serial="SN-Y")
    add(store, 300, module_serial="SN-Y")
    events = [
        {"maintenance_event_id": "MEV-X", "occurred_at": datetime.fromtimestamp(50, timezone.utc).isoformat(),
         "category": "maintenance", "title": "Alt", "module_number": 1, "cell_number": 1,
         "archived_at": None, "resolved_module_serial": "SN-X", "identity_status": "position_history"},
        {"maintenance_event_id": "MEV-Y", "occurred_at": datetime.fromtimestamp(250, timezone.utc).isoformat(),
         "category": "maintenance", "title": "Neu", "module_number": 1, "cell_number": 1,
         "archived_at": None, "resolved_module_serial": "SN-Y", "identity_status": "position_history"},
    ]
    result = store.analyse(1, options(), events)
    assert result["sample_count"] == 2
    context = result["cells"][0]["diagnostics"]["maintenance_context"]
    assert [event["maintenance_event_id"] for event in context["events"]] == ["MEV-Y"]
    assert context["physical_module_serial"] == "SN-Y"


def test_unknown_physical_identity_makes_maintenance_correlation_not_assessable(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")
    add(store, 0); add(store, 100)
    context = store.analyse(1, options(), [
        {"maintenance_event_id": "MEV-UNKNOWN", "occurred_at": datetime.fromtimestamp(50, timezone.utc).isoformat(),
         "category": "maintenance", "title": "Unklar", "module_number": 1,
         "cell_number": 1, "archived_at": None, "identity_status": "unknown"}
    ])["cells"][0]["diagnostics"]["maintenance_context"]
    assert context["status"] == "NICHT BEWERTBAR"
    assert "Physische Modulidentität" in context["reason"]


def test_old_sample_file_without_new_aggregates_remains_readable(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text('{"samples":{"1":[{"timestamp":1,"module":1,"voltages_mv":['
                    + ','.join(['3300'] * 15)
                    + '],"current_a":-2,"soc_percent":50,"temperatures_c":['
                    + ','.join(['25'] * 15) + '],"balancing":['
                    + ','.join(['false'] * 15) + '] }]}}')
    store = CellDiagnosticStore(path)
    assert store.analyse(1, options())["sample_count"] == 1


def test_maintenance_risk_requires_converging_evidence_and_never_invents_urgency():
    class Controlled(EvidenceDiagnostics):
        def _method(self, trend):
            return {"cells": [{"status": "BEWERTBAR", "quality": "MEDIUM",
                               "trend": trend, "valid_data": 3} for _ in range(15)]}
        def _ranking(self, *_): return self._method("verschlechternd")
        def _resistance(self, *_): return self._method(self.second)
        def _capacity_and_curves(self, *_): return self._method("stabil"), self._method("stabil")
        def _rest(self, *_): return self._method("stabil")
        def _balancing(self, *_): return self._method("stabil")
        def _ica_readiness(self, *_): return {"status": "NICHT BEWERTBAR"}
        def _maintenance(self, *_): return [{"event_count": 0} for _ in range(15)]

    base = [{"status": "AUFFÄLLIG", "confidence": "HIGH"} for _ in range(15)]
    samples = [{"timestamp": 1, "module": 1, "voltages_mv": [3300] * 15}]
    analyzer = Controlled(lambda *_: ["discharge"])
    analyzer.second = "stabil"
    one = analyzer.analyse(samples, options(), base)["cells"][0]
    assert one["maintenance_risk"] == "beobachten"
    analyzer.second = "verschlechternd"
    two = analyzer.analyse(samples, options(), base)["cells"][0]
    assert two["maintenance_risk"] == "Wartung empfohlen"
    assert two["maintenance_risk"] != "Service dringend"
    critical = [{"status": "KRITISCH", "confidence": "HIGH"} for _ in range(15)]
    hard = analyzer.analyse(samples, options(), critical)["cells"][0]
    assert hard["maintenance_risk"] == "Wartung empfohlen"
    assert "harte Current-Condition-Regel" in hard["maintenance_risk_reason"]


def test_capacity_and_curve_count_as_one_risk_family_and_quality_blocks_escalation():
    class Controlled(EvidenceDiagnostics):
        def _cells(self, trend="stabil", quality="MEDIUM"):
            return {"cells": [{"status": "BEWERTBAR", "quality": quality,
                               "trend": trend, "valid_data": 3,
                               "observation_period": {"from": "2026-01-01T00:00:00+00:00",
                                                      "to": "2026-01-10T00:00:00+00:00",
                                                      "seconds": 9 * 86400}}
                              for _ in range(15)]}
        def _ranking(self, *_): return self._cells(self.ranking)
        def _resistance(self, *_): return self._cells(self.resistance, self.resistance_quality)
        def _capacity_and_curves(self, *_): return self._cells(self.capacity), self._cells(self.curve)
        def _rest(self, *_): return self._cells("stabil")
        def _balancing(self, *_): return self._cells("stabil")
        def _ica_readiness(self, *_): return {"status": "NICHT BEWERTBAR"}
        def _maintenance(self, *_):
            return [{"status": "NICHT BEWERTBAR", "quality": "LOW", "trend": "unklar",
                     "valid_data": 0} for _ in range(15)]

    analyzer = Controlled(lambda *_: ["discharge"])
    analyzer.ranking = analyzer.resistance = "stabil"
    analyzer.capacity = analyzer.curve = "verschlechternd"
    analyzer.resistance_quality = "MEDIUM"
    base = [{"status": "NORMAL", "confidence": "HIGH"} for _ in range(15)]
    samples = [{"timestamp": 1, "module": 1, "voltages_mv": [3300] * 15}]
    one_family = analyzer.analyse(samples, options(), base)["cells"][0]
    assert one_family["maintenance_risk"] == "beobachten"
    assert one_family["contributing_evidence"] == ["capacity_curve"]

    analyzer.ranking = "verschlechternd"
    two_families = analyzer.analyse(samples, options(), base)["cells"][0]
    assert two_families["maintenance_risk"] == "Wartung empfohlen"
    assert set(two_families["contributing_evidence"]) == {"voltage_ranking", "capacity_curve"}

    analyzer.capacity = analyzer.curve = "stabil"
    analyzer.ranking = analyzer.resistance = "verschlechternd"
    analyzer.resistance_quality = "LOW"
    low_quality = analyzer.analyse(samples, options(), base)["cells"][0]
    assert low_quality["maintenance_risk"] == "beobachten"
    assert low_quality["contributing_evidence"] == ["voltage_ranking"]


def test_trend_risk_confidence_low_medium_high_has_no_percentage_level():
    analyzer = EvidenceDiagnostics(lambda *_: ["discharge"])
    period = {"from": "2026-01-01T00:00:00+00:00", "to": "2026-01-20T00:00:00+00:00",
              "seconds": 19 * 86400}
    ranking = {"phases": {"discharge": {"coverage_percent": 80}}}
    family = lambda quality: {"status": "BEWERTBAR", "quality": quality,
                              "data_basis": 3, "observation_period": period}
    low, low_basis = analyzer._trend_risk_confidence({}, ranking, period, options())
    medium, medium_basis = analyzer._trend_risk_confidence(
        {"a": family("MEDIUM"), "b": family("MEDIUM")}, ranking, period, options())
    high, high_basis = analyzer._trend_risk_confidence(
        {"a": family("HIGH"), "b": family("HIGH"), "c": family("MEDIUM"),
         "d": family("MEDIUM")}, ranking, period, options())
    assert (low, medium, high) == ("LOW", "MEDIUM", "HIGH")
    assert all(value in {"LOW", "MEDIUM", "HIGH"} for value in (low, medium, high))
    assert medium_basis["independent_families"] == 2
    assert high_basis["high_quality_families"] == 2
    assert isinstance(low_basis["data_coverage_percent"], (int, float))
