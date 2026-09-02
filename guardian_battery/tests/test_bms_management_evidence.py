import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from bms_management_evidence import (BmsManagementEvidenceAnalyzer,
                                     BmsManagementEvidenceStore, CAUSALITY,
                                     EvidenceParameters)


FIXTURE = Path(__file__).parent / "fixtures" / "bms_management_reference_v1.json"
BASE = datetime(2026, 8, 31, tzinfo=timezone.utc).timestamp()


def frame(timestamp, command, *, adr=6, serial=None, decoded=None, info_raw="",
          reference=None, valid=True, position=5, history_id="PHS-synthetic"):
    return {
        "record_type": "frame", "direction": "response", "paired_command": command,
        "timestamp": timestamp, "adr": adr, "checksum_valid": valid,
        "frame_complete": valid, "request_matched": valid, "physical_serial": serial,
        "position": position, "position_history_id": history_id, "decoded": decoded,
        "info_raw": info_raw, "source_frame_reference": reference or f"ref-{timestamp}",
        "source": "rs485_passive",
    }


def management(timestamp, dcl, *, serial="SYNTHETIC-MODULE-0001", adr=6,
               enabled=True, charge_enabled=True, ccl=25.0, position=5):
    return frame(timestamp, 0x92, adr=adr, serial=serial, position=position, decoded={
        "discharge_current_limit_a": dcl, "discharge_enable": enabled,
        "charge_current_limit_a": ccl, "charge_enable": charge_enabled,
        "charge_voltage_limit_v": 53.25,
        "discharge_voltage_limit_v": 45.0,
    })


def cell(timestamp, *, serial="SYNTHETIC-MODULE-0001", module=5, c8=3260,
         spread=50, current=-1.0, soc=25):
    voltages = [c8 + spread] * 15
    voltages[7] = c8
    return {"timestamp": timestamp, "module": module, "module_serial": serial,
            "position_history_id": "PHS-historical", "soc_percent": soc,
            "current_a": current, "voltages_mv": voltages,
            "temperatures_c": [30.0] * 15, "balancing": [False] * 15}


def transition_frames(timestamp, *, adr=6, old=0x11, new=0x00):
    return [frame(timestamp - 1, 0x44, adr=adr, info_raw=f"{old:02X}06"),
            frame(timestamp, 0x44, adr=adr, info_raw=f"{new:02X}06")]


def analyze(rs485, cells=(), **kwargs):
    return BmsManagementEvidenceAnalyzer().analyze(rs485, cells, **kwargs)


def test_dcl_zero_detection_duplicate_suppression_recovery_and_stable_id():
    rs = [management(BASE, -25), management(BASE + 10, 0), management(BASE + 20, 0),
          management(BASE + 40, -25)]
    first = analyze(rs)
    second = analyze(rs)
    assert len(first["events"]) == 1
    event = first["events"][0]
    assert event["event_id"] == second["events"][0]["event_id"]
    assert event["dcl_before"] == -25 and event["dcl_after"] == 0
    assert event["zero_poll_count"] == 2
    assert event["observed_duration_seconds"] == 30
    assert event["recovery_dcl"] == -25
    assert event["recovery_transition_0x44"] is None
    assert event["causality"] == CAUSALITY


def test_unpaired_event_despite_enable_and_historical_position_resolver():
    calls = []
    def resolver(serial, timestamp):
        calls.append((serial, timestamp))
        return 4, "PHS-at-event"
    event = analyze([management(BASE, -25), management(BASE + 10, 0)],
                    position_resolver=resolver)["events"][0]
    assert event["observed_end"] is None and event["observed_duration_seconds"] is None
    assert event["dcl_zero_despite_enable"] is True
    assert event["physical_serial"] == "SYNTHETIC-MODULE-0001"
    assert event["position_at_time"] == 4
    assert event["position_history_id"] == "PHS-at-event"
    assert calls[-1][1].startswith("2026-08-31T00:00:10")


def test_identity_is_restored_from_valid_historical_0x93_raw():
    serial = "ABCDEFGHIJKLMNOP"
    identity = frame(BASE, 0x93, serial=None, info_raw=(b"\x06" + serial.encode()).hex(),
                     position=None, history_id=None)
    identity["decoder_supported"] = False
    identity["decoded"] = None
    result = analyze([identity, management(BASE + 1, -25, serial=None),
                      management(BASE + 2, 0, serial=None)])
    assert result["events"][0]["physical_serial"] == serial


@pytest.mark.parametrize("age,quality", [(10, "high"), (15, "high"),
                                           (30, "medium"), (60, "medium"),
                                           (90, "low"), (120, "low"),
                                           (121, "unavailable")])
def test_cell_context_age_quality_and_no_future_leakage(age, quality):
    event_time = BASE + 200
    cells = [cell(event_time - age, c8=3100), cell(event_time + 1, c8=2800)]
    event = analyze([management(BASE, -25), management(event_time, 0)], cells)["events"][0]
    assert event["cell_context_quality"] == quality
    if quality == "unavailable":
        assert event["cell_context"] is None
    else:
        assert event["cell_context"]["min_cell_voltage_mv"] == 3100
        assert event["cell_sample_age_seconds"] == age


def test_cell_metrics_dynamics_lowest_and_median_deviation():
    cells = [cell(BASE + 20, c8=3260, spread=40, current=-1),
             cell(BASE + 80, c8=2900, spread=400, current=-22)]
    event = analyze([management(BASE, -25), management(BASE + 90, 0)], cells)["events"][0]
    context = event["cell_context"]
    assert context["min_cell_number"] == context["worst_negative_cell"] == 8
    assert context["spread_mv"] == 400
    assert context["module_median_mv"] == 3300
    assert context["per_cell_deviation_mv"][7] == -400
    assert event["cell_dynamics"]["60"]["delta_worst_cell_mv"] == -360
    assert event["cell_dynamics"]["60"]["delta_module_current_a"] == -21


def test_0x44_nearest_preceding_window_wrong_adr_and_later_excluded():
    event_time = BASE + 100
    rs = [management(BASE, -25),
          *transition_frames(event_time - 5, adr=5),
          *transition_frames(event_time - 4),
          management(event_time, 0),
          *transition_frames(event_time + 2)]
    transition = analyze(rs)["events"][0]["transition_0x44"]
    assert transition["offset"] == 0
    assert (transition["old_hex"], transition["new_hex"]) == ("11", "00")
    assert transition["delta_t_seconds"] == 4


def test_0x44_outside_window_is_not_correlated():
    rs = [management(BASE, -25), *transition_frames(BASE + 10), management(BASE + 30, 0)]
    assert analyze(rs)["events"][0]["transition_0x44"] is None


def test_current_before_after_windows_continued_discharge_and_near_zero():
    rs = [management(BASE, -25), management(BASE + 100, 0), management(BASE + 130, -25),
          management(BASE + 200, 0)]
    cells = [cell(BASE + 95, current=-1), cell(BASE + 105, current=-24.281),
             cell(BASE + 190, current=-1), cell(BASE + 205, current=0)]
    events = analyze(rs, cells)["events"]
    assert events[0]["current_response"]["category"] == "continued_discharge_observed"
    assert events[0]["current_response"]["after_60s"]["median_current_a"] == -24.281
    assert events[1]["current_response"]["category"] == "near_zero_observed"


def test_current_response_insufficient_data():
    event = analyze([management(BASE, -25), management(BASE + 10, 0)])["events"][0]
    assert event["current_response"]["category"] == "insufficient_data"


def test_reconstructed_stack_current_and_synchronization_rejection():
    rs = [management(BASE, -25), management(BASE + 100, 0)]
    close = [cell(BASE + 98, serial="A", module=1, current=-2),
             cell(BASE + 99, serial="B", module=2, current=-3)]
    stack = analyze(rs, close)["events"][0]["reconstructed_stack_current"]
    assert stack == {"value_a": -5.0, "source": "reconstructed_from_module_currents",
                     "module_count": 2, "max_sample_span_seconds": 1.0}
    far = [cell(BASE + 90, serial="A", module=1), cell(BASE + 99, serial="B", module=2)]
    assert analyze(rs, far)["events"][0]["reconstructed_stack_current"] is None


def reference_records():
    spec = json.loads(FIXTURE.read_text())
    serial, adr = spec["physical_serial"], spec["adr"]
    rs, cells = [], []
    for index, offset in enumerate(spec["event_offsets_seconds"]):
        timestamp = BASE + offset
        rs.extend([management(timestamp - 40, -25, serial=serial, adr=adr),
                   *transition_frames(timestamp - 4, adr=adr),
                   management(timestamp, 0, serial=serial, adr=adr)])
        recovery = timestamp + spec["zero_durations_seconds"][index]
        poll = timestamp + 60
        while poll < recovery:
            rs.append(management(poll, 0, serial=serial, adr=adr))
            poll += 60
        if index < spec["recovery_0x44_count"]:
            rs.extend(transition_frames(recovery - 4, adr=adr, old=0, new=0x11))
        rs.append(management(recovery, -25, serial=serial, adr=adr))
        cells.extend([cell(timestamp - 10, serial=serial,
                           c8=spec["cell_8_mv"][index], spread=spec["spread_mv"][index],
                           current=spec["current_before_a"][index]),
                      cell(timestamp + 10, serial=serial,
                           current=spec["current_after_a"][index])])
    return spec, rs, cells


def test_anonymized_reference_fixture_seven_events_and_7_of_7_pattern():
    spec, rs, cells = reference_records()
    result = analyze(rs, cells)
    events, aggregate = result["events"], result["daily_aggregates"][0]
    assert len(events) == 7
    assert sum(event["observed_end"] is not None for event in events) == 7
    assert sum(event["dcl_zero_despite_enable"] for event in events) == 7
    assert sum(event["cell_context"]["min_cell_number"] == 8 for event in events) == 7
    assert sum(event["cell_context"]["spread_mv"] > 300 for event in events) == 2
    assert sum(event["current_response"]["category"] == "continued_discharge_observed"
               for event in events) == 1
    assert sum(event["recovery_transition_0x44"] is not None for event in events) == 6
    assert aggregate["dominant_lowest_cell"] == 8
    assert aggregate["dominant_lowest_ratio"] == 1
    assert aggregate["dominant_0x44_transition"] == "offset:0:11->00"
    assert aggregate["dominant_0x44_ratio"] == 1
    assert result["causality"] == spec["causality"] == CAUSALITY


def test_daily_aggregate_duration_duty_coverage_and_cell_statistics():
    _, rs, cells = reference_records()
    aggregate = analyze(rs, cells)["daily_aggregates"][0]
    expected_duration = sum([365, 374, 287, 336, 302, 303, 52])
    assert aggregate["dcl_zero_count"] == 7
    assert aggregate["dcl_zero_total_observed_duration_seconds"] == expected_duration
    assert aggregate["dcl_zero_duty_cycle"] == pytest.approx(
        expected_duration / aggregate["observed_management_duration_seconds"])
    assert aggregate["management_coverage_ratio_of_day"] < 1
    assert aggregate["dcl_zero_despite_enable_ratio"] == 1
    assert aggregate["cell_context_coverage_ratio"] == 1
    assert aggregate["max_spread_before_dcl_zero_mv"] == 398
    assert aggregate["minimum_cell_before_dcl_zero_mv"] == 2884
    assert aggregate["lowest_count_per_cell"][7] == 7
    assert aggregate["worst_count_per_cell"][7] == 7


def test_daily_aggregate_no_divide_by_zero_and_missing_context():
    result = analyze([management(BASE, -25)])
    aggregate = result["daily_aggregates"][0]
    assert aggregate["dcl_zero_count"] == 0
    assert aggregate["dcl_zero_duty_cycle"] is None
    assert aggregate["cell_context_coverage_ratio"] is None


def test_negative_control_windows_are_observations_not_alarm_status():
    analyzer = BmsManagementEvidenceAnalyzer()
    management_rows = [{"timestamp": BASE + n, "physical_serial": "S", "dcl": -25}
                       for n in (10, 20)]
    controls = analyzer.negative_controls(management_rows,
                                         [cell(BASE + 5, serial="S"),
                                          cell(BASE + 15, serial="S")])
    assert len(controls) == 2
    assert controls[0]["matching"] == "same_serial_nonzero_dcl_observation"
    assert controls[0]["causality"] == CAUSALITY


def aggregate(day, value, *, serial="S", coverage=1):
    return {"day": day, "physical_serial": serial,
            "observed_management_duration_seconds": coverage,
            "dcl_zero_count": value, "dcl_zero_duty_cycle": value,
            "minimum_cell_before_dcl_zero_mv": value,
            "median_spread_before_dcl_zero_mv": value,
            "median_min_cell_before_dcl_zero_mv": value,
            "median_module_current_before_a": value,
            "dominant_0x44_ratio": value, "dominant_lowest_ratio": value}


@pytest.mark.parametrize("values,expected", [([1, 2, 3], "increasing"),
                                               ([3, 2, 1], "decreasing"),
                                               ([2, 2, 2], "stable")])
def test_deterministic_trends(values, expected):
    analyzer = BmsManagementEvidenceAnalyzer()
    rows = [aggregate(f"2026-08-{index + 1:02d}", value)
            for index, value in enumerate(values)]
    result = analyzer.trends(rows, days=7)
    assert result["event_rate"] == expected
    assert result["duty_cycle"] == expected


def test_trend_one_day_and_missing_minimum_are_insufficient():
    result = BmsManagementEvidenceAnalyzer().trends([aggregate("2026-08-01", 1)], days=30)
    assert result["days_available"] == 1
    assert result["event_rate"] == "insufficient_data"


def test_trends_ignore_no_evidence_days_but_keep_observed_zero_event_days():
    rows = [aggregate("2026-08-01", 0, coverage=60),
            aggregate("2026-08-02", 99, coverage=0),
            aggregate("2026-08-04", 1, coverage=60)]
    result = BmsManagementEvidenceAnalyzer().trends(rows, days=7)
    assert result["days_available"] == 3
    assert result["days_with_management_coverage"] == 2
    assert result["event_rate"] == "insufficient_data"


def test_trends_require_identity_when_multiple_serials_are_supplied():
    rows = [aggregate("2026-08-01", 1, serial="A"),
            aggregate("2026-08-01", 1, serial="B")]
    with pytest.raises(ValueError, match="physical_serial"):
        BmsManagementEvidenceAnalyzer().trends(rows, days=7)


def test_invalid_rs485_frames_and_wrong_serial_are_excluded():
    invalid = management(BASE + 10, 0)
    invalid["checksum_valid"] = False
    result = analyze([management(BASE, -25), invalid],
                     [cell(BASE + 5, serial="OTHER")])
    assert result["events"] == []


def test_ccl_reduction_recovery_and_charging_cell_context():
    rs = [management(BASE, -25, ccl=25), management(BASE + 100, -25, ccl=10),
          management(BASE + 200, -25, ccl=25)]
    cells = [cell(BASE + 95, current=8.5, soc=91, c8=3310, spread=20),
             cell(BASE + 105, current=4.0, soc=91, c8=3312, spread=18)]
    events = analyze(rs, cells)["events"]
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "ccl_reduction"
    assert (event["ccl_before"], event["ccl_after"]) == (25, 10)
    assert event["restriction_increase_a"] == 15
    assert event["ccl_reduced_despite_enable"] is True
    assert event["cell_context"]["soc_percent"] == 91
    assert event["cell_context"]["module_current_a"] == 8.5
    assert event["observed_duration_seconds"] == 100
    assert event["recovery_ccl"] == 25


def test_ccl_zero_despite_charge_enable_and_no_fixed_25_amp_normal():
    events = analyze([management(BASE, -25, ccl=17),
                      management(BASE + 10, -25, ccl=9),
                      management(BASE + 20, -25, ccl=17),
                      management(BASE + 30, -25, ccl=0)])["events"]
    assert [item["event_type"] for item in events] == ["ccl_reduction", "ccl_zero"]
    assert events[0]["limit_before_a"] == 17 and events[0]["limit_after_a"] == 9
    assert events[1]["ccl_zero_despite_enable"] is True


def test_dcl_nonzero_reduction_uses_signed_limit_magnitude_and_recovers():
    events = analyze([management(BASE, -25), management(BASE + 10, -10),
                      management(BASE + 20, -25)])["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "dcl_reduction"
    assert events[0]["restriction_increase_a"] == 15
    assert events[0]["recovery_dcl"] == -25


def test_peer_relative_limits_one_outlier_and_all_identical():
    rs = []
    for index, ccl in enumerate((25, 25, 10), 1):
        rs.append(management(BASE + index, -25, serial=f"S{index}", adr=index, ccl=ccl,
                             position=index))
    result = analyze(rs)
    outlier = next(item for item in result["relative_limits"]
                   if item["physical_serial"] == "S3")
    assert outlier["ccl_peer_median_a"] == 25
    assert outlier["ccl_peer_deviation_a"] == -15
    assert outlier["relative_ccl_ratio"] == .4
    assert outlier["relative_ccl_percent"] == 40
    assert outlier["relative_dcl_ratio"] == 1
    identical = analyze([management(BASE + index, -17, serial=f"I{index}", adr=index,
                                    ccl=13) for index in range(1, 4)])
    assert all(item["relative_ccl_ratio"] == 1 for item in identical["relative_limits"])
    assert all(item["ccl_peer_deviation_a"] == 0 for item in identical["relative_limits"])


def test_peer_relative_limits_multiple_outliers_use_each_modules_other_peers():
    values = (25, 20, 10, 5)
    rs = [management(BASE + index, -25, serial=f"M{index}", adr=index, ccl=value)
          for index, value in enumerate(values, 1)]
    rows = analyze(rs)["relative_limits"]
    lowest = next(item for item in rows if item["physical_serial"] == "M4")
    assert lowest["ccl_peer_median_a"] == 20
    assert lowest["relative_ccl_percent"] == 25


def test_generalized_daily_limit_aggregates_and_peer_values():
    rs = [management(BASE, -25, serial="A", adr=1, ccl=20),
          management(BASE + 1, -25, serial="B", adr=2, ccl=20),
          management(BASE + 10, -10, serial="A", adr=1, ccl=5),
          management(BASE + 11, -25, serial="B", adr=2, ccl=20),
          management(BASE + 20, -25, serial="A", adr=1, ccl=20),
          management(BASE + 21, -25, serial="B", adr=2, ccl=20)]
    aggregate = next(item for item in analyze(rs)["daily_aggregates"]
                     if item["physical_serial"] == "A")
    assert aggregate["ccl_reduction_event_count"] == 1
    assert aggregate["ccl_zero_event_count"] == 0
    assert aggregate["ccl_min_a"] == 5
    assert aggregate["ccl_median_a"] == 20
    assert aggregate["ccl_reduced_duration_seconds"] == 10
    assert aggregate["dcl_reduction_event_count"] == 1
    assert aggregate["dcl_zero_event_count"] == 0
    assert aggregate["dcl_max_restriction_a"] == 15
    assert aggregate["peer_relative_ccl_min_ratio"] == .25


def test_multistep_ccl_nested_events_have_partial_and_full_recovery_without_double_duration():
    rs = [management(BASE, -25, ccl=25), management(BASE + 10, -25, ccl=10),
          management(BASE + 20, -25, ccl=5), management(BASE + 30, -25, ccl=10),
          management(BASE + 40, -25, ccl=25)]
    result = analyze(rs)
    events = result["events"]
    assert [(item["ccl_before"], item["ccl_after"], item["recovery_ccl"])
            for item in events] == [(25, 10, 25), (10, 5, 10)]
    aggregate = result["daily_aggregates"][0]
    assert aggregate["ccl_reduction_event_count"] == 2
    assert aggregate["ccl_reduced_duration_seconds"] == 30


def test_multistep_signed_dcl_nested_restriction_and_recovery():
    rs = [management(BASE, -25), management(BASE + 10, -10),
          management(BASE + 20, 0), management(BASE + 30, -10),
          management(BASE + 40, -25)]
    result = analyze(rs)
    events = result["events"]
    assert [(item["dcl_before"], item["dcl_after"], item["recovery_dcl"])
            for item in events] == [(-25, -10, -25), (-10, 0, -10)]
    aggregate = result["daily_aggregates"][0]
    assert aggregate["dcl_reduction_event_count"] == 2
    assert aggregate["dcl_zero_event_count"] == aggregate["dcl_zero_count"] == 1


def test_open_event_at_eof_uses_only_observed_restricted_interval():
    result = analyze([management(BASE, -25, ccl=25),
                      management(BASE + 10, -25, ccl=10),
                      management(BASE + 40, -25, ccl=10)])
    event = result["events"][0]
    assert event["observed_end"] is None
    assert event["observed_through"].endswith("00:00:40+00:00")
    assert event["observed_restricted_duration_seconds"] == 30
    assert result["daily_aggregates"][0]["ccl_reduced_duration_seconds"] == 30


def test_management_gap_is_excluded_from_coverage():
    six_hours = 6 * 3600
    rs = [management(BASE, -25), management(BASE + 60, -25),
          management(BASE + six_hours, -25), management(BASE + six_hours + 60, -25)]
    aggregate = analyze(rs)["daily_aggregates"][0]
    assert aggregate["observed_management_duration_seconds"] == 120
    assert aggregate["management_gap_limit_seconds"] == 120


def test_restriction_duration_does_not_bridge_unobserved_management_gap():
    six_hours = 6 * 3600
    result = analyze([management(BASE, -25), management(BASE + 10, 0),
                      management(BASE + six_hours, -25)])
    aggregate = result["daily_aggregates"][0]
    assert result["events"][0]["observed_duration_seconds"] == six_hours - 10
    assert aggregate["dcl_zero_duration_seconds"] == 0
    assert aggregate["dcl_zero_duty_cycle"] == 0


def test_peer_one_peer_no_peer_and_stale_peer_not_mixed():
    pair = analyze([management(BASE, -25, serial="A", adr=1, ccl=10),
                    management(BASE + 1, -25, serial="B", adr=2, ccl=20)])
    a = next(item for item in pair["relative_limits"] if item["physical_serial"] == "A")
    assert a["peer_count"] == 1 and a["ccl_peer_median_a"] == 20
    single = analyze([management(BASE, -25, serial="A", adr=1, ccl=10)])
    assert single["relative_limits"][0]["peer_context_quality"] == "unavailable"
    assert single["relative_limits"][0]["relative_ccl_ratio"] is None
    stale = analyze([management(BASE, -25, serial="A", adr=1, ccl=10),
                     management(BASE + 11, -25, serial="B", adr=2, ccl=20)])
    assert all(item["peer_count"] == 0 for item in stale["relative_limits"])


def test_tied_lowest_cells_are_reported_without_unique_claim():
    sample = cell(BASE + 5, c8=3000, spread=300)
    sample["voltages_mv"][6] = 3000
    result = analyze([management(BASE, -25), management(BASE + 10, 0)], [sample])
    context = result["events"][0]["cell_context"]
    assert context["min_cell_numbers"] == [7, 8]
    assert context["min_cell_is_unique"] is False
    aggregate = result["daily_aggregates"][0]
    assert aggregate["lowest_cell_counts"] == {"7": 1, "8": 1}
    assert aggregate["unique_lowest_count_per_cell"][6:8] == [0, 0]


def test_cell_voltage_contract_is_millivolts_and_median_is_signed():
    sample = cell(BASE + 5, c8=2912, spread=398)
    context = analyze([management(BASE, -25), management(BASE + 10, 0)],
                      [sample])["events"][0]["cell_context"]
    assert context["min_cell_voltage_mv"] == 2912
    assert context["max_cell_voltage_mv"] == 3310
    assert context["spread_mv"] == 398
    assert context["module_median_mv"] == 3310
    assert context["worst_negative_deviation_mv"] == -398
    assert context["module_voltage_v"] == pytest.approx((14 * 3310 + 2912) / 1000)


def test_equal_timestamp_cell_selection_is_deterministic_and_never_future():
    low = cell(BASE + 5, c8=3000, spread=300)
    high = cell(BASE + 5, c8=3200, spread=100)
    rs = [management(BASE, -25), management(BASE + 10, 0)]
    first = analyze(rs, [low, high])["events"][0]["cell_context"]
    second = analyze(rs, [high, low])["events"][0]["cell_context"]
    assert first == second


def test_local_day_crossing_midnight_splits_duration_without_duplicate_event_count():
    zone = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 9, 1, 23, 59, tzinfo=zone).timestamp()
    end = datetime(2026, 9, 2, 0, 1, tzinfo=zone).timestamp()
    result = analyze([management(start - 10, -25), management(start, 0),
                      management(end, -25)])
    days = {item["day"]: item for item in result["daily_aggregates"]}
    assert days["2026-09-01"]["dcl_zero_count"] == 1
    assert days["2026-09-01"]["dcl_zero_duration_seconds"] == 60
    assert days["2026-09-02"]["dcl_zero_count"] == 0
    assert days["2026-09-02"]["dcl_zero_duration_seconds"] == 60


def test_dst_local_day_uses_actual_25_hour_denominator():
    zone = ZoneInfo("Europe/Berlin")
    first = datetime(2026, 10, 25, 0, 0, tzinfo=zone).timestamp()
    result = analyze([management(first, -25), management(first + 60, -25)])
    aggregate = result["daily_aggregates"][0]
    assert aggregate["day"] == "2026-10-25"
    assert aggregate["day_timezone"] == "Europe/Berlin"
    assert aggregate["management_coverage_ratio_of_day"] == pytest.approx(60 / (25 * 3600))


def test_separate_persistence_is_append_only_for_events_and_regenerates_aggregates(tmp_path):
    event_path = tmp_path / "bms_management_events.jsonl"
    aggregate_path = tmp_path / "bms_management_daily.json"
    store = BmsManagementEvidenceStore(event_path, aggregate_path)
    _, rs, cells = reference_records()
    result = analyze(rs, cells)
    assert store.append_events(result["events"]) == 7
    original = event_path.read_bytes()
    assert store.append_events(result["events"]) == 0
    assert event_path.read_bytes() == original
    store.save_daily_aggregates(result["daily_aggregates"])
    stored = json.loads(aggregate_path.read_text())
    assert stored["schema_version"] == 1
    assert stored["aggregates"][0]["dcl_zero_count"] == 7


def test_event_store_deduplicates_same_event_inside_one_daily_job_batch(tmp_path):
    store = BmsManagementEvidenceStore(tmp_path / "events.jsonl", tmp_path / "daily.json")
    event = {"event_id": "BME-one", "causality": CAUSALITY}
    assert store.append_events([event, dict(event)]) == 1
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 1


def test_event_store_refuses_to_append_to_crash_truncated_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"event_id":"incomplete"')
    store = BmsManagementEvidenceStore(path, tmp_path / "daily.json")
    with pytest.raises(ValueError, match="crash-truncated"):
        store.append_events([{"event_id": "BME-next"}])
    assert path.read_bytes() == b'{"event_id":"incomplete"'
