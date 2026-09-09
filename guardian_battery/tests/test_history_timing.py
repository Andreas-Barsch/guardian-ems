import json
import threading
import time
from datetime import datetime, timezone

import pytest
import config_ui
from config_ui import Handler
from event_overlay import EventOverlayAdapter
from history_api import HistoryApi
from history_series import CellHistorySeries
from history_timing import HistoryRequestTimingState
from hycube_evidence import HycubeBatteryCapacitySeries, HycubeHistoryError
from maintenance_api import ApiResponse
from test_timeline import build


def record(timestamp, module=1):
    return {"schema_version": 1, "timestamp": timestamp, "module": module,
            "voltages_mv": [3300] * 15, "current_a": 0,
            "soc_percent": 50, "temperatures_c": [25] * 15,
            "balancing": [False] * 15, "physical_groups": {}}


def test_cross_day_reference_counts_and_no_response_semantics_change(tmp_path):
    maintenance, _, timeline = build(tmp_path)
    directory = tmp_path / "cell"; directory.mkdir()
    start = datetime(2026, 9, 7, 22, 39, tzinfo=timezone.utc).timestamp()
    for day, rows in (("2026-09-07", [record(start), record(start, 2)]),
                      ("2026-09-08", [record(start + 3600)])):
        (directory / f"{day}.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in rows))
    plain = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline))
    state = HistoryRequestTimingState()
    timed = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                       timing_state=state)
    target = ("/api/history/series?metric=soc&from=2026-09-07T22:39:00Z"
              "&to=2026-09-08T22:39:00Z&module_number=1")
    expected = plain.handle("GET", target)
    actual = timed.handle("GET", target)
    assert actual.status == expected.status
    assert {key: value for key, value in actual.body.items() if key != "performance"} == {
        key: value for key, value in expected.body.items() if key != "performance"}
    context = state.context()
    assert context["counts"]["cell_files_opened"] == 2
    assert context["counts"]["cell_raw_lines"] == 3
    assert context["counts"]["cell_lines_rejected_before_json"] == 1
    assert context["counts"]["cell_parsed_records"] == 2
    assert context["counts"]["cell_records_for_requested_modules"] == 2
    assert "voltages_mv" not in json.dumps(state.snapshot())
    state.complete()
    assert state.snapshot()["last_completed_request"]["status"] == "completed"


def test_cache_hit_marks_expensive_stages_not_executed(tmp_path):
    _, _, timeline = build(tmp_path)
    directory = tmp_path / "cell"; directory.mkdir()
    timestamp = datetime(2026, 9, 7, 23, tzinfo=timezone.utc).timestamp()
    (directory / "2026-09-07.jsonl").write_text(json.dumps(record(timestamp)) + "\n")
    state = HistoryRequestTimingState(); series = CellHistorySeries(directory)
    api = HistoryApi(series, EventOverlayAdapter(timeline), timing_state=state)
    target = ("/api/history/series?metric=soc&from=2026-09-07T22:39:00Z"
              "&to=2026-09-08T22:39:00Z&module_number=1")
    api.handle("GET", target); state.complete()
    api.handle("GET", target)
    assert state.context()["counts"]["cache_hit"] is True
    assert state.context()["components"]["cell_read_parse_filter"]["status"] == "not_executed"
    state.complete()


def test_non_soc_request_marks_policy_hycube_and_identity_not_executed(tmp_path):
    _, _, timeline = build(tmp_path)
    directory = tmp_path / "cell"; directory.mkdir()
    timestamp = datetime(2026, 9, 7, 23, tzinfo=timezone.utc).timestamp()
    (directory / "2026-09-07.jsonl").write_text(json.dumps(record(timestamp)) + "\n")
    state = HistoryRequestTimingState()
    api = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                     timing_state=state)
    response = api.handle("GET", "/api/history/series?metric=current"
                          "&from=2026-09-07T22:39:00Z&to=2026-09-08T22:39:00Z"
                          "&module_number=1")
    assert response.status == 200
    components = state.context()["components"]
    assert components["policy"]["status"] == "not_executed"
    assert components["hycube_discovery"]["status"] == "not_executed"
    assert components["identity"]["status"] == "not_executed"
    assert response.body["soc_timeline"] is None


def test_slow_response_write_is_measured_as_wall_not_cpu(monkeypatch):
    state = HistoryRequestTimingState(); monkeypatch.setattr(config_ui, "_HISTORY_TIMING", state)
    state.begin({"view": "single"})
    class Writer:
        def write(self, raw): time.sleep(.03); self.raw = raw
    handler = Handler.__new__(Handler); handler.wfile = Writer()
    handler.send_response = lambda _code: None
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler._send_history(ApiResponse(200, {"ok": True}))
    last = state.snapshot()["last_completed_request"]
    write = last["components"]["response_write"]
    assert write["wall_seconds"] >= .025
    assert write["thread_cpu_seconds"] < .01
    assert last["counts"]["response_bytes"] == len(b'{"ok": true}')


def test_parallel_requests_follow_actual_completion_order_and_failure_recovers():
    state = HistoryRequestTimingState(); release = threading.Event()
    def older():
        state.begin({"metric": "old"}); release.wait(); state.complete()
    thread = threading.Thread(target=older); thread.start()
    while state.snapshot()["current_request"] is None: time.sleep(.001)
    state.begin({"metric": "new"}); state.complete()
    assert state.snapshot()["last_completed_request"]["metric"] == "new"
    assert state.snapshot()["current_request"]["metric"] == "old"
    release.set(); thread.join()
    assert state.snapshot()["last_completed_request"]["metric"] == "old"
    state.begin({"metric": "failed"}); state.complete(failed=True, error="ValueError")
    assert state.snapshot()["last_completed_request"]["status"] == "failed"
    state.begin({"metric": "next"}); state.complete()
    assert state.snapshot()["last_completed_request"]["status"] == "completed"


def test_observability_clock_failure_does_not_change_history_response(tmp_path):
    _, _, timeline = build(tmp_path)
    directory = tmp_path / "cell"; directory.mkdir()
    timestamp = datetime(2026, 9, 7, 23, tzinfo=timezone.utc).timestamp()
    (directory / "2026-09-07.jsonl").write_text(json.dumps(record(timestamp)) + "\n")
    expected = HistoryApi(CellHistorySeries(directory),
                          EventOverlayAdapter(timeline))

    def broken_clock():
        raise RuntimeError("timing unavailable")

    state = HistoryRequestTimingState(monotonic=broken_clock)
    actual = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                        timing_state=state)
    target = ("/api/history/series?metric=soc&from=2026-09-07T22:39:00Z"
              "&to=2026-09-08T22:39:00Z&module_number=1")
    actual_response = actual.handle("GET", target)
    expected_response = expected.handle("GET", target)
    assert actual_response.status == expected_response.status
    assert {key: value for key, value in actual_response.body.items()
            if key != "performance"} == {
                key: value for key, value in expected_response.body.items()
                if key != "performance"}
    assert state.snapshot()["observability_error_count"] >= 1


def test_failed_snapshot_has_bounded_error_type_and_message():
    state = HistoryRequestTimingState()
    state.begin({"metric": "soc"})
    state.complete(failed=True, error=ValueError("x" * 500))
    failed = state.snapshot()["last_completed_request"]
    assert failed["error_type"] == "ValueError"
    assert len(failed["error_message"]) == 160
    assert "traceback" not in json.dumps(failed).lower()


def test_cell_parse_error_count_uses_existing_failure_semantics(tmp_path):
    _, _, timeline = build(tmp_path)
    directory = tmp_path / "cell"; directory.mkdir()
    (directory / "2026-09-07.jsonl").write_text("{not-json}\n")
    state = HistoryRequestTimingState()
    api = HistoryApi(CellHistorySeries(directory), EventOverlayAdapter(timeline),
                     timing_state=state)
    target = ("/api/history/series?metric=soc&from=2026-09-07T00:00:00Z"
              "&to=2026-09-07T23:59:59Z&module_number=1")
    response = api.handle("GET", target)
    assert response.status == 503
    assert state.context()["counts"]["cell_parse_errors"] == 1
    assert state.context()["counts"]["cell_invalid_records"] == 0
    state.complete(failed=True)
    assert state.snapshot()["last_completed_request"]["error_type"] == "SeriesHistoryError"


def test_hycube_parse_error_count_uses_existing_failure_semantics(tmp_path):
    directory = tmp_path / "hycube"; directory.mkdir()
    (directory / "2026-09-07.jsonl").write_text("{not-json}\n")
    state = HistoryRequestTimingState(); state.begin({"metric": "soc"})
    with pytest.raises(HycubeHistoryError):
        HycubeBatteryCapacitySeries(directory).query(
            timestamp_from="2026-09-07T00:00:00+00:00",
            timestamp_to="2026-09-07T23:59:59+00:00", timing=state)
    assert state.context()["counts"]["hycube_parse_errors"] == 1
    assert state.context()["counts"]["hycube_invalid_records"] == 0
