from pathlib import Path

from cell_diagnostics import (
    CellDiagnosticStore,
    CellSample,
    DIAGNOSTIC_PARAMETER_META,
)


def opts(**overrides):
    values = {
        "cell_diag_low_soc_percent": 30,
        "cell_diag_high_soc_percent": 80,
        "cell_diag_charge_current_a": 0.8,
        "cell_diag_discharge_current_a": 0.8,
        "cell_diag_min_phase_samples": 5,
        "cell_diag_confidence_medium_samples": 10,
        "cell_diag_confidence_high_samples": 20,

        # Globale Fallback-Werte für bestehende Konfigurationen.
        "cell_diag_observe_deviation_mv": 10,
        "cell_diag_warning_deviation_mv": 20,
        "cell_diag_critical_deviation_mv": 40,

        # 0.4.7: getrennte Grenzwerte je statuswirksamer Phase.
        "cell_diag_discharge_observe_deviation_mv": 10,
        "cell_diag_discharge_warning_deviation_mv": 20,
        "cell_diag_discharge_critical_deviation_mv": 40,

        "cell_diag_low_observe_deviation_mv": 10,
        "cell_diag_low_warning_deviation_mv": 20,
        "cell_diag_low_critical_deviation_mv": 40,

        "cell_diag_charge_observe_deviation_mv": 10,
        "cell_diag_charge_warning_deviation_mv": 20,
        "cell_diag_charge_critical_deviation_mv": 40,

        "cell_diag_high_observe_deviation_mv": 10,
        "cell_diag_high_warning_deviation_mv": 20,
        "cell_diag_high_critical_deviation_mv": 40,
    }
    values.update(overrides)
    return values


def sample(
    timestamp,
    voltages,
    *,
    module=1,
    current_a=0.0,
    soc_percent=50.0,
):
    return CellSample(
        timestamp=timestamp,
        module=module,
        voltages_mv=voltages,
        current_a=current_a,
        soc_percent=soc_percent,
        temperatures_c=[25.0] * 15,
        balancing=[False] * 15,
    )


def voltages_with_cell2(delta_mv):
    values = [3300] * 15
    values[1] = 3300 + delta_mv
    return values


def add_repeated(
    store,
    count,
    voltages,
    *,
    current_a=0.0,
    soc_percent=50.0,
    start=1,
):
    for n in range(count):
        store.add(
            sample(
                start + n,
                list(voltages),
                current_a=current_a,
                soc_percent=soc_percent,
            )
        )


def test_empty_store_is_learning_phase(tmp_path):
    store = CellDiagnosticStore(tmp_path / "empty.json")
    result = store.analyse(1, opts())

    assert result["status"] == "LERNPHASE"
    assert result["confidence"] == "LOW"
    assert result["sample_count"] == 0
    assert result["cells"] == []


def test_phase_assignment():
    o = opts()

    base = {
        "voltages_mv": [3300] * 15,
        "soc_percent": 50,
    }

    assert "charge" in CellDiagnosticStore.phases(
        {**base, "current_a": 1.0}, o
    )
    assert "discharge" in CellDiagnosticStore.phases(
        {**base, "current_a": -1.0}, o
    )
    assert "rest" in CellDiagnosticStore.phases(
        {**base, "current_a": 0.0}, o
    )

    low = CellDiagnosticStore.phases(
        {**base, "current_a": 0.0, "soc_percent": 25}, o
    )
    high = CellDiagnosticStore.phases(
        {**base, "current_a": 0.0, "soc_percent": 90}, o
    )

    assert "low" in low
    assert "high" in high


def test_status_thresholds_for_discharge(tmp_path):
    cases = [
        (5, "NORMAL"),
        (10, "BEOBACHTEN"),
        (20, "AUFFÄLLIG"),
        (40, "KRITISCH"),
    ]

    for delta_mv, expected in cases:
        store = CellDiagnosticStore(
            tmp_path / f"discharge_{delta_mv}.json"
        )
        add_repeated(
            store,
            5,
            voltages_with_cell2(delta_mv),
            current_a=-2.0,
        )

        result = store.analyse(1, opts())

        assert result["cells"][1]["status"] == expected


def test_minimum_samples_keep_status_in_learning_phase(tmp_path):
    store = CellDiagnosticStore(tmp_path / "learning.json")

    add_repeated(
        store,
        4,
        voltages_with_cell2(50),
        current_a=-2.0,
    )

    result = store.analyse(1, opts(cell_diag_min_phase_samples=5))

    assert result["cells"][1]["status"] == "LERNPHASE"


def test_confidence_low_medium_high(tmp_path):
    cases = [
        (5, "LOW"),
        (10, "MEDIUM"),
        (20, "HIGH"),
    ]

    for count, expected in cases:
        store = CellDiagnosticStore(
            tmp_path / f"confidence_{count}.json"
        )
        add_repeated(
            store,
            count,
            voltages_with_cell2(5),
            current_a=-2.0,
        )

        result = store.analyse(1, opts())

        assert result["cells"][1]["confidence"] == expected


def test_phase_specific_thresholds_change_result(tmp_path):
    # Derselbe Betrag von 25 mV wird absichtlich unterschiedlich bewertet:
    # Entladen: unter Observe 30 mV => NORMAL
    # Laden:    über Warning 20 mV => AUFFÄLLIG
    o = opts(
        cell_diag_discharge_observe_deviation_mv=30,
        cell_diag_discharge_warning_deviation_mv=40,
        cell_diag_discharge_critical_deviation_mv=50,
        cell_diag_charge_observe_deviation_mv=10,
        cell_diag_charge_warning_deviation_mv=20,
        cell_diag_charge_critical_deviation_mv=40,
    )

    discharge = CellDiagnosticStore(tmp_path / "discharge.json")
    add_repeated(
        discharge,
        5,
        voltages_with_cell2(25),
        current_a=-2.0,
    )
    discharge_result = discharge.analyse(1, o)

    charge = CellDiagnosticStore(tmp_path / "charge.json")
    add_repeated(
        charge,
        5,
        voltages_with_cell2(25),
        current_a=2.0,
    )
    charge_result = charge.analyse(1, o)

    assert discharge_result["cells"][1]["status"] == "NORMAL"
    assert charge_result["cells"][1]["status"] == "AUFFÄLLIG"


def test_worst_phase_determines_cell_status(tmp_path):
    o = opts(
        cell_diag_discharge_observe_deviation_mv=10,
        cell_diag_discharge_warning_deviation_mv=20,
        cell_diag_discharge_critical_deviation_mv=40,
        cell_diag_charge_observe_deviation_mv=10,
        cell_diag_charge_warning_deviation_mv=20,
        cell_diag_charge_critical_deviation_mv=30,
    )

    store = CellDiagnosticStore(tmp_path / "worst_phase.json")

    add_repeated(
        store,
        5,
        voltages_with_cell2(15),
        current_a=-2.0,
        start=1,
    )
    add_repeated(
        store,
        5,
        voltages_with_cell2(35),
        current_a=2.0,
        start=100,
    )

    result = store.analyse(1, o)
    cell2 = result["cells"][1]

    assert cell2["status"] == "KRITISCH"
    assert cell2["evidence_phase"] == "charge"
    assert cell2["evidence_deviation_mv"] == 35


def test_phase_status_uses_absolute_median_deviation(tmp_path):
    for delta_mv in (-43, 43):
        store = CellDiagnosticStore(tmp_path / f"absolute_{delta_mv}.json")
        add_repeated(
            store,
            5,
            voltages_with_cell2(delta_mv),
            current_a=-2.0,
        )

        phase = store.analyse(1, opts())["cells"][1]["phases"]["discharge"]

        assert phase["median_deviation_mv"] == delta_mv
        assert phase["status"] == "KRITISCH"
        assert phase["thresholds_mv"] == {
            "observe": 10,
            "warning": 20,
            "critical": 40,
        }


def test_equal_worst_status_selects_larger_absolute_phase_deviation(tmp_path):
    store = CellDiagnosticStore(tmp_path / "phase_tie.json")
    add_repeated(store, 5, voltages_with_cell2(25), current_a=-2.0, start=1)
    add_repeated(store, 5, voltages_with_cell2(-35), current_a=2.0, start=100)

    cell2 = store.analyse(1, opts())["cells"][1]

    assert cell2["phases"]["discharge"]["status"] == "AUFFÄLLIG"
    assert cell2["phases"]["charge"]["status"] == "AUFFÄLLIG"
    assert cell2["evidence_phase"] == "charge"
    assert cell2["evidence_deviation_mv"] == 35


def test_rest_is_not_status_effective(tmp_path):
    store = CellDiagnosticStore(tmp_path / "rest.json")

    # SOC 50 und Mittelspannung > 3.22 V / < 3.38 V:
    # dadurch ausschließlich Rest, keine zusätzliche Low-/High-Phase.
    add_repeated(
        store,
        20,
        voltages_with_cell2(80),
        current_a=0.0,
        soc_percent=50,
    )

    result = store.analyse(1, opts())

    assert result["cells"][1]["phases"]["rest"]["samples"] == 20
    assert (
        result["cells"][1]["phases"]["rest"]["status"]
        == "NICHT STATUSWIRKSAM"
    )
    assert result["cells"][1]["status"] == "LERNPHASE"


def test_worst_cell_determines_module_status(tmp_path):
    store = CellDiagnosticStore(tmp_path / "worst_cell.json")

    values = [3300] * 15
    values[1] = 3315
    values[4] = 3345

    add_repeated(
        store,
        20,
        values,
        current_a=-2.0,
    )

    result = store.analyse(1, opts())

    assert result["status"] == "KRITISCH"
    assert result["evidence_worst_cell"] == 5


def test_phase_sample_counts(tmp_path):
    store = CellDiagnosticStore(tmp_path / "samples.json")

    add_repeated(
        store,
        8,
        [3327] * 15,
        current_a=-2.0,
        soc_percent=25,
    )

    result = store.analyse(1, opts())

    assert result["cells"][4]["phases"]["low"]["samples"] == 8
    assert result["cells"][4]["phases"]["discharge"]["samples"] == 8


def test_current_module_median(tmp_path):
    store = CellDiagnosticStore(tmp_path / "median.json")

    values = [
        3300, 3301, 3302, 3303, 3304,
        3305, 3306, 3307, 3308, 3309,
        3310, 3311, 3312, 3313, 3400,
    ]

    store.add(
        sample(
            1,
            values,
            current_a=-2.0,
            soc_percent=50,
        )
    )

    result = store.analyse(
        1,
        opts(cell_diag_min_phase_samples=1),
    )

    assert result["current_median_mv"] == 3307
    assert result["cells"][0]["current_deviation_mv"] == -7


def test_explainability_metadata():
    assert DIAGNOSTIC_PARAMETER_META["deviation"]["unit"] == "mV"
    assert (
        "Median"
        in DIAGNOSTIC_PARAMETER_META["deviation"]["definition"]
    )
    assert DIAGNOSTIC_PARAMETER_META["rank"]["unit"] == "Rang von 15"
    assert DIAGNOSTIC_PARAMETER_META["confidence"]["unit"] == "dimensionslos"


def test_no_historical_median_api(tmp_path):
    store = CellDiagnosticStore(tmp_path / "no_history.json")

    assert not hasattr(store, "median_history")
