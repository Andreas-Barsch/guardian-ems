import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from guardian_diagnostics import (GuardianDiagnosticsApi,
                                  GuardianDiagnosticsRepository,
                                  read_validated_daily_result)
import config_ui


def write_day(root, day, *, status="complete", aggregates=None, warnings=None,
              fingerprint=None):
    fingerprint = fingerprint or (day.replace("-", "") + "f" * 40)
    result_id = "DDR-" + day
    day_dir = root / "daily" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": 1, "diagnostic_date": day, "timezone": "Europe/Berlin",
        "daily_result_id": result_id, "input_fingerprint": fingerprint,
        "overall_status": status,
        "sources": {"rs485": {"missing": False}, "cell": {"missing": False}},
        "components": {"bms_management": {
            "status": status, "quality": "complete" if status == "complete" else "limited",
            "warnings": list(warnings or []), "coverage": {"rs485_records": 10,
            "cell_records": 20}, "provenance": {"causality": "not_determined"}}},
        "provenance": {"component_manifest": {"bms_management": "1"}},
        "execution": {"completed_at": "2026-09-02T00:15:00+00:00"},
    }
    name = fingerprint + ".json"
    (day_dir / name).write_text(json.dumps(result), encoding="utf-8")
    index = {"schema_version": 1, "diagnostic_date": day,
             "daily_result_id": result_id, "input_fingerprint": fingerprint,
             "result": name, "overall_status": status,
             "updated_at": "2026-09-02T00:15:00+00:00"}
    (day_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
    if aggregates is not None:
        target = root / "aggregates" / "bms_management" / f"{day}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema_version": 1, "aggregates": aggregates}),
                          encoding="utf-8")
    return result


def aggregate(day="2026-08-31", serial="Y225004C32250226", **values):
    return {"schema_version": 1, "day": day, "physical_serial": serial,
            "ccl_reduction_event_count": 0, "ccl_zero_event_count": 0,
            "dcl_reduction_event_count": 7, "dcl_zero_event_count": 7,
            "dcl_zero_count": 7, "dcl_zero_despite_enable_count": 7,
            "dcl_zero_total_observed_duration_seconds": 52.313,
            "observed_management_duration_seconds": 52.313 / 0.00564049,
            "management_coverage_ratio_of_day": 0.107343448,
            "dcl_zero_duty_cycle": 0.00564049,
            "dominant_lowest_cell": 8, "dominant_lowest_ratio": 1.0,
            "max_spread_before_dcl_zero_mv": 398,
            "minimum_cell_before_dcl_zero_mv": 2884,
            "dominant_0x44_transition": "offset:0:11->00",
            "dominant_0x44_ratio": 1.0, "max_discharge_current_before_a": -22.413,
            "causality": "not_determined", **values}


def api(root):
    return GuardianDiagnosticsApi(GuardianDiagnosticsRepository(root))


def test_overview_without_data_and_read_only_contract(tmp_path):
    service = api(tmp_path)
    response = service.handle("GET", "/api/diagnostics/overview")
    assert response.status == 200
    assert response.body["latest_date"] is None
    assert response.body["data_quality"]["status"] == "insufficient_history"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        denied = service.handle(method, "/api/diagnostics/overview")
        assert denied.status == 405 and denied.headers == {"Allow": "GET"}
    assert list(tmp_path.iterdir()) == []


def test_complete_partial_multiple_days_windows_and_sorting(tmp_path):
    write_day(tmp_path, "2026-08-31", aggregates=[aggregate()])
    write_day(tmp_path, "2026-09-01", status="partial", aggregates=[],
              warnings=["identity_missing"])
    write_day(tmp_path, "2026-08-20", aggregates=[])
    days = api(tmp_path).handle("GET", "/api/diagnostics/days").body["days"]
    assert [item["date"] for item in days] == ["2026-09-01", "2026-08-31", "2026-08-20"]
    assert days[0]["quality"]["interpretation_limited"] is True
    overview = api(tmp_path).handle("GET", "/api/diagnostics/overview").body
    assert overview["windows"]["7d"]["available_days"] == 2
    assert overview["windows"]["30d"]["available_days"] == 3
    assert overview["windows"]["7d"]["partial_days"] == 1
    assert overview["windows"]["7d"]["missing_days_are_not_zero_days"] is True
    assert overview["data_quality"]["label"] == "Datenlage teilweise"
    assert any("Datenlage teilweise" in item["text"]
               for item in overview["important_observations"])


def test_complete_zero_event_day_is_explicit_observation(tmp_path):
    write_day(tmp_path, "2026-09-01", aggregates=[])
    detail = api(tmp_path).handle("GET", "/api/diagnostics/daily/2026-09-01").body
    assert detail["overall_status"] == "complete"
    assert detail["summary"]["event_count"] == 0
    assert any("Keine besonderen" in item["text"]
               for item in detail["summary"]["important_observations"])


def test_daily_detail_reference_semantics(tmp_path):
    row = aggregate()
    write_day(tmp_path, "2026-08-31", aggregates=[row])
    response = api(tmp_path).handle("GET", "/prefix/api/diagnostics/daily/2026-08-31")
    assert response.status == 200
    detail = response.body
    projected = detail["bms_management"]["aggregates"][0]
    assert projected["dcl_zero_event_count"] == 7
    assert projected["dcl_zero_despite_enable_count"] == 7
    assert projected["dominant_lowest_cell"] == 8
    assert projected["dominant_0x44_transition"] == "offset:0:11->00"
    assert projected["dominant_0x44_ratio"] == 1.0
    assert projected["max_spread_before_dcl_zero_mv"] == 398
    assert projected["minimum_cell_before_dcl_zero_mv"] == 2884
    assert projected["causality"] == "not_determined"
    assert projected["dcl_zero_duty_cycle"] == pytest.approx(
        projected["dcl_zero_total_observed_duration_seconds"]
        / projected["observed_management_duration_seconds"])


def test_bms_aggregate_events_missing_and_raw_frame_redaction(tmp_path):
    write_day(tmp_path, "2026-08-31", aggregates=[aggregate()])
    missing_aggregate = api(tmp_path).handle(
        "GET", "/api/diagnostics/bms-management/aggregate/2026-09-01")
    missing_events = api(tmp_path).handle(
        "GET", "/api/diagnostics/bms-management/events/2026-09-01")
    assert missing_aggregate.status == missing_events.status == 404
    path = tmp_path / "events" / "bms_management" / "2026-08-31.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"event_id": "BME-1", "raw_frame": "secret",
                                "cell_context": {"min_cell_number": 8},
                                "causality": "not_determined"}) + "\n", encoding="utf-8")
    response = api(tmp_path).handle(
        "GET", "/api/diagnostics/bms-management/events/2026-08-31")
    assert response.status == 200 and response.body["event_count"] == 1
    assert "raw_frame" not in json.dumps(response.body)


def test_daily_source_projection_does_not_expose_physical_paths(tmp_path):
    result = write_day(tmp_path, "2026-08-31", aggregates=[])
    day_dir = tmp_path / "daily" / "2026-08-31"
    index = json.loads((day_dir / "index.json").read_text())
    result["sources"]["rs485"]["files"] = [{"path": "/private/raw/rs485.jsonl"}]
    (day_dir / index["result"]).write_text(json.dumps(result), encoding="utf-8")
    response = api(tmp_path).handle("GET", "/api/diagnostics/daily/2026-08-31")
    assert response.status == 200
    assert "/private/raw" not in json.dumps(response.body)


def test_invalid_dates_traversal_and_missing_result(tmp_path):
    service = api(tmp_path)
    for value in ("../state", "2026-8-31", "2026-02-30", "%2e%2e%2fstate"):
        response = service.handle("GET", f"/api/diagnostics/daily/{value}")
        assert response.status == 400
    assert service.handle("GET", "/api/diagnostics/daily/2026-09-02").status == 404


def test_result_symlink_cannot_escape_diagnostics_root(tmp_path):
    outside = tmp_path.parent / "outside-diagnostic-result.json"
    outside.write_text(json.dumps({"secret": True}), encoding="utf-8")
    day_dir = tmp_path / "daily" / "2026-08-31"
    day_dir.mkdir(parents=True)
    (day_dir / "escape.json").symlink_to(outside)
    (day_dir / "index.json").write_text(json.dumps({
        "schema_version": 1, "diagnostic_date": "2026-08-31",
        "daily_result_id": "DDR-escape", "input_fingerprint": "fingerprint",
        "result": "escape.json", "overall_status": "complete"}), encoding="utf-8")
    response = api(tmp_path).handle("GET", "/api/diagnostics/daily/2026-08-31")
    assert response.status == 503
    assert "secret" not in json.dumps(response.body)


def test_corrupt_index_revision_and_mismatch_are_controlled(tmp_path):
    service = api(tmp_path)
    day_dir = tmp_path / "daily" / "2026-08-31"
    day_dir.mkdir(parents=True)
    (day_dir / "index.json").write_text("{", encoding="utf-8")
    assert service.handle("GET", "/api/diagnostics/daily/2026-08-31").status == 503
    (day_dir / "index.json").write_text(json.dumps({"schema_version": 1,
        "diagnostic_date": "2026-08-31", "daily_result_id": "x",
        "input_fingerprint": "f", "overall_status": "complete",
        "result": "missing.json"}), encoding="utf-8")
    assert service.handle("GET", "/api/diagnostics/daily/2026-08-31").status == 503
    write_day(tmp_path, "2026-08-31", aggregates=[])
    index_path = day_dir / "index.json"
    index = json.loads(index_path.read_text())
    result_path = day_dir / index["result"]
    result = json.loads(result_path.read_text())
    result["daily_result_id"] = "mismatch"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    assert service.handle("GET", "/api/diagnostics/daily/2026-08-31").status == 503


def test_overview_keeps_valid_days_when_one_day_is_corrupt(tmp_path):
    write_day(tmp_path, "2026-08-31", aggregates=[aggregate()])
    corrupt = tmp_path / "daily" / "2026-08-30"
    corrupt.mkdir(parents=True)
    (corrupt / "index.json").write_text("bad", encoding="utf-8")
    result = api(tmp_path).handle("GET", "/api/diagnostics/overview")
    assert result.status == 200 and result.body["available_day_count"] == 1
    assert result.body["unavailable_days"] == ["2026-08-30"]


def test_get_requests_do_not_change_derived_files_or_read_raw_history(tmp_path,
                                                                     monkeypatch):
    write_day(tmp_path, "2026-08-31", aggregates=[aggregate()])
    raw = tmp_path / "rs485_history" / "2026-08-31.jsonl"
    raw.parent.mkdir()
    raw.write_text("DO NOT READ", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    seen = []
    original = Path.read_text
    def tracked(path, *args, **kwargs):
        seen.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", tracked)
    service = api(tmp_path)
    for route in ("overview", "days", "daily/2026-08-31",
                  "bms-management/aggregate/2026-08-31"):
        assert service.handle("GET", "/api/diagnostics/" + route).status == 200
    after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after
    assert not any("rs485_history" in item or "cell_history" in item for item in seen)


def test_thirty_day_overview_is_lightweight(tmp_path):
    for offset in range(30):
        day = (datetime(2026, 9, 1) - timedelta(days=offset)).date().isoformat()
        write_day(tmp_path, day, aggregates=[aggregate(day=day, serial=f"SERIAL-{offset}")])
    started = time.perf_counter()
    overview = api(tmp_path).handle("GET", "/api/diagnostics/overview")
    elapsed = time.perf_counter() - started
    assert overview.status == 200 and overview.body["available_day_count"] == 30
    assert elapsed < 1.0


def test_ccL_peer_values_enable_and_staged_limits_remain_separate(tmp_path):
    rows = [aggregate(day="2026-09-01", serial=f"M{number}", ccl_min_a=value,
                      ccl_median_a=value, ccl_zero_event_count=int(value == 0),
                      ccl_reduction_event_count=1,
                      charge_enable=True,
                      ccl_observed_a=value, ccl_peer_median_a=25,
                      ccl_peer_deviation_a=value - 25,
                      relative_ccl_ratio=value / 25)
            for number, value in enumerate((10, 10, 5, 0, 0, 0), 1)]
    write_day(tmp_path, "2026-09-01", aggregates=rows)
    detail = api(tmp_path).handle("GET", "/api/diagnostics/daily/2026-09-01").body
    first = detail["bms_management"]["aggregates"][0]
    assert (first["ccl_observed_a"], first["ccl_peer_median_a"],
            first["ccl_peer_deviation_a"], first["relative_ccl_ratio"]) == (10, 25, -15, .4)
    assert all(row["charge_enable"] is True for row in detail["bms_management"]["aggregates"])


def test_existing_config_server_routes_diagnostics_get_and_rejects_write(tmp_path,
                                                                         monkeypatch):
    write_day(tmp_path, "2026-08-31", aggregates=[aggregate()])
    monkeypatch.setattr(config_ui, "DAILY_DIAGNOSTICS_ROOT", tmp_path)
    monkeypatch.setattr(config_ui, "_DIAGNOSTICS_API", None)
    handler = object.__new__(config_ui.Handler)
    handler.path = "/api/hassio_ingress/token/api/diagnostics/overview"
    handler.headers = {"X-Ingress-Path": "/api/hassio_ingress/token"}
    handler._ingress_allowed = lambda: True
    captured = {}
    handler._send = lambda code, body, ctype="application/json", headers=None: captured.update(
        code=code, body=body, headers=headers or {})
    handler.do_GET()
    assert captured["code"] == 200 and captured["body"]["latest_date"] == "2026-08-31"
    handler.do_POST()
    assert captured["code"] == 405 and captured["headers"] == {"Allow": "GET"}


@pytest.mark.parametrize("token", ["dynamic-token-a", "dynamic-token-b"])
def test_diagnostics_ui_route_uses_current_dynamic_ingress_base(token):
    handler = object.__new__(config_ui.Handler)
    prefix = f"/api/hassio_ingress/{token}"
    handler.path = prefix + "/diagnostics"
    handler.headers = {"X-Ingress-Path": prefix}
    assert handler._is_diagnostics_ui() is True
    assert handler._ingress_base() == prefix
    handler._ingress_allowed = lambda: True
    captured = {}
    handler._send = lambda code, body, ctype="application/json", headers=None: captured.update(
        code=code, body=body, ctype=ctype)
    handler.do_GET()
    assert captured["code"] == 200 and captured["ctype"] == "text/html"
    assert f"const API='{prefix}/api/diagnostics'" in captured["body"]
    assert 'href="./">Module &amp; Stack</a>' in captured["body"]
    assert "3195b09a_guardian_battery" not in captured["body"]
