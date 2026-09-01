import pytest
from unittest.mock import patch

import position_history
from position_history import (current_presence, stable_observed_changes,
                              missing_expected_positions,
                              project_live_topology, update_observed_stack,
                              update_rs485_observations)


@pytest.fixture(autouse=True)
def reset_presence(monkeypatch):
    monkeypatch.setattr(position_history, "_OBSERVED_STACK", {})
    monkeypatch.setattr(position_history, "_OBSERVATION_CANDIDATES", {})
    monkeypatch.setattr(position_history, "_PRESENCE_SOURCES", {})
    monkeypatch.setattr(position_history, "_MISSING_CANDIDATES", {})
    monkeypatch.setattr(position_history, "_COMMUNICATION_HEALTHY", None)
    monkeypatch.setattr(position_history, "_EXPECTED_MODULE_COUNT", 6)


@pytest.mark.parametrize(
    ("expected", "observed", "missing"),
    [(6, range(1, 6), [6]), (5, range(1, 6), []),
     (5, range(1, 5), [5]), (4, range(1, 5), [])],
)
def test_missing_alarms_use_only_configured_topology(expected, observed, missing):
    assert missing_expected_positions(expected, observed) == missing


def test_historical_position_six_does_not_expand_expected_topology():
    historical_positions = {"1": "A", "2": "B", "3": "C", "4": "D",
                            "5": "E", "6": "F"}
    assert historical_positions["6"] == "F"
    assert missing_expected_positions(5, range(1, 6)) == []


def test_evaluate_emits_no_module_six_alarm_when_five_are_configured():
    with patch("pathlib.Path.mkdir"):
        from main import Module, evaluate
    modules = [Module(number, 48.0, 0.0, 25.0, 24.0, 26.0, 3.30, 3.31,
                      "Idle", "Normal", "Normal", "Normal", 50.0)
               for number in range(1, 6)]
    config = {
        "module_count": 5, "missing_module_is_critical": True,
        "critical_cell_delta_mv": 80, "warning_cell_delta_mv": 30,
        "critical_soc_deviation_pct": 30, "warning_soc_deviation_pct": 10,
    }
    _status, alarms = evaluate(modules, config)
    assert not [item for item in alarms
                if item["code"] == "module_missing" and item["module"] == 6]


def test_current_console_and_rs485_sources_are_time_aware():
    update_observed_stack({1: {"barcode": "SN-1"}}, present_positions={1},
                          expected_module_count=2, observed_at=100)
    assert current_presence(now=100, expected_module_count=2)[1] == {
        "position": 1, "expected": True, "status": "present",
        "observed_serial": "SN-1", "last_observed_at": 100.0,
        "sources": ["console"],
    }
    update_rs485_observations({7: {"position": 1, "serial_string": "SN-1",
                                        "timestamp": 150}}, observed_at=150)
    presence = current_presence(now=200, expected_module_count=2)[1]
    assert presence["status"] == "present"
    assert presence["sources"] == ["rs485_0x93"]


def test_one_stale_source_and_one_current_source_remains_present():
    update_observed_stack({1: {"barcode": "SN-1"}}, present_positions={1},
                          observed_at=0)
    update_rs485_observations({1: {"position": 1, "serial_string": "SN-1",
                                        "timestamp": 100}}, observed_at=100)
    assert current_presence(now=150)[1]["status"] == "present"


def test_one_rs485_frame_is_not_counted_as_three_history_confirmations():
    observation = {1: {"position": 1, "serial_string": "SN-1", "timestamp": 100}}
    for now in (100, 110, 120):
        update_rs485_observations(observation, observed_at=now)
    assert current_presence(now=120)[1]["status"] == "present"
    assert stable_observed_changes({"1": None, "2": None, "3": None,
                                    "4": None, "5": None, "6": None}) == {}

    for timestamp in (130, 140):
        update_rs485_observations(
            {1: {"position": 1, "serial_string": "SN-1", "timestamp": timestamp}},
            observed_at=timestamp)
    assert stable_observed_changes({"1": None, "2": None, "3": None,
                                    "4": None, "5": None, "6": None}) == {1: "SN-1"}


def test_stale_then_confirmed_absent_only_with_healthy_rest_stack():
    infos = {1: {"barcode": "SN-1"}, 2: {"barcode": "SN-2"}}
    update_observed_stack(infos, present_positions={1, 2}, expected_module_count=2,
                          observed_at=0)
    assert current_presence(now=91, expected_module_count=2)[2]["status"] == "stale"
    for timestamp in (100, 115, 131):
        update_observed_stack(infos, present_positions={1}, expected_module_count=2,
                              observed_at=timestamp)
    presence = current_presence(now=131, expected_module_count=2)
    assert presence[2]["status"] == "absent"
    assert presence[2]["observed_serial"] is None
    assert stable_observed_changes({"1": "SN-1", "2": "SN-2",
                                    "3": None, "4": None, "5": None,
                                    "6": None})[2] is None


def test_global_communication_outage_is_unknown_and_never_removal():
    for timestamp in (0, 15, 31, 60):
        update_observed_stack({}, present_positions=set(), communication_healthy=False,
                              expected_module_count=2, observed_at=timestamp)
    assert current_presence(now=100, expected_module_count=2)[2]["status"] == "unknown"
    assert 2 not in stable_observed_changes({"1": "SN-1", "2": "SN-2",
                                             "3": None, "4": None, "5": None,
                                             "6": None})


def test_position_above_configured_topology_is_not_expected():
    presence = current_presence(now=100, expected_module_count=5)
    assert presence[6]["status"] == "not_expected"
    assert presence[6]["expected"] is False


def test_real_five_position_live_projection_keeps_inventory_identity():
    infos = {position: {"barcode": f"SN-{position}"} for position in range(1, 7)}
    update_observed_stack(infos, present_positions=set(range(1, 6)),
                          expected_module_count=5, observed_at=0)
    for timestamp in (100, 115, 131):
        update_observed_stack(infos, present_positions=set(range(1, 5)),
                              expected_module_count=5, observed_at=timestamp)
    documented = {str(position): f"SN-{position}" for position in range(1, 7)}
    topology = project_live_topology(
        documented, now=131, expected_module_count=5)
    assert [topology[position]["presence_status"] for position in range(1, 7)] == [
        "present", "present", "present", "present", "absent", "not_expected"]
    assert sum(1 for value in topology.values()
               if value["expected"] and value["presence_status"] == "present") == 4
    assert topology[5]["physical_serial"] == "SN-5"
    assert topology[6]["physical_serial"] == "SN-6"
    assert topology[6]["expected"] is False
