import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
import research_api
import research_budget
from research_api import QueryGate
from research_budget import ResearchQueryError, check_budget, query_budget
from timeline import TechnicalEventSource
from test_research_api import environment, get, write_jsonl


class Clock:
    now = 0.0
    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(research_api, "time", value)
    monkeypatch.setattr(research_budget, "time", value)
    return value


def test_full_response_after_old_budget_but_before_sixty(clock):
    gate = QueryGate()
    result = {"data": {"alarms": [{"id": i} for i in range(174)]}, "truncated": False}
    def query(deadline):
        assert deadline == 60
        clock.now = 59.9
        check_budget()
        return result
    assert gate.run("alarms", False, query) == result
    assert gate.active == 0 and gate.audit[-1]["records"] == 174
    assert gate.audit[-1]["bytes"] == len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.mark.parametrize("elapsed", [60, 60.1])
def test_expiry_never_returns_partial_success_and_releases_all_slots(clock, elapsed):
    gate = QueryGate()
    def query(deadline):
        clock.now = elapsed
        return {"data": {"alarms": [1]}}
    with pytest.raises(ResearchQueryError, match="timed out"):
        gate.run("alarms", True, query)
    assert gate.active == 0
    assert gate.raw.acquire(False)
    gate.raw.release()
    assert gate.all.acquire(False) and gate.all.acquire(False)
    gate.all.release(); gate.all.release()
    assert gate.run("status", False, lambda _: {}) == {}
    check_budget()  # expired context was reset


def test_checked_alarm_reader_matches_legacy_and_stops_at_initial_extent(tmp_path):
    path = tmp_path / "events"
    rows = [{"timestamp": i + 1, "type": "alarm_cleared", "code": "1:test"} for i in range(4)]
    write_jsonl(path, rows)
    source = TechnicalEventSource(path)
    expected = source.read()
    appended = False
    def check():
        pass
    original = source._project
    def project(raw, number):
        nonlocal appended
        if not appended:
            with path.open("a") as handle:
                handle.write(json.dumps(rows[0]) + "\n")
            appended = True
        return original(raw, number)
    source._project = project
    assert source.read(check=check) == expected


def test_alarm_scan_timeout_closes_file_without_background_work(tmp_path, monkeypatch, clock):
    api, _, _ = environment(tmp_path)
    path = tmp_path / "alarms.jsonl"
    write_jsonl(path, [{"timestamp": 1789120800 + i, "type": "alarm_cleared", "code": "4:test"} for i in range(100)])
    api.paths = replace(api.paths, technical_events=path)
    handles = []
    opened = Path.open
    def track(p, *args, **kwargs):
        handle = opened(p, *args, **kwargs)
        if p == path: handles.append(handle)
        return handle
    monkeypatch.setattr(Path, "open", track)
    original = TechnicalEventSource._project
    projected = []
    def slow(raw, line):
        projected.append(line)
        clock.now += 20
        return original(raw, line)
    monkeypatch.setattr(TechnicalEventSource, "_project", staticmethod(slow))
    result = get(api, "alarms?from=2026-09-11T00:00:00Z&to=2026-09-12T00:00:00Z")
    assert result.status == 503 and result.body["error"]["code"] == "timeout"
    assert projected == [1, 2, 3]  # no fourth row, no abandoned worker
    assert handles and all(handle.closed for handle in handles)
    assert api.gate.active == 0
    assert get(api, "status").status == 200


def test_identity_timeout_is_not_published_as_refreshed_cache(tmp_path, monkeypatch, clock):
    api, _, _ = environment(tmp_path)
    original = research_api.ResearchIdentityResolver.from_path
    def late(path):
        result = original(path)
        clock.now = 60
        return result
    monkeypatch.setattr(research_api.ResearchIdentityResolver, "from_path", late)
    result = get(api, "status")
    assert result.status == 503 and result.body["error"]["code"] == "timeout"
    assert api._identity is None and api.gate.active == 0


def test_rejected_full_query_does_not_decrement_other_active_query():
    gate = QueryGate()
    entered, release = threading.Event(), threading.Event()
    def query(_):
        entered.set()
        assert release.wait(2)
        return {}
    worker = threading.Thread(target=lambda: gate.run("cell-history", True, query))
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(ResearchQueryError) as exc:
            gate.run("cell-history", True, lambda _: {})
        assert exc.value.code == "busy" and gate.active == 1
    finally:
        release.set(); worker.join(2)
    assert not worker.is_alive() and gate.active == 0


def test_encoding_failure_always_releases_slots():
    gate = QueryGate()
    with pytest.raises(TypeError):
        gate.run("test", True, lambda _: {"data": object()})
    assert gate.active == 0
    assert gate.run("test", True, lambda _: {}) == {}


def test_response_size_limit_unchanged():
    gate = QueryGate()
    with pytest.raises(ResearchQueryError) as exc:
        gate.run("test", False, lambda _: {"data": "x" * (2 * 1024 * 1024)})
    assert exc.value.code == "response_too_large" and gate.active == 0


def test_real_alarm_pipeline_returns_every_event_after_fifteen_seconds(tmp_path, monkeypatch, clock):
    api, _, _ = environment(tmp_path)
    path = tmp_path / "alarms.jsonl"
    write_jsonl(path, [{"timestamp": 1789120800 + i, "type": "alarm_cleared", "code": "4:test"} for i in range(174)])
    api.paths = replace(api.paths, technical_events=path)
    query = "alarms?from=2026-09-11T00:00:00Z&to=2026-09-12T00:00:00Z"
    expected = get(api, query).body
    original = TechnicalEventSource._project
    def slow(raw, line):
        clock.now += 0.3
        return original(raw, line)
    monkeypatch.setattr(TechnicalEventSource, "_project", staticmethod(slow))
    result = get(api, query)
    assert 15 < clock.now < 60
    assert result.status == 200 and result.body == expected
    assert len(result.body["data"]["alarms"]) == 174
    assert not result.body["truncated"] and api.gate.active == 0


def test_request_budget_does_not_leak_into_other_threads(clock):
    clock.now = 0
    done = []
    with query_budget(1):
        clock.now = 2
        worker = threading.Thread(target=lambda: (check_budget(), done.append(True)))
        worker.start(); worker.join(1)
        assert done == [True]
        clock.now = 0


def test_mutex_wait_is_bounded_and_preserves_owner():
    import time
    from research_budget import budget_lock
    lock = threading.Lock()
    lock.acquire()
    try:
        with pytest.raises(ResearchQueryError):
            with query_budget(time.monotonic() + 0.02):
                with budget_lock(lock):
                    pytest.fail("locked mutex acquired")
        assert lock.locked()
    finally:
        lock.release()


def test_file_lock_wait_is_bounded(tmp_path):
    import fcntl
    import time
    from research_budget import acquire_shared_file_lock
    path = tmp_path / "lock"
    path.touch()
    with path.open() as owner, path.open() as reader:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ResearchQueryError):
            with query_budget(time.monotonic() + 0.02):
                acquire_shared_file_lock(reader)
        fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
        with query_budget(time.monotonic() + 1):
            acquire_shared_file_lock(reader)


def test_deadline_survives_hycube_fallback_catches(tmp_path, monkeypatch, clock):
    from hycube_evidence import HycubeBatteryCapacitySeries
    import hycube_evidence
    source = tmp_path / "2026-09-11.jsonl"
    source.write_text("{}\n")
    # Enter the real raw-source read, then expire before its line projection.
    original = Path.open
    def expired_open(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == source: clock.now = 60
        return result
    monkeypatch.setattr(Path, "open", expired_open)
    with pytest.raises(ResearchQueryError) as exc:
        with query_budget(60):
            HycubeBatteryCapacitySeries(tmp_path).query(
                timestamp_from="2026-09-11T00:00:00+00:00",
                timestamp_to="2026-09-12T00:00:00+00:00")
    assert exc.value.code == "timeout"


def test_alarm_snapshot_uses_open_descriptor_if_path_is_replaced(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    rows = [{"timestamp": i + 1, "type": "alarm_cleared", "code": "1:test"} for i in range(4)]
    write_jsonl(path, rows)
    write_jsonl(replacement, rows[:1])
    expected = TechnicalEventSource(path).read()
    original = Path.open
    def replaced(p, *args, **kwargs):
        handle = original(p, *args, **kwargs)
        if p == path and args and args[0] == "rb":
            replacement.replace(path)
        return handle
    monkeypatch.setattr(Path, "open", replaced)
    assert TechnicalEventSource(path).read(check=lambda: None) == expected


def test_alarm_snapshot_rejects_premature_eof(tmp_path, monkeypatch):
    from timeline import TechnicalHistoryError
    path = tmp_path / "events.jsonl"
    write_jsonl(path, [{"timestamp": i + 1, "type": "alarm_cleared", "code": "1:test"} for i in range(2)])
    original = Path.open
    class EarlyEof:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def fileno(self): return self.handle.fileno()
        def readline(self, limit): return b""
    def truncated(p, *args, **kwargs):
        handle = original(p, *args, **kwargs)
        return EarlyEof(handle) if p == path and args and args[0] == "rb" else handle
    monkeypatch.setattr(Path, "open", truncated)
    with pytest.raises(TechnicalHistoryError, match="changed during read"):
        TechnicalEventSource(path).read(check=lambda: None)


def test_audit_failure_still_releases_both_gate_slots():
    class BrokenAudit(list):
        def append(self, _): raise RuntimeError("audit unavailable")
    gate = QueryGate()
    gate.audit = BrokenAudit()
    with pytest.raises(RuntimeError, match="audit unavailable"):
        gate.run("test", True, lambda _: {"data": {}})
    assert gate.active == 0
    assert gate.raw.acquire(False)
    gate.raw.release()
    assert gate.all.acquire(False) and gate.all.acquire(False)
    gate.all.release(); gate.all.release()
