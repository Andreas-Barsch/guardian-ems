import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from cell_history import cell_history_timing


def test_cell_history_timing_records_pwr_age_compatibly():
    assert cell_history_timing(105.5, 100) == {
        "cell_sample_at": 105.5, "pwr_sample_at": 100.0,
        "pwr_age_seconds": 5.5, "pwr_age_quality": "observed"}


def test_cell_history_timing_never_emits_unqualified_negative_age():
    assert cell_history_timing(99, 100) == {
        "cell_sample_at": 99.0, "pwr_sample_at": 100.0,
        "pwr_age_seconds": None, "pwr_age_quality": "invalid_future"}
    assert cell_history_timing(99, None)["pwr_age_quality"] == "unavailable"
