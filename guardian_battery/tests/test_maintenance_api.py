import json
from datetime import datetime, timedelta, timezone

import pytest

from maintenance import MAINTENANCE_TEXT_LIMITS, MaintenanceEventLog
from maintenance_api import MAX_PAGE_LIMIT, MAX_REQUEST_BODY_BYTES, MaintenanceApi
from maintenance_service import MaintenanceRepository, MaintenanceService


BASE_TIME = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8"}


class Clock:
    def __init__(self):
        self.value = BASE_TIME

    def __call__(self):
        current = self.value
        self.value += timedelta(minutes=1)
        return current


@pytest.fixture
def api_env(tmp_path):
    path = tmp_path / "maintenance_events.jsonl"
    service = MaintenanceService(
        MaintenanceRepository(MaintenanceEventLog(path)),
        clock=Clock(),
    )
    return MaintenanceApi(service), service, path


def request(api, method, target, payload=None, headers=None, raw=None):
    if raw is None:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return api.handle(method, target, headers or (JSON_HEADERS if payload is not None else {}), raw)


def create_payload(**overrides):
    payload = {
        "occurred_at": "2025-03-12T14:00:00+01:00",
        "category": "maintenance",
        "title": "Batterie geprüft",
        "description": "Sichtprüfung",
        "affected_system": "Pylontech Stack",
        "module_number": 1,
    }
    payload.update(overrides)
    return payload


def create(api, **overrides):
    response = request(api, "POST", "/api/maintenance/events", create_payload(**overrides))
    assert response.status == 201
    return response.body["event"]


def test_create_returns_canonical_generated_event(api_env):
    api, _, _ = api_env

    response = request(
        api,
        "POST",
        "/ingress/session/api/maintenance/events",
        create_payload(title="<script>data only</script>"),
    )

    assert response.status == 201
    event = response.body["event"]
    assert event["maintenance_event_id"].startswith("MEV-")
    assert event["revision"] == 1
    assert event["occurred_at"] == "2025-03-12T13:00:00+00:00"
    assert event["created_at"] == "2026-08-20T10:00:00+00:00"
    assert event["updated_at"] is None
    assert event["source"] == {"kind": "manual"}
    assert event["title"] == "<script>data only</script>"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"category": "Ungültig"}, "validation_error"),
        ({"module_number": 7}, "validation_error"),
        ({"cell_number": 16}, "validation_error"),
        ({"occurred_at": "not-a-time"}, "validation_error"),
    ],
)
def test_create_validation_errors(api_env, overrides, code):
    api, _, _ = api_env

    response = request(api, "POST", "/api/maintenance/events", create_payload(**overrides))

    assert response.status == 400
    assert response.body["error"]["code"] == code


def test_create_rejects_unknown_and_server_owned_fields(api_env):
    api, _, _ = api_env

    for field in ("unknown", "maintenance_event_id", "created_at", "source"):
        response = request(
            api,
            "POST",
            "/api/maintenance/events",
            create_payload(**{field: "forbidden"}),
        )
        assert response.status == 400
        assert response.body["error"]["code"] == "invalid_request"
        assert field in response.body["error"]["details"]["fields"]


def test_create_rejects_missing_required_fields(api_env):
    api, _, _ = api_env
    payload = create_payload()
    payload.pop("title")

    response = request(api, "POST", "/api/maintenance/events", payload)

    assert response.status == 400
    assert response.body["error"]["code"] == "invalid_request"
    assert response.body["error"]["details"]["fields"] == ["title"]


@pytest.mark.parametrize(
    "field",
    [
        "title",
        "description",
        "affected_system",
        "module_serial",
        "action_taken",
        "previous_state",
        "result",
        "reason",
    ],
)
def test_create_enforces_central_text_limits(api_env, field):
    api, _, _ = api_env
    payload = create_payload(**{field: "x" * (MAINTENANCE_TEXT_LIMITS[field] + 1)})

    response = request(api, "POST", "/api/maintenance/events", payload)

    assert response.status == 400
    assert response.body["error"]["code"] == "validation_error"


def test_body_limit_malformed_json_and_content_type(api_env):
    api, _, _ = api_env

    too_large = request(
        api,
        "POST",
        "/api/maintenance/events",
        headers=JSON_HEADERS,
        raw=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
    )
    malformed = request(
        api,
        "POST",
        "/api/maintenance/events",
        headers=JSON_HEADERS,
        raw=b'{"broken":',
    )
    wrong_type = request(
        api,
        "POST",
        "/api/maintenance/events",
        headers={"Content-Type": "text/plain"},
        raw=b"{}",
    )

    assert too_large.status == 413
    assert too_large.body["error"]["code"] == "request_too_large"
    assert malformed.status == 400
    assert malformed.body["error"]["code"] == "invalid_request"
    assert wrong_type.status == 400
    assert wrong_type.body["error"]["code"] == "invalid_request"


def test_get_detail_unknown_and_archived(api_env):
    api, _, _ = api_env
    event = create(api)
    event_id = event["maintenance_event_id"]

    detail = request(api, "GET", f"/api/maintenance/events/{event_id}")
    missing = request(
        api,
        "GET",
        "/api/maintenance/events/MEV-00000000-0000-4000-8000-000000000000",
    )
    archived = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/archive",
        {"expected_revision": 1},
    )
    archived_detail = request(api, "GET", f"/api/maintenance/events/{event_id}")

    assert detail.status == 200
    assert missing.status == 404
    assert missing.body["error"]["code"] == "not_found"
    assert archived.status == 200
    assert archived_detail.status == 200
    assert archived_detail.body["event"]["archived_at"] is not None


def test_list_defaults_archives_sorting_and_pagination(api_env):
    api, _, _ = api_env
    old = create(api, occurred_at="2024-01-01T00:00:00+00:00", title="old")
    middle = create(api, occurred_at="2025-01-01T00:00:00+00:00", title="middle")
    new = create(api, occurred_at="2026-01-01T00:00:00+00:00", title="new")
    request(
        api,
        "POST",
        f"/api/maintenance/events/{middle['maintenance_event_id']}/archive",
        {"expected_revision": 1},
    )

    default = request(api, "GET", "/api/maintenance/events")
    chronological = request(
        api,
        "GET",
        "/api/maintenance/events?include_archived=true&newest_first=false&limit=2&offset=1",
    )

    assert [item["title"] for item in default.body["events"]] == ["new", "old"]
    assert [item["title"] for item in chronological.body["events"]] == ["middle", "new"]
    assert chronological.body["pagination"] == {
        "limit": 2,
        "offset": 1,
        "returned": 2,
        "total": 3,
    }
    assert old["maintenance_event_id"] != new["maintenance_event_id"]


def test_list_time_category_module_cell_and_combined_filters(api_env):
    api, _, _ = api_env
    create(
        api,
        occurred_at="2025-01-10T00:00:00+00:00",
        category="inspection",
        module_number=2,
        cell_number=4,
        title="match",
    )
    create(
        api,
        occurred_at="2025-02-10T00:00:00+00:00",
        category="repair",
        module_number=2,
        cell_number=5,
        title="other",
    )

    targets = {
        "time": "/api/maintenance/events?occurred_from=2025-01-01T00%3A00%3A00%2B00%3A00&occurred_to=2025-01-31T23%3A59%3A59%2B00%3A00",
        "category": "/api/maintenance/events?category=inspection",
        "module": "/api/maintenance/events?module_number=2",
        "cell": "/api/maintenance/events?cell_number=4",
        "combined": "/api/maintenance/events?category=inspection&module_number=2&cell_number=4&occurred_from=2025-01-01T00%3A00%3A00%2B00%3A00",
    }

    assert [e["title"] for e in request(api, "GET", targets["time"]).body["events"]] == ["match"]
    assert [e["title"] for e in request(api, "GET", targets["category"]).body["events"]] == ["match"]
    assert len(request(api, "GET", targets["module"]).body["events"]) == 2
    assert [e["title"] for e in request(api, "GET", targets["cell"]).body["events"]] == ["match"]
    assert [e["title"] for e in request(api, "GET", targets["combined"]).body["events"]] == ["match"]


@pytest.mark.parametrize(
    "query",
    [
        "include_archived=yes",
        "newest_first=1",
        "module_number=0",
        "cell_number=16",
        "limit=0",
        f"limit={MAX_PAGE_LIMIT + 1}",
        "offset=-1",
        "occurred_from=invalid",
        "occurred_from=2026-01-01T00%3A00%3A00%2B00%3A00&occurred_to=2025-01-01T00%3A00%3A00%2B00%3A00",
        "unknown=value",
        "category=Not-A-Slug",
    ],
)
def test_invalid_list_filters_are_client_errors(api_env, query):
    api, _, _ = api_env

    response = request(api, "GET", f"/api/maintenance/events?{query}")

    assert response.status == 400
    assert response.body["error"]["code"] in {"invalid_request", "validation_error"}


def test_patch_success_missing_revision_stale_immutable_and_empty(api_env):
    api, _, _ = api_env
    event = create(api)
    event_id = event["maintenance_event_id"]

    missing_revision = request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"changes": {"title": "x"}},
    )
    success = request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"expected_revision": 1, "changes": {"title": "updated"}},
    )
    stale = request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"expected_revision": 1, "changes": {"title": "stale"}},
    )
    immutable = request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"expected_revision": 2, "changes": {"created_at": BASE_TIME.isoformat()}},
    )
    empty = request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"expected_revision": 2, "changes": {}},
    )

    assert missing_revision.status == 400
    assert success.status == 200 and success.body["event"]["revision"] == 2
    assert stale.status == 409
    assert stale.body["error"]["details"] == {
        "maintenance_event_id": event_id,
        "expected_revision": 1,
        "actual_revision": 2,
    }
    assert immutable.status == 400
    assert empty.status == 400


def test_archive_and_restore_revision_conflicts_and_states(api_env):
    api, _, _ = api_env
    event = create(api)
    event_id = event["maintenance_event_id"]
    updated = request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"expected_revision": 1, "changes": {"result": "done"}},
    )

    stale_archive = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/archive",
        {"expected_revision": 1},
    )
    archived = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/archive",
        {"expected_revision": 2},
    )
    duplicate_archive = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/archive",
        {"expected_revision": 3},
    )
    stale_restore = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/restore",
        {"expected_revision": 2},
    )
    restored = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/restore",
        {"expected_revision": 3},
    )
    active_restore = request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/restore",
        {"expected_revision": 4},
    )

    assert updated.status == 200
    assert stale_archive.status == 409
    assert archived.status == 200 and archived.body["event"]["revision"] == 3
    assert duplicate_archive.status == 409
    assert stale_restore.status == 409
    assert restored.status == 200 and restored.body["event"]["revision"] == 4
    assert active_restore.status == 409


def test_active_api_and_three_way_list_filter_are_backward_compatible(api_env):
    api, _, _ = api_env
    first = create(api, title="Position 5", module_number=5, module_serial=None)
    event_id = first["maintenance_event_id"]
    inactive = request(api, "POST", f"/api/maintenance/events/{event_id}/deactivate",
                       {"expected_revision": 1})
    stale = request(api, "POST", f"/api/maintenance/events/{event_id}/activate",
                    {"expected_revision": 1})
    assert inactive.status == 200
    assert inactive.body["event"]["active"] is False
    assert inactive.body["event"]["module_number"] == 5
    assert inactive.body["event"]["module_serial"] is None
    assert stale.status == 409
    assert request(api, "GET", "/api/maintenance/events?active=true").body["events"] == []
    assert len(request(api, "GET", "/api/maintenance/events?active=false").body["events"]) == 1
    assert len(request(api, "GET", "/api/maintenance/events?active=all").body["events"]) == 1
    active = request(api, "POST", f"/api/maintenance/events/{event_id}/activate",
                     {"expected_revision": 2})
    assert active.body["event"]["active"] is True
    assert active.body["event"]["revision"] == 3


def test_history_returns_full_sequence_and_unknown_is_404(api_env):
    api, _, _ = api_env
    event = create(api)
    event_id = event["maintenance_event_id"]
    request(
        api,
        "PATCH",
        f"/api/maintenance/events/{event_id}",
        {"expected_revision": 1, "changes": {"result": "done"}},
    )
    request(
        api,
        "POST",
        f"/api/maintenance/events/{event_id}/archive",
        {"expected_revision": 2},
    )

    history = request(api, "GET", f"/api/maintenance/events/{event_id}/history")
    missing = request(
        api,
        "GET",
        "/api/maintenance/events/MEV-00000000-0000-4000-8000-000000000000/history",
    )

    assert [item["revision"] for item in history.body["history"]] == [1, 2, 3]
    assert missing.status == 404


def test_method_not_allowed_has_uniform_error_and_allow_header(api_env):
    api, _, _ = api_env

    response = request(api, "DELETE", "/api/maintenance/events")

    assert response.status == 405
    assert response.body["error"]["code"] == "method_not_allowed"
    assert response.headers["Allow"] == "GET, POST"


def test_corrupt_history_is_503_without_traceback(api_env):
    api, _, path = api_env
    path.write_text('{"broken":\n', encoding="utf-8")

    response = request(api, "GET", "/api/maintenance/events")
    serialized = json.dumps(response.body)

    assert response.status == 503
    assert response.body["error"]["code"] == "history_error"
    assert "Traceback" not in serialized
    assert str(path) not in serialized


def test_unexpected_domain_failure_is_500_without_traceback(tmp_path):
    class BrokenService:
        def list(self, **_):
            raise RuntimeError("secret internal detail")

    response = request(MaintenanceApi(BrokenService()), "GET", "/api/maintenance/events")
    serialized = json.dumps(response.body)

    assert response.status == 500
    assert response.body["error"]["code"] == "internal_error"
    assert "secret internal detail" not in serialized
    assert "Traceback" not in serialized
