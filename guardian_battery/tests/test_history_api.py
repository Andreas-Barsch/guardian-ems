import json
from datetime import datetime, timezone

from event_overlay import EventOverlayAdapter
from history_api import HistoryApi
from history_series import CellHistorySeries
from config_history import ConfigHistory
from phase_engine import PhaseEngine
from test_timeline import add, build


def write_sample(directory, timestamp, module=3):
    directory.mkdir()
    record = {"schema_version": 1, "timestamp": timestamp, "module": module,
              "voltages_mv": [3300 + n for n in range(15)], "current_a": -2.5,
              "soc_percent": 64.0, "temperatures_c": [24.0 + n / 10 for n in range(15)],
              "balancing": [False] * 15, "physical_groups": {}}
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    (directory / f"{day}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


def api_env(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    directory = tmp_path / "cell_history"
    return maintenance, directory, HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline))


def test_soc_reference_combines_unchanged_series_and_overlay(tmp_path):
    maintenance, directory, api = api_env(tmp_path)
    timestamp = datetime(2026, 8, 12, 10, 42, tzinfo=timezone.utc).timestamp()
    write_sample(directory, timestamp)
    event = add(maintenance, occurred_at="2026-08-12T10:42:00Z")
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-08-01T00%3A00%3A00Z&to=2026-09-01T00%3A00%3A00Z&module_number=3")
    assert response.status == 200
    assert response.body["series"]["points"] == [
        {"timestamp": "2026-08-12T10:42:00+00:00", "value": 64.0}
    ]
    assert response.body["overlays"][0]["maintenance_event_id"] == event.maintenance_event_id
    assert response.body["semantics"] == {"overlay_timestamp": "occurred_at", "correlation_only": True}
    assert response.body["phase_analysis"]["intervals"] == []


def test_series_without_maintenance_is_unchanged_and_has_no_markers(tmp_path):
    _, directory, api = api_env(tmp_path)
    timestamp = datetime(2026, 8, 12, 10, 42, tzinfo=timezone.utc).timestamp()
    write_sample(directory, timestamp)
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-08-01T00:00:00Z&to=2026-09-01T00:00:00Z&module_number=3")
    assert response.status == 200
    assert response.body["series"]["points"][0]["value"] == 64.0
    assert response.body["overlays"] == []


def test_cell_history_marker_matching_time_and_deep_link(tmp_path):
    maintenance, directory, api = api_env(tmp_path)
    write_sample(directory, datetime(2026, 8, 15, 8, tzinfo=timezone.utc).timestamp())
    exact = add(maintenance, occurred_at="2026-08-15T08:00:00Z", module_number=3, cell_number=7)
    add(maintenance, occurred_at="2026-08-15T08:01:00Z", module_number=3, cell_number=8)
    response = api.handle("GET", "/api/history/series?metric=cell_voltage&from=2026-08-15T00%3A00%3A00Z&to=2026-08-16T00%3A00%3A00Z&module_number=3&cell_number=7")
    assert response.status == 200
    assert response.body["series"]["points"][0]["value"] == 3306.0
    assert [m["maintenance_event_id"] for m in response.body["overlays"]] == [exact.maintenance_event_id]
    assert response.body["overlays"][0]["deep_link"].endswith(exact.maintenance_event_id)


def test_cell_metric_module_level_returns_all_raw_cells_and_module_markers(tmp_path):
    maintenance, directory, api = api_env(tmp_path)
    write_sample(directory, datetime(2026, 8, 15, 8, tzinfo=timezone.utc).timestamp())
    system = add(maintenance, occurred_at="2026-08-15T08:00:00Z",
                 module_number=None, cell_number=None)
    module = add(maintenance, occurred_at="2026-08-15T08:01:00Z",
                 module_number=3, cell_number=None)
    add(maintenance, occurred_at="2026-08-15T08:02:00Z", module_number=4, cell_number=None)
    response = api.handle("GET", "/api/history/series?metric=cell_voltage&from=2026-08-15T00:00:00Z&to=2026-08-16T00:00:00Z&module_number=3")
    assert response.status == 200
    assert len(response.body["series"]["points"]) == 15
    assert [point["cell_number"] for point in response.body["series"]["points"]] == list(range(1, 16))
    assert [point["value"] for point in response.body["series"]["points"]] == [3300.0 + n for n in range(15)]
    assert {item["maintenance_event_id"] for item in response.body["overlays"]} == {
        system.maintenance_event_id, module.maintenance_event_id}


def test_api_rejects_invalid_requests_and_corrupt_series(tmp_path):
    _, directory, api = api_env(tmp_path)
    for query in ("", "?metric=bad&from=x&to=x&module_number=3",
                  "?metric=soc&from=2026-08-02T00:00:00Z&to=2026-08-01T00:00:00Z&module_number=3",
                  "?metric=cell_voltage&from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z&module_number=0"):
        assert api.handle("GET", "/api/history/series" + query).status == 400
    directory.mkdir(); (directory / "2026-08-01.jsonl").write_text("broken\n", encoding="utf-8")
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z&module_number=3")
    assert response.status == 503 and response.body["error"]["code"] == "series_history_error"


def test_phase_projection_modes_are_separate_from_raw_series(tmp_path):
    maintenance, directory, timeline = build(tmp_path)
    timestamp = datetime(2026, 8, 15, 8, tzinfo=timezone.utc).timestamp()
    write_sample(directory, timestamp)
    config = tmp_path / "config.jsonl"
    parameters = {"cell_diag_low_soc_percent": 30, "cell_diag_high_soc_percent": 80,
                  "cell_diag_charge_current_a": .8, "cell_diag_discharge_current_a": .8}
    config.write_text(json.dumps({"schema_version": 1, "timestamp": "2026-08-01T00:00:00+00:00",
                                  "config_id": "one", "parameters": parameters}) + "\n")
    api = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                     PhaseEngine(ConfigHistory(config), lambda: parameters))
    target = "/api/history/series?metric=soc&from=2026-08-15T00:00:00Z&to=2026-08-16T00:00:00Z&module_number=3"
    historical = api.handle("GET", target)
    assert historical.body["series"]["points"][0]["value"] == 64.0
    assert historical.body["phase_analysis"]["mode"] == "historical"
    assert historical.body["phase_analysis"]["intervals"][0]["phase"] == "discharge"
    what_if = api.handle("GET", target + "&analysis_mode=what_if&what_if_low_soc_percent=30"
                         "&what_if_high_soc_percent=80&what_if_charge_current_a=0.8"
                         "&what_if_discharge_current_a=3")
    assert what_if.body["phase_analysis"]["intervals"][0]["phase"] == "rest"
    assert config.read_text().count("\n") == 1
