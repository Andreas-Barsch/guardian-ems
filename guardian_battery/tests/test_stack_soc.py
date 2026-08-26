from datetime import datetime, timezone
from types import SimpleNamespace
import json
import uuid

from history_series import CellHistorySeries
from stack_soc import current_stack_soc, project_stack_soc, relative_cycle_endpoints


def module(number, soc):
    return SimpleNamespace(module=number, soc_percent=soc)


def snapshot(timestamp, positions, suffix):
    complete = {str(index): positions.get(index) for index in range(1, 7)}
    return SimpleNamespace(effective_at=timestamp, created_at=timestamp,
                           position_history_id=f"PHS-{suffix}", positions=complete)


def raw(timestamp, position, soc, serial=None, current=-2.0):
    value = {"timestamp": timestamp, "module": position, "soc_percent": soc,
             "current_a": current, "voltages_mv": [3300.0] * 15}
    if serial:
        value["module_serial"] = serial
    return value


OPTIONS = {"cell_diag_charge_current_a": .8, "cell_diag_discharge_current_a": .8,
           "cell_diag_low_soc_percent": 30, "cell_diag_high_soc_percent": 80}


def test_current_median_for_six_five_and_outlier_is_signed():
    six = current_stack_soc([module(n, value) for n, value in enumerate(
        [40, 41, 42, 43, 44, 99], 1)])
    assert six["median"] == 42.5
    assert six["deviations"][6] == 56.5
    five = current_stack_soc([module(n, value) for n, value in enumerate(
        [40, 41, 42, 43, 99], 1)])
    assert five["median"] == 42


def test_current_median_excludes_missing_and_non_finite_soc():
    result = current_stack_soc([module(1, 30), module(2, None),
                                module(3, float("nan")), module(4, 50)])
    assert result == {"median": 40, "deviations": {1: -10, 4: 10},
                      "module_count": 2}


def test_historical_projection_uses_then_current_topology_and_follows_serial():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    moved = start + 3600
    snapshots = [
        snapshot("2026-07-01T00:00:00+00:00", {1: "SN-A", 2: "SN-B"}, "one"),
        snapshot("2026-07-01T01:00:00+00:00", {1: "SN-B", 2: "SN-A"}, "two"),
    ]
    records = [raw(start + 1, 1, 30), raw(start + 2, 2, 50),
               raw(moved + 1, 1, 55), raw(moved + 2, 2, 35)]
    result = project_stack_soc(records, snapshots)
    assert [(item["module_serial"], item["soc_deviation_pp"]) for item in result] == [
        ("SN-A", -10), ("SN-B", 10), ("SN-B", 10), ("SN-A", -10)]


def test_removed_module_is_not_in_historical_median_and_today_is_not_projected_back():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    snapshots = [snapshot("2026-07-01T00:00:00+00:00", {1: "SN-A", 2: "SN-B"}, "a"),
                 snapshot("2026-07-01T01:00:00+00:00", {1: "SN-A"}, "b")]
    result = project_stack_soc([raw(start + 3601, 1, 40), raw(start + 3602, 2, 90)], snapshots)
    assert len(result) == 1
    assert result[0]["stack_soc_median"] == 40
    assert result[0]["active_module_count"] == 1


def test_reinserted_module_returns_to_peer_median_with_its_physical_identity():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    snapshots = [
        snapshot("2026-07-01T00:00:00+00:00", {1: "SN-A", 2: "SN-B"}, "a"),
        snapshot("2026-07-01T01:00:00+00:00", {1: "SN-A"}, "b"),
        snapshot("2026-07-01T02:00:00+00:00", {1: "SN-A", 2: "SN-B"}, "c"),
    ]
    result = project_stack_soc(
        [raw(start + 7201, 1, 40), raw(start + 7202, 2, 60)], snapshots)
    assert [(item["module_serial"], item["stack_soc_median"]) for item in result] == [
        ("SN-A", 50), ("SN-B", 50)]


def test_module_replacement_does_not_accept_old_serial_after_identity_change():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    snapshots = [
        snapshot("2026-07-01T00:00:00+00:00", {1: "SN-OLD", 2: "SN-B"}, "old"),
        snapshot("2026-07-01T01:00:00+00:00", {1: "SN-NEW", 2: "SN-B"}, "new"),
    ]
    records = [raw(start + 3601, 1, 10, "SN-OLD"),
               raw(start + 3602, 1, 40, "SN-NEW"),
               raw(start + 3603, 2, 60, "SN-B")]
    result = project_stack_soc(records, snapshots)
    assert [(item["module_serial"], item["soc_percent"]) for item in result] == [
        ("SN-NEW", 40), ("SN-B", 60)]


def test_samples_before_first_identity_and_invalid_soc_are_excluded():
    identified = datetime(2026, 7, 1, 1, tzinfo=timezone.utc).timestamp()
    snapshots = [snapshot("2026-07-01T01:00:00+00:00", {1: "SN-A"}, "known")]
    records = [raw(identified - 1, 1, 10), raw(identified, 1, float("nan")),
               raw(identified + 1, 1, 40)]
    result = project_stack_soc(records, snapshots)
    assert [(item["module_serial"], item["soc_percent"]) for item in result] == [
        ("SN-A", 40)]


def test_topology_boundary_uses_old_identity_before_and_new_at_effective_time():
    boundary = datetime(2026, 7, 1, 1, tzinfo=timezone.utc).timestamp()
    snapshots = [
        snapshot("2026-07-01T00:00:00+00:00", {1: "SN-OLD"}, "old"),
        snapshot("2026-07-01T01:00:00+00:00", {1: "SN-NEW"}, "new"),
    ]
    result = project_stack_soc([raw(boundary - .001, 1, 30, "SN-OLD"),
                                raw(boundary, 1, 40, "SN-NEW")], snapshots)
    assert [item["module_serial"] for item in result] == ["SN-OLD", "SN-NEW"]


def test_stale_values_are_not_combined_into_one_stack_median():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    snapshots = [snapshot("2026-07-01T00:00:00+00:00",
                          {1: "SN-A", 2: "SN-B"}, "stack")]
    result = project_stack_soc([raw(start, 1, 20), raw(start + 31, 2, 80)], snapshots)
    assert [(item["stack_soc_median"], item["active_module_count"]) for item in result] == [
        (20, 1), (80, 1)]


def test_relative_endpoints_are_observations_and_never_claim_bms_blocking():
    base = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    discharge = [raw(base + n * 60, 1, 50 - n, "SN-A", -2) for n in range(4)]
    charge = [raw(base + 600 + n * 60, 1, 50 + n, "SN-A", 2) for n in range(4)]
    rest = raw(base + 900, 1, 54, "SN-A", 0)
    result = relative_cycle_endpoints(discharge + charge + [rest], OPTIONS)
    assert [item["kind"] for item in result] == ["relative_low_point", "relative_high_point"]
    assert all(item["evidence_level"] == "observation" for item in result)
    assert all(item["causality"] == "not_determined" for item in result)
    assert all(item["bms_limit_confirmed"] is False for item in result)


def test_query_window_end_is_not_misreported_as_an_observed_cycle_endpoint():
    base = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
    ongoing = [raw(base + n * 60, 1, 50 - n, "SN-A", -2) for n in range(4)]
    assert relative_cycle_endpoints(ongoing, OPTIONS) == []


def test_history_series_exposes_identity_safe_stack_median_and_deviation(tmp_path):
    history = tmp_path / "cell_history"
    history.mkdir()
    positions = tmp_path / "position_history.jsonl"
    position_record = {
        "schema_version": 1,
        "position_history_id": "PHS-" + str(uuid.uuid4()),
        "effective_at": "2026-07-01T00:00:00+00:00",
        "created_at": "2026-07-01T00:00:00+00:00",
        "maintenance_event_id": "MEV-" + str(uuid.uuid4()),
        "positions": {"1": "SN-A", "2": "SN-B", "3": None, "4": None,
                      "5": None, "6": None},
    }
    positions.write_text(json.dumps(position_record) + "\n")
    timestamp = datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp()
    records = [raw(timestamp + 1, 1, 30), raw(timestamp + 2, 2, 50)]
    for value in records:
        value.update(schema_version=1, temperatures_c=[25.0] * 15,
                     balancing=[False] * 15)
    (history / "2026-07-01.jsonl").write_text(
        "".join(json.dumps(value) + "\n" for value in records))
    series = CellHistorySeries(history, position_history_path=positions)
    median = series.query_bundle(metric="stack_soc_median",
                                 timestamp_from="2026-07-01T00:00:00+00:00",
                                 timestamp_to="2026-07-02T00:00:00+00:00",
                                 module_number=1)
    deviation = series.query_bundle(metric="soc_deviation",
                                    timestamp_from="2026-07-01T00:00:00+00:00",
                                    timestamp_to="2026-07-02T00:00:00+00:00",
                                    module_number=1)
    assert median["points"][0]["value"] == 40
    assert deviation["points"][0]["value"] == -10
    assert median["points"][0]["module_serial"] == "SN-A"
    assert median["points"][0]["active_module_count"] == 2
