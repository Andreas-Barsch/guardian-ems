from test_timeline import add, build
from timeline_api import TimelineApi


def test_api_response_window_sorting_and_multiple_types(tmp_path):
    maintenance, _, service = build(tmp_path)
    add(maintenance)
    response = TimelineApi(service).handle(
        "GET", "/ingress/token/api/timeline?from=2024-04-05T09%3A00%3A00Z&to=2024-04-05T09%3A00%3A00Z&event_type=maintenance%2Cstatus_changed&module_number=3&cell_number=7&category=inspection"
    )
    assert response.status == 200
    assert len(response.body["events"]) == 1
    assert response.body["window"]["inclusive"] is True
    assert response.body["sorting"]["direction"] == "oldest_first"


def test_api_rejects_invalid_parameters_and_method(tmp_path):
    _, _, service = build(tmp_path)
    api = TimelineApi(service)
    targets = ["?from=invalid", "?from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z",
               "?event_type=bogus", "?module_number=7", "?cell_number=0",
               "?include_archived=yes", "?unknown=value"]
    for suffix in targets:
        response = api.handle("GET", "/api/timeline" + suffix)
        assert response.status == 400
        assert response.body["error"]["code"] == "invalid_request"
    assert api.handle("POST", "/api/timeline").status == 405


def test_api_reports_each_broken_source_as_503(tmp_path):
    maintenance, technical_path, service = build(tmp_path)
    add(maintenance)
    technical_path.write_text("broken", encoding="utf-8")
    response = TimelineApi(service).handle("GET", "/api/timeline")
    assert response.status == 503
    assert response.body["error"]["code"] == "technical_history_error"

    maintenance.repository.log.path.write_text("broken", encoding="utf-8")
    technical_path.unlink()
    response = TimelineApi(service).handle("GET", "/api/timeline")
    assert response.status == 503
    assert response.body["error"]["code"] == "maintenance_history_error"
