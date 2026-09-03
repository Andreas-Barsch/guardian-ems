import json
import uuid
from datetime import datetime, timezone

import pytest

from event_overlay import EventOverlayAdapter
from history_api import HistoryApi
from history_series import CellHistorySeries
from hycube_evidence import (HycubeBatteryCapacitySeries, HycubePolicyHistory,
                             policy_observation)
from rs485_evidence import Rs485HistorySeries
from config_history import ConfigHistory
from phase_engine import PhaseEngine
from position_history import PositionHistoryLog, PositionSnapshot
from test_timeline import add, build


def write_sample(directory, timestamp, module=3):
    directory.mkdir(exist_ok=True)
    record = {"schema_version": 1, "timestamp": timestamp, "module": module,
              "voltages_mv": [3300 + n for n in range(15)], "current_a": -2.5,
              "soc_percent": 64.0, "temperatures_c": [24.0 + n / 10 for n in range(15)],
              "balancing": [False] * 15, "physical_groups": {}}
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    (directory / f"{day}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


def append_sample(directory, timestamp, module, soc):
    directory.mkdir(exist_ok=True)
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    record = {"schema_version": 1, "timestamp": timestamp, "module": module,
              "voltages_mv": [3300] * 15, "current_a": 0, "soc_percent": soc,
              "temperatures_c": [25] * 15, "balancing": [False] * 15,
              "physical_groups": {}, "module_serial": f"SERIAL-{module}"}
    with (directory / f"{day}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


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
    assert response.body["semantics"]["overlay_timestamp"] == "occurred_at"
    assert response.body["semantics"]["correlation_only"] is True
    assert response.body["semantics"]["relative_endpoints"] == "observation_only"
    assert response.body["semantics"]["bms_limit_requires_direct_evidence"] is True
    assert response.body["phase_analysis"]["intervals"] == []


def test_series_without_maintenance_is_unchanged_and_has_no_markers(tmp_path):
    _, directory, api = api_env(tmp_path)
    timestamp = datetime(2026, 8, 12, 10, 42, tzinfo=timezone.utc).timestamp()
    write_sample(directory, timestamp)
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-08-01T00:00:00Z&to=2026-09-01T00:00:00Z&module_number=3")
    assert response.status == 200
    assert response.body["series"]["points"][0]["value"] == 64.0
    assert response.body["overlays"] == []


def test_soc_timeline_single_projects_selected_module_and_hycube_received_at(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    cell_directory = tmp_path / "cell_history"
    hycube_directory = tmp_path / "hycube_history"
    timestamp = datetime(2026, 9, 2, 8, tzinfo=timezone.utc).timestamp()
    for module in range(1, 7):
        append_sample(cell_directory, timestamp + module, module, 70 + module)
    hycube_directory.mkdir()
    record = {"schema_version": 1, "record_type": "hycube_system_observation",
              "received_at": "2026-09-02T08:00:20+00:00", "BatteryCapacity": 82,
              "Date2": None, "device_timestamp": None, "timezone_semantics": "unavailable",
              "parse_quality": "complete", "payload_sha256": "abc",
              "configured_interval_seconds": 5.0, "actual_interval_seconds": 5.1,
              "actual_interval_quality": "observed"}
    (hycube_directory / "2026-09-02.jsonl").write_text(json.dumps(record) + "\n")
    api = HistoryApi(CellHistorySeries(cell_directory), EventOverlayAdapter(timeline),
                     hycube_series=HycubeBatteryCapacitySeries(hycube_directory))
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-09-02T08:00:00Z&to=2026-09-02T09:00:00Z&module_number=3")
    assert response.status == 200
    projected = response.body["soc_timeline"]
    assert [item["module_number"] for item in projected["module_series"]] == [3]
    assert projected["hycube_series"]["points"] == [{
        "timestamp": "2026-09-02T08:00:20+00:00", "value": 82.0,
        "source": "hycube", "source_field": "BatteryCapacity",
        "device_timestamp": None, "timezone_semantics": "unavailable",
        "parse_quality": "complete", "payload_sha256": "abc",
        "configured_interval_seconds": 5.0, "actual_interval_seconds": 5.1,
        "actual_interval_quality": "observed"}]
    assert projected["policy_series"] == []
    assert projected["policy_evidence"] == "unavailable"
    assert projected["aggregation_rule"] == "not_verified"

    comparison = api.handle(
        "GET", "/api/history/series?metrics=soc&from=2026-09-02T08:00:00Z"
        "&to=2026-09-02T09:00:00Z&module_number=3")
    assert comparison.status == 200
    assert [item["module_number"] for item in
            comparison.body["soc_timeline"]["module_series"]] == list(range(1, 7))
    assert (comparison.body["soc_timeline"]["hycube_series"]["points"] ==
            projected["hycube_series"]["points"])


def test_soc_timeline_omits_hycube_series_when_history_is_missing(tmp_path):
    maintenance, directory, timeline = build(tmp_path)
    timestamp = datetime(2026, 9, 2, 8, tzinfo=timezone.utc).timestamp()
    append_sample(directory, timestamp, 1, 70)
    api = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                     hycube_series=HycubeBatteryCapacitySeries(tmp_path / "missing"))
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-09-02T08:00:00Z&to=2026-09-02T09:00:00Z&module_number=1")
    assert response.status == 200
    assert response.body["soc_timeline"]["hycube_series"] is None


@pytest.mark.parametrize("selected", [1, 6])
def test_soc_timeline_single_keeps_only_requested_edge_module(tmp_path, selected):
    _, directory, api = api_env(tmp_path)
    timestamp = datetime(2026, 9, 2, 8, tzinfo=timezone.utc).timestamp()
    for module in range(1, 7):
        append_sample(directory, timestamp + module, module, 70 + module)
    response = api.handle(
        "GET", "/api/history/series?metric=soc&from=2026-09-02T08:00:00Z"
        f"&to=2026-09-02T09:00:00Z&module_number={selected}")
    assert response.status == 200
    assert [item["module_number"] for item in
            response.body["soc_timeline"]["module_series"]] == [selected]


def test_soc_timeline_projects_time_valid_policy_segments_without_backdating(tmp_path):
    maintenance, directory, timeline = build(tmp_path)
    append_sample(directory, datetime(2026, 9, 2, 8, tzinfo=timezone.utc).timestamp(), 1, 82)
    policy = HycubePolicyHistory(tmp_path / "policy")
    policy.append(policy_observation(
        b'{"normalMode":82,"bufferMode":3,"emergency":10,"batProtection":5}',
        datetime(2026, 9, 2, 8, 5, tzinfo=timezone.utc).timestamp()))
    policy.append(policy_observation(
        b'{"normalMode":72,"bufferMode":3,"emergency":20,"batProtection":5}',
        datetime(2026, 9, 2, 8, 30, tzinfo=timezone.utc).timestamp()))
    api = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                     hycube_policy_history=policy)
    before = api.handle("GET", "/api/history/series?metric=soc&from=2026-09-02T08:00:00Z&to=2026-09-02T08:04:00Z&module_number=1")
    assert before.body["soc_timeline"]["policy_evidence"] == "unavailable"
    assert before.body["soc_timeline"]["policy_series"] == []
    response = api.handle("GET", "/api/history/series?metric=soc&from=2026-09-02T08:10:00Z&to=2026-09-02T09:00:00Z&module_number=1")
    projected = response.body["soc_timeline"]
    assert projected["policy_evidence"] == "observed"
    assert [(item["boundary_normal_passive"], item["boundary_passive_emergency"],
             item["boundary_emergency_protection"]) for item in projected["policy_series"]] == [
        (18.0, 15.0, 5.0), (28.0, 25.0, 5.0)]
    assert projected["policy_series"][0]["quality"] == "historically_applicable"
    assert all(item["causality"] == "not_determined" for item in projected["policy_series"])


def test_rs485_history_api_resolves_module_via_serial_without_adr_selector(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    cell_directory = tmp_path / "cell_history"
    rs_directory = tmp_path / "rs485_history"
    timestamp = datetime(2026, 8, 12, 10, 42, tzinfo=timezone.utc).timestamp()
    write_sample(cell_directory, timestamp, module=3)
    rs_directory.mkdir()
    serial = "SERIAL-123456789"
    identity = {"record_type": "frame", "timestamp": "2026-08-12T10:41:00+00:00",
                "adr": 2, "direction": "response", "paired_command": 0x93,
                "checksum_valid": True, "frame_complete": True, "request_matched": True,
                "info_raw": "02" + serial.encode().hex().upper(), "decoded": None}
    management_record = {
        "record_type": "frame", "timestamp": "2026-08-12T10:42:00+00:00",
        "adr": 2, "direction": "response", "paired_command": 0x92,
        "decoded": {"discharge_current_limit_a": -25.0}}
    (rs_directory / "2026-08-12.jsonl").write_text(
        json.dumps(identity) + "\n" + json.dumps(management_record) + "\n")
    positions = tmp_path / "positions.jsonl"
    PositionHistoryLog(positions).append(PositionSnapshot(
        schema_version=1, position_history_id="PHS-" + str(uuid.uuid4()),
        effective_at="2026-08-12T00:00:00+00:00",
        created_at="2026-08-12T00:00:00+00:00",
        maintenance_event_id="MEV-" + str(uuid.uuid4()),
        positions={str(number): serial if number == 3 else None for number in range(1, 7)}))
    add(maintenance, occurred_at="2026-08-12T10:42:00Z", module_number=3)
    api = HistoryApi(CellHistorySeries(cell_directory), EventOverlayAdapter(timeline),
                     rs485_series=Rs485HistorySeries(
                         rs_directory, position_history_path=positions))
    base = ("/api/history/series?metric=rs485_dcl&from=2026-08-12T00:00:00Z"
            "&to=2026-08-13T00:00:00Z&module_number=3")
    response = api.handle("GET", base)
    assert response.status == 200
    assert response.body["series"]["points"][0]["value"] == -25.0
    assert response.body["series"]["module_number"] == 3
    assert response.body["series"]["points"][0]["physical_serial"] == serial
    assert len(response.body["overlays"]) == 1
    assert api.handle("GET", base + "&adr=2").status == 400


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


def test_cell_multi_select_uses_one_history_scan_and_returns_selected_series(tmp_path):
    _, directory, api = api_env(tmp_path)
    write_sample(directory, datetime(2026, 8, 15, 8, tzinfo=timezone.utc).timestamp())
    target = ("/api/history/series?metric=cell_voltage&from=2026-08-15T00:00:00Z"
              "&to=2026-08-16T00:00:00Z&module_number=3&cell_numbers=2,6,12")
    first = api.handle("GET", target)
    second = api.handle("GET", target)
    assert first.status == 200
    assert first.body["series"]["cell_numbers"] == [2, 6, 12]
    assert {point["cell_number"] for point in first.body["series"]["points"]} == {2, 6, 12}
    assert first.body["performance"]["raw_records"] == 1
    assert second.body["performance"]["cache_hit"] is True


def test_combined_metrics_use_one_scan_and_independent_cell_selections(tmp_path, monkeypatch):
    _, directory, api = api_env(tmp_path)
    write_sample(directory, datetime(2026, 8, 15, 8, tzinfo=timezone.utc).timestamp())
    opened = []
    original_open = type(directory).open

    def counted_open(path, *args, **kwargs):
        if path.parent == directory and path.suffix == ".jsonl":
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(directory), "open", counted_open)
    target = ("/api/history/series?metrics=soc,current,cell_voltage,cell_temperature"
              "&from=2026-08-15T00:00:00Z&to=2026-08-16T00:00:00Z&module_number=3"
              "&voltage_cell_numbers=2,6,12&temperature_cell_numbers=2,6")
    response = api.handle("GET", target)
    assert response.status == 200
    assert opened == [directory / "2026-08-15.jsonl"]
    assert [item["metric"] for item in response.body["series"]] == [
        "soc", "current", "cell_voltage", "cell_temperature"]
    assert response.body["series"][2]["cell_numbers"] == [2, 6, 12]
    assert response.body["series"][3]["cell_numbers"] == [2, 6]
    assert {point["cell_number"] for point in response.body["series"][2]["points"]} == {2, 6, 12}
    assert {point["cell_number"] for point in response.body["series"][3]["points"]} == {2, 6}
    assert response.body["performance"]["raw_records"] == 1


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


def test_series_downsampling_preserves_extrema_and_cache_invalidates(tmp_path):
    directory = tmp_path / "cell_history"; directory.mkdir()
    day = "2026-08-15"; rows=[]
    start=datetime(2026,8,15,tzinfo=timezone.utc).timestamp()
    for index in range(200):
        value=5000 if index==87 else 1000 if index==113 else 3300+index%7
        rows.append(json.dumps({"schema_version":1,"timestamp":start+index*60,"module":3,
          "voltages_mv":[value]*15,"current_a":0,"soc_percent":50,"temperatures_c":[25]*15,
          "balancing":[False]*15,"physical_groups":{}}))
    path=directory/f"{day}.jsonl"; path.write_text("\n".join(rows)+"\n")
    series=CellHistorySeries(directory)
    args=dict(metric="cell_voltage",timestamp_from="2026-08-15T00:00:00+00:00",
              timestamp_to="2026-08-16T00:00:00+00:00",module_number=3,cell_number=1,max_points=40)
    first=series.query_bundle(**args); second=series.query_bundle(**args)
    assert len(first["points"]) <= 40 and {1000.0,5000.0} <= {p["value"] for p in first["points"]}
    assert not first["cache_hit"] and second["cache_hit"]
    path.write_text(path.read_text()+rows[0]+"\n")
    assert not series.query_bundle(**args)["cache_hit"]


def test_series_points_carry_only_documented_physical_identity(tmp_path):
    directory=tmp_path/"cell_history"; directory.mkdir()
    timestamp=datetime(2026,8,15,8,tzinfo=timezone.utc).timestamp()
    base={"schema_version":1,"timestamp":timestamp,"module":3,"voltages_mv":[3300]*15,
          "current_a":0,"soc_percent":50,"temperatures_c":[25]*15,"balancing":[False]*15,"physical_groups":{}}
    path=directory/"2026-08-15.jsonl"
    path.write_text(json.dumps(base)+"\n"+json.dumps({**base,"timestamp":timestamp+60,"module_serial":"SN-X",
                    "position_history_id":"PHS-1","identity_source":"position_history"})+"\n")
    result=CellHistorySeries(directory).query_bundle(metric="soc",timestamp_from="2026-08-15T00:00:00+00:00",
        timestamp_to="2026-08-16T00:00:00+00:00",module_number=3)
    assert "module_serial" not in result["points"][0]
    assert result["points"][1]["module_serial"] == "SN-X"
    assert result["points"][1]["identity_source"] == "position_history"
