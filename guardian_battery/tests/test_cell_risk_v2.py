import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from cell_risk_v2 import (CELL_RISK_ALGORITHM_VERSION, analyze_cell_risk,
                          cell_group, ols_slope, percentile, risk_class,
                          score_components)


def sample(index=0, *, serial="MODULE-A", current=-2.0, weak_cell=8,
           weak_mv=3200, tie=False):
    values = [3300] * 15
    values[weak_cell - 1] = weak_mv
    if tie:
        values[weak_cell] = weak_mv
    return {"timestamp": datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp() + index * 60,
            "module": 5, "module_serial": serial, "current_a": current,
            "voltages_mv": values, "temperatures_c": [25] * 15,
            "balancing": [False] * 15}


def test_group_boundaries_percentile_ols_and_classes():
    assert [cell_group(x) for x in (1, 5, 6, 10, 11, 15)] == [
        "G1", "G1", "G2", "G2", "G3", "G3"]
    assert percentile([0, 10, 20], .1) == 2
    assert ols_slope([3, 2, 1]) == -1
    expected = [(0, "UNAUFFÄLLIG"), (15, "UNAUFFÄLLIG"),
                (15.0001, "HINWEIS"), (30, "HINWEIS"),
                (30.0001, "BEOBACHTEN"), (50, "BEOBACHTEN"),
                (50.0001, "DEUTLICH_AUFFÄLLIG"), (75, "DEUTLICH_AUFFÄLLIG"),
                (75.0001, "HOHES_RISIKO"), (100, "HOHES_RISIKO")]
    assert [(score, risk_class(score)) for score, _ in expected] == expected


@pytest.mark.parametrize("median_module,median_group,lowest,load,trend,expected", [
    (-55, -54, .96973, 120.5, -12.392857, 99.818),
    (-47, -37, .99577, 17, -3.321429, 89.541),
    (-32, -34, .02841, 30, -3.214286, 60.948),
    (-13, -12, .91999, 2, -.928571, 40.544),
    (-1, -1, .77199, 2, -.196429, 3.469),
])
def test_reference_formula_controls(median_module, median_group, lowest, load,
                                    trend, expected):
    assert score_components(median_module, median_group, lowest, load, trend)[
        "risk_score_v2"] == pytest.approx(expected, abs=.01)


@pytest.mark.parametrize("current,accepted", [(-.79, False), (-.8, False), (-.81, True)])
def test_exact_sample_selection(current, accepted):
    result = analyze_cell_risk([sample(current=current)], diagnostic_date="2026-08-20")
    assert bool(result["cells"]) is accepted
    invalid_length = {**sample(current=-1), "voltages_mv": [3300] * 14}
    missing_identity = {**sample(current=-1), "module_serial": None}
    assert not analyze_cell_risk([invalid_length, missing_identity],
                                 diagnostic_date="2026-08-20")["cells"]


def test_lowest_tie_credits_first_cell_only_and_masked_cell_path_b():
    tied = analyze_cell_risk([sample(tie=True)], diagnostic_date="2026-08-20")
    cells = {row["cell_number"]: row for row in tied["cells"]}
    assert cells[8]["lowest_share"] == 1 and cells[9]["lowest_share"] == 0
    records = [sample(i, current=-2 if i < 40 else -10, weak_mv=3100) for i in range(80)]
    for record in records:
        record["voltages_mv"][13] = 3250 if abs(record["current_a"]) < 4 else 3150
    result = analyze_cell_risk(records, diagnostic_date="2026-08-20")
    z14 = next(row for row in result["cells"] if row["cell_number"] == 14)
    assert z14["lowest_share"] == 0 and z14["path_b"] > 0


def test_quality_contract_determinism_position_and_balancing_context():
    start = datetime(2026, 8, 20, tzinfo=timezone.utc)
    records = []
    for index in range(300):
        record = sample(index, current=-2 if index < 150 else -10)
        record["timestamp"] = (start + timedelta(days=index % 7, minutes=index)).timestamp()
        records.append(record)
    maintenance = [{"module_serial": "MODULE-A", "cell_number": 8,
                    "category": "maintenance", "title": "Balancing durchgeführt",
                    "occurred_at": "2026-08-19T12:00:00+00:00"}]
    resolver = lambda serial, timestamp: (5, "PHS-REFERENCE")
    first = analyze_cell_risk(records, diagnostic_date="2026-08-26",
                              maintenance_records=maintenance,
                              position_resolver=resolver)
    second = analyze_cell_risk(list(reversed(records)), diagnostic_date="2026-08-26",
                               maintenance_records=maintenance,
                               position_resolver=resolver)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    z8 = next(row for row in first["cells"] if row["cell_number"] == 8)
    assert z8["sample_quality"] == z8["load_quality"] == "SUFFICIENT"
    assert z8["trend_quality"] == "STRONG" and z8["overall_confidence"] == "HIGH"
    assert z8["current_position"] == 5 and z8["position_history_id"] == "PHS-REFERENCE"
    assert z8["balancing"]["status"] == "BALANCING_PERFORMED_EVALUATION_PENDING"
    assert z8["cell_risk_algorithm_version"] == CELL_RISK_ALGORITHM_VERSION


def test_confidence_does_not_change_score_or_ranking_and_top10_has_no_dummies():
    records = [sample(i, serial="A", weak_mv=3200) for i in range(10)]
    records += [sample(i, serial="B", weak_mv=3290) for i in range(300)]
    result = analyze_cell_risk(records, diagnostic_date="2026-08-20")
    assert len(result["top10"]) == 10
    assert result["cells"][0]["physical_serial"] == "A"
    assert result["cells"][0]["overall_confidence"] == "LOW"
    assert all(result["cells"][i]["risk_score_v2"] >= result["cells"][i + 1]["risk_score_v2"]
               for i in range(len(result["cells"]) - 1))


def test_quality_exact_boundaries_299_300_29_30_and_2_3_7_days():
    def row(count, low, days):
        records = []
        for index in range(count):
            value = sample(index, current=-2 if index < low else -10)
            value["timestamp"] = (datetime(2026, 8, 20, tzinfo=timezone.utc)
                                  + timedelta(days=index % days, minutes=index)).timestamp()
            records.append(value)
        return analyze_cell_risk(records, diagnostic_date="2026-09-02")["cells"][0]
    assert row(299, 150, 7)["sample_quality"] == "INSUFFICIENT"
    assert row(300, 150, 7)["sample_quality"] == "SUFFICIENT"
    assert row(59, 29, 3)["load_quality"] == "INSUFFICIENT"
    assert row(60, 30, 3)["load_quality"] == "SUFFICIENT"
    assert row(60, 30, 2)["trend_quality"] == "INSUFFICIENT"
    assert row(60, 30, 3)["trend_quality"] == "SUFFICIENT"
    assert row(60, 30, 7)["trend_quality"] == "STRONG"
