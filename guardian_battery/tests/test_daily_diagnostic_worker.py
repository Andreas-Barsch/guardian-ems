import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from daily_diagnostics import (DailyDiagnosticBusyError, DailyDiagnosticSources,
                               SourceChangedError)
import daily_diagnostic_worker as worker_module
from daily_diagnostic_worker import DailyDiagnosticWorker


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class Harness:
    def __init__(self, fingerprints=None):
        self.fingerprints = fingerprints or {}
        self.probes = []
        self.runs = []
        self.active = 0
        self.maximum_active = 0
        self.result_status = "complete"
        self.run_error = None

    def probe(self, diagnostic_date, *args, **kwargs):
        self.probes.append(diagnostic_date)
        value = self.fingerprints.get(diagnostic_date, f"fp-{diagnostic_date}")
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(input_fingerprint=value)

    def run(self, diagnostic_date, sources, output_root, **kwargs):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.runs.append(diagnostic_date)
        try:
            if self.run_error:
                raise self.run_error
            return {"diagnostic_date": diagnostic_date,
                    "overall_status": self.result_status,
                    "persisted": self.result_status in {"complete", "partial"}}
        finally:
            self.active -= 1


NOW = datetime(2026, 9, 2, 0, 20, tzinfo=ZoneInfo("Europe/Berlin"))


def worker(tmp_path, harness=None, *, now=NOW, **kwargs):
    harness = harness or Harness()
    kwargs.setdefault("automatic_history_days", 1)
    kwargs.setdefault("initial_catchup_days", 1)
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics",
        clock=Clock(now), run_callable=harness.run,
        probe_callable=harness.probe, **kwargs)
    return instance, harness


def write_index(root, day, fingerprint="old", *, valid_result=True):
    directory = root / "daily" / day
    directory.mkdir(parents=True, exist_ok=True)
    result = directory / f"{fingerprint}.json"
    result_id = f"DDR-{day}-{fingerprint}"
    if valid_result:
        result.write_text(json.dumps({
            "schema_version": 1, "diagnostic_date": day,
            "daily_result_id": result_id, "input_fingerprint": fingerprint,
            "overall_status": "complete"}))
    (directory / "index.json").write_text(json.dumps({
        "schema_version": 1, "diagnostic_date": day,
        "daily_result_id": result_id, "input_fingerprint": fingerprint,
        "result": result.name, "overall_status": "complete"}))
    return directory / "index.json"


def management_record(timestamp, dcl=-25):
    return {"record_type": "frame", "direction": "response",
            "paired_command": 0x92, "timestamp": timestamp, "adr": 1,
            "checksum_valid": True, "frame_complete": True,
            "request_matched": True, "physical_serial": "SYNTHETIC",
            "source_frame_reference": f"ref-{timestamp}", "decoded": {
                "discharge_current_limit_a": dcl, "discharge_enable": True,
                "charge_current_limit_a": 25, "charge_enable": True,
                "charge_voltage_limit_v": 53.25,
                "discharge_voltage_limit_v": 45.0}}


def test_no_run_before_grace_and_current_day_never_considered(tmp_path):
    before = datetime(2026, 9, 2, 0, 14, tzinfo=ZoneInfo("Europe/Berlin"))
    instance, harness = worker(tmp_path, now=before)
    assert instance.check_once() == []
    assert harness.probes == ["2026-09-01"] and harness.runs == []


def test_pre_grace_observation_allows_run_at_local_0015(tmp_path):
    clock = Clock(datetime(2026, 9, 2, 0, 10,
                           tzinfo=ZoneInfo("Europe/Berlin")))
    harness = Harness()
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=clock,
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    assert instance.check_once() == []
    clock.value = datetime(2026, 9, 2, 0, 15,
                           tzinfo=ZoneInfo("Europe/Berlin"))
    assert instance.check_once()[0]["diagnostic_date"] == "2026-09-01"


def test_stability_requires_two_identical_observations_then_exactly_one_run(tmp_path):
    instance, harness = worker(tmp_path)
    assert instance.check_once() == []
    assert harness.runs == []
    result = instance.check_once()
    assert len(result) == 1 and harness.runs == ["2026-09-01"]
    instance.check_once()
    assert harness.runs == ["2026-09-01"]


def test_fingerprint_change_resets_stability(tmp_path):
    harness = Harness({"2026-09-01": "one"})
    instance, _ = worker(tmp_path, harness)
    instance.check_once()
    harness.fingerprints["2026-09-01"] = "two"
    instance.check_once()
    assert harness.runs == []
    instance.check_once()
    assert harness.runs == ["2026-09-01"]


def test_two_pre_grace_observations_run_at_0015_without_new_stability_requirement(tmp_path):
    clock = Clock(datetime(2026, 9, 2, 0, 5,
                           tzinfo=ZoneInfo("Europe/Berlin")))
    harness = Harness()
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=clock,
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    instance.check_once()
    clock.value += timedelta(minutes=5)
    instance.check_once()
    assert harness.runs == []
    assert instance.state["stability_candidates"]["2026-09-01"]["observations"] == 2
    clock.value += timedelta(minutes=5)
    instance.check_once()
    assert harness.runs == ["2026-09-01"]


def test_changed_fingerprint_at_0015_runs_after_matching_0020_observation(tmp_path):
    clock = Clock(datetime(2026, 9, 2, 0, 10,
                           tzinfo=ZoneInfo("Europe/Berlin")))
    harness = Harness({"2026-09-01": "A"})
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=clock,
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    instance.check_once()
    clock.value += timedelta(minutes=5)
    harness.fingerprints["2026-09-01"] = "B"
    instance.check_once()
    assert harness.runs == []
    clock.value += timedelta(minutes=5)
    instance.check_once()
    assert harness.runs == ["2026-09-01"]


@pytest.mark.parametrize("day,now,eligible", [
    ("2026-03-29", datetime(2026, 3, 30, 0, 14,
                            tzinfo=ZoneInfo("Europe/Berlin")), False),
    ("2026-03-29", datetime(2026, 3, 30, 0, 15,
                            tzinfo=ZoneInfo("Europe/Berlin")), True),
    ("2026-10-25", datetime(2026, 10, 26, 0, 14,
                            tzinfo=ZoneInfo("Europe/Berlin")), False),
    ("2026-10-25", datetime(2026, 10, 26, 0, 15,
                            tzinfo=ZoneInfo("Europe/Berlin")), True),
])
def test_dst_grace_is_local_midnight_plus_15_minutes(tmp_path, day, now, eligible):
    instance, harness = worker(tmp_path, now=now)
    instance.check_once()
    assert day in harness.probes
    instance.check_once()
    assert (day in harness.runs) is eligible


def test_start_stop_and_wait_is_interruptible(tmp_path):
    instance, _ = worker(tmp_path, check_interval_seconds=3600)
    assert instance.start() is True
    assert instance.start() is False
    started = time.monotonic()
    assert instance.stop(timeout=1) is True
    assert time.monotonic() - started < 1


def test_startup_log_contains_release_acceptance_parameters(tmp_path, caplog):
    harness = Harness()
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=Clock(NOW),
        run_callable=harness.run, probe_callable=harness.probe)
    with caplog.at_level(logging.INFO):
        instance.start()
        time.sleep(0.03)
        instance.stop(timeout=1)
    assert "Daily diagnostics worker started" in caplog.text
    for value in ("output_root=", "timezone=Europe/Berlin", "grace_minutes=15",
                  "check_interval_seconds=300", "initial_catchup_days=3",
                  "automatic_history_days=7", "late_data_days=3"):
        assert value in caplog.text
    assert "Daily diagnostics catch-up candidates=" in caplog.text
    assert "initial_plan=" in caplog.text and "remaining_backlog=" in caplog.text


def test_candidate_stability_change_and_unchanged_logs_are_bounded(tmp_path, caplog):
    harness = Harness({"2026-09-01": "first-fingerprint"})
    instance, _ = worker(tmp_path, harness)
    with caplog.at_level(logging.INFO):
        instance.check_once()
        harness.fingerprints["2026-09-01"] = "second-fingerprint"
        instance.check_once()
        instance.check_once()
    assert "first_observation" in caplog.text
    assert "fingerprint changed" in caplog.text
    assert "stable fingerprint=" in caplog.text

    root = tmp_path / "current"
    current_harness = Harness({"2026-09-01": "same"})
    current = DailyDiagnosticWorker(
        DailyDiagnosticSources(), root, clock=Clock(NOW),
        run_callable=current_harness.run, probe_callable=current_harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    write_index(root, "2026-09-01", "same")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        current.check_once()
        current.check_once()
    assert caplog.text.count("Daily diagnostics unchanged") == 1


def test_run_log_contains_attempt_result_duration_fingerprint_and_component(tmp_path,
                                                                            caplog):
    harness = Harness()
    def rich_run(diagnostic_date, sources, output_root, **kwargs):
        return {"diagnostic_date": diagnostic_date, "daily_result_id": "DDR-result",
                "input_fingerprint": "1234567890abcdef", "overall_status": "partial",
                "persisted": True, "components": {"bms_management": {
                    "component_name": "bms_management", "status": "partial",
                    "events": {"count": 7}, "quality": "limited",
                    "coverage": {"rs485_records": 10}}}}
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=Clock(NOW),
        run_callable=rich_run, probe_callable=harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    with caplog.at_level(logging.INFO):
        instance.check_once()
        instance.check_once()
    for value in ("Daily diagnostics started date=2026-09-01 attempt_id=",
                  "result_id=DDR-result", "status=partial", "duration_seconds=",
                  "fingerprint=1234567890ab", "component=bms_management",
                  "component_status=partial", "events=7", "quality=limited"):
        assert value in caplog.text


def test_missing_output_root_is_lazy_and_write_failure_isolated(tmp_path, monkeypatch,
                                                                caplog):
    instance, _ = worker(tmp_path)
    assert not instance.output_root.exists()
    instance.check_once()
    assert instance.state_path.exists()

    failed, _ = worker(tmp_path / "failed", check_interval_seconds=3600)
    monkeypatch.setattr(worker_module, "_atomic_json",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            PermissionError("read-only output")))
    with caplog.at_level(logging.ERROR):
        failed.start()
        time.sleep(0.03)
        assert failed.thread.is_alive()
        failed.stop(timeout=1)
    assert "worker state write failed" in caplog.text


def test_initial_catchup_yesterday_first_max_three_then_one_per_cycle(tmp_path):
    harness = Harness()
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=Clock(NOW),
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=6, initial_catchup_days=3)
    instance.check_once()  # first stability observation
    instance.check_once()  # initial bounded batch
    assert harness.runs == ["2026-09-01", "2026-08-31", "2026-08-30"]
    assert harness.maximum_active == 1
    instance.check_once()
    assert harness.runs[-1] == "2026-08-27"
    assert len(harness.runs) == 4


def test_stale_yesterday_has_priority_over_missing_backlog(tmp_path):
    harness = Harness({"2026-09-01": "new-yesterday"})
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=Clock(NOW),
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=5, initial_catchup_days=3)
    write_index(instance.output_root, "2026-09-01", "old-yesterday")
    instance.check_once()
    instance.check_once()
    assert harness.runs[0] == "2026-09-01"


def test_first_deployment_never_discovers_beyond_bounded_horizon(tmp_path):
    instance, harness = worker(tmp_path, automatic_history_days=7)
    instance.check_once()
    assert len(set(harness.probes)) == 7
    assert "2026-08-25" not in harness.probes


def test_sixty_date_named_history_files_only_read_bounded_catchup_plus_risk_horizon(
        tmp_path, monkeypatch):
    rs485 = tmp_path / "rs485"
    cells = tmp_path / "cells"
    rs485.mkdir()
    cells.mkdir()
    today = NOW.date()
    for age in range(1, 61):
        day = today - timedelta(days=age)
        timestamp = datetime(day.year, day.month, day.day, 12,
                             tzinfo=timezone.utc).timestamp()
        (rs485 / f"{day.isoformat()}.jsonl").write_text(
            json.dumps(management_record(timestamp)) + "\n")
        (cells / f"{day.isoformat()}.jsonl").write_text("")
    read_paths = []
    original = Path.read_bytes
    def observed_read(path):
        read_paths.append(path)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", observed_read)
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(cell_history_root=cells,
                               rs485_history_root=rs485),
        tmp_path / "diagnostics", clock=Clock(NOW),
        automatic_history_days=7)
    instance.check_once()
    assert read_paths
    # Seven catch-up candidates each use the bounded 14-day Cell Risk window:
    # oldest candidate age 7 plus 13 preceding local days and one possible UTC
    # boundary filename = 21, never all 60 days.
    assert all((today - datetime.strptime(path.stem[:10], "%Y-%m-%d").date()).days <= 21
               for path in read_paths)
    assert not any(path.stem == "2026-07-04" for path in read_paths)


def test_identical_indexed_input_is_skipped(tmp_path):
    harness = Harness({"2026-09-01": "same"})
    instance, _ = worker(tmp_path, harness)
    write_index(instance.output_root, "2026-09-01", "same")
    instance.check_once()
    instance.check_once()
    assert harness.runs == []
    assert instance.state["stale_dates"] == []


@pytest.mark.parametrize("damage", ["corrupt", "result_id", "fingerprint", "date",
                                    "path_traversal"])
def test_invalid_index_or_revision_is_not_factual_truth(tmp_path, damage):
    harness = Harness({"2026-09-01": "new"})
    instance, _ = worker(tmp_path, harness)
    index_path = write_index(instance.output_root, "2026-09-01", "old")
    index = json.loads(index_path.read_text())
    result_path = index_path.parent / index["result"]
    if damage == "path_traversal":
        index = json.loads(index_path.read_text())
        index["result"] = "../outside.json"
        index_path.write_text(json.dumps(index))
    elif damage == "corrupt":
        result_path.write_text("{broken")
    else:
        result = json.loads(result_path.read_text())
        field = {"result_id": "daily_result_id", "fingerprint": "input_fingerprint",
                 "date": "diagnostic_date"}[damage]
        result[field] = "mismatch"
        result_path.write_text(json.dumps(result))
    assert instance._index("2026-09-01") is None
    instance.check_once()
    instance.check_once()
    assert harness.runs == ["2026-09-01"]


def test_stop_event_prevents_a_new_analysis_from_starting(tmp_path):
    instance, harness = worker(tmp_path)
    instance.check_once()
    instance.stop_event.set()
    assert instance.check_once() == []
    assert harness.runs == []


def test_late_data_marks_stale_then_creates_sequential_revision(tmp_path):
    harness = Harness({"2026-09-01": "new"})
    instance, _ = worker(tmp_path, harness)
    old_index = write_index(instance.output_root, "2026-09-01", "old")
    old_bytes = old_index.read_bytes()
    instance.check_once()
    assert instance.state["stale_dates"] == ["2026-09-01"]
    instance.check_once()
    assert harness.runs == ["2026-09-01"]
    # The harness deliberately does not touch persisted truth.
    assert old_index.read_bytes() == old_bytes


def test_outside_day_physical_change_with_same_semantic_fingerprint_not_stale(tmp_path):
    harness = Harness({"2026-09-01": "same-semantic"})
    instance, _ = worker(tmp_path, harness)
    write_index(instance.output_root, "2026-09-01", "same-semantic")
    instance.check_once()
    assert instance.state["stale_dates"] == [] and harness.runs == []


def test_real_core_late_data_revision_and_outside_day_growth(tmp_path):
    rs485 = tmp_path / "rs485"
    cells = tmp_path / "cells"
    rs485.mkdir()
    cells.mkdir()
    path = rs485 / "utc-history.jsonl"
    first = datetime(2026, 9, 1, 10, tzinfo=timezone.utc).timestamp()
    path.write_text(json.dumps(management_record(first)) + "\n")
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(cell_history_root=cells,
                               rs485_history_root=rs485),
        tmp_path / "diagnostics", clock=Clock(NOW),
        automatic_history_days=1, initial_catchup_days=1)
    instance.check_once()
    assert instance.check_once()[0]["overall_status"] == "partial"
    index_path = instance.output_root / "daily/2026-09-01/index.json"
    initial = json.loads(index_path.read_text())

    outside = datetime(2026, 9, 2, 10, tzinfo=timezone.utc).timestamp()
    with path.open("a") as handle:
        handle.write(json.dumps(management_record(outside)) + "\n")
    assert instance.check_once() == []
    assert json.loads(index_path.read_text())["input_fingerprint"] == initial["input_fingerprint"]
    assert instance.state["stale_dates"] == []

    with path.open("a") as handle:
        handle.write(json.dumps(management_record(first + 60, 0)) + "\n")
    assert instance.check_once() == []
    assert instance.state["stale_dates"] == ["2026-09-01"]
    assert instance.check_once()[0]["persisted"] is True
    revised = json.loads(index_path.read_text())
    assert revised["input_fingerprint"] != initial["input_fingerprint"]
    assert len(list(index_path.parent.glob("*.json"))) == 3  # two revisions plus index


def test_old_index_outside_late_window_is_not_repeatedly_probed(tmp_path):
    harness = Harness()
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), tmp_path / "diagnostics", clock=Clock(NOW),
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=5, late_data_days=2)
    for age in range(1, 6):
        day = (NOW.date() - timedelta(days=age)).isoformat()
        write_index(instance.output_root, day, f"fp-{day}")
    instance.check_once()
    assert set(harness.probes) == {"2026-09-01", "2026-08-31"}


@pytest.mark.parametrize("error,kind", [
    (SourceChangedError("moving"), "source_changed"),
    (DailyDiagnosticBusyError("busy"), "lock_busy"),
])
def test_transient_run_error_retries_without_killing_worker(tmp_path, error, kind):
    harness = Harness()
    instance, _ = worker(tmp_path, harness)
    instance.check_once()
    harness.run_error = error
    instance.check_once()
    assert instance.state["last_error"]["kind"] == kind
    harness.run_error = None
    instance.check_once()  # new first observation after transient snapshot failure
    instance.check_once()
    assert len(harness.runs) == 2


def test_failed_rerun_keeps_old_index_and_worker_state_records_failure(tmp_path):
    harness = Harness({"2026-09-01": "new"})
    harness.result_status = "failed"
    instance, _ = worker(tmp_path, harness)
    index = write_index(instance.output_root, "2026-09-01", "old")
    before = index.read_bytes()
    instance.check_once()
    instance.check_once()
    assert instance.state["last_error"]["kind"] == "run_failed"
    assert index.read_bytes() == before


def test_probe_source_change_is_retried_from_first_stability_observation(tmp_path):
    harness = Harness({"2026-09-01": SourceChangedError("moving")})
    instance, _ = worker(tmp_path, harness)
    instance.check_once()
    assert instance.state["last_error"]["kind"] == "source_changed"
    harness.fingerprints["2026-09-01"] = "stable"
    instance.check_once()
    assert harness.runs == []
    instance.check_once()
    assert harness.runs == ["2026-09-01"]


def test_outer_worker_exception_isolated_and_thread_survives(tmp_path):
    harness = Harness({"2026-09-01": RuntimeError("unexpected")})
    instance, _ = worker(tmp_path, harness, check_interval_seconds=3600)
    instance.start()
    time.sleep(0.05)
    assert instance.thread.is_alive()
    assert instance.state["last_error"]["kind"] == "probe_failure"
    assert instance.stop(timeout=1)


def test_running_state_is_recovered_without_deleting_valid_result(tmp_path):
    root = tmp_path / "diagnostics"
    index = write_index(root, "2026-09-01", "valid")
    state = {**DailyDiagnosticWorker._new_state(),
             "currently_running_date": "2026-09-01",
             "currently_running_attempt": "attempt-old"}
    path = root / "state/daily_job_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state))
    instance = DailyDiagnosticWorker(DailyDiagnosticSources(), root,
                                     clock=Clock(NOW), automatic_history_days=1)
    assert instance.state["currently_running_date"] is None
    assert instance.state["last_error"]["kind"] == "interrupted"
    assert index.exists()


def test_corrupt_state_reconstructs_safely_from_result_index(tmp_path, caplog):
    root = tmp_path / "diagnostics"
    write_index(root, "2026-09-01", "same")
    state = root / "state/daily_job_state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{broken")
    harness = Harness({"2026-09-01": "same"})
    with caplog.at_level(logging.WARNING):
        instance = DailyDiagnosticWorker(
            DailyDiagnosticSources(), root, clock=Clock(NOW),
            run_callable=harness.run, probe_callable=harness.probe,
            automatic_history_days=1)
    instance.check_once()
    assert harness.runs == []
    assert "state corrupt" in caplog.text


def test_state_write_failure_after_valid_result_is_recovered_from_index(tmp_path,
                                                                        monkeypatch):
    harness = Harness({"2026-09-01": "stable"})
    root = tmp_path / "diagnostics"
    def run_and_publish(diagnostic_date, sources, output_root, **kwargs):
        write_index(Path(output_root), diagnostic_date, "stable")
        harness.runs.append(diagnostic_date)
        return {"diagnostic_date": diagnostic_date, "overall_status": "complete",
                "persisted": True}
    instance = DailyDiagnosticWorker(
        DailyDiagnosticSources(), root, clock=Clock(NOW),
        run_callable=run_and_publish, probe_callable=harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    original = instance._save_state
    calls = 0
    def failing_state_write():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk full during state write")
        original()
    monkeypatch.setattr(instance, "_save_state", failing_state_write)
    instance.check_once()
    with pytest.raises(OSError, match="disk full"):
        instance.check_once()
    restarted = DailyDiagnosticWorker(
        DailyDiagnosticSources(), root, clock=Clock(NOW),
        run_callable=harness.run, probe_callable=harness.probe,
        automatic_history_days=1, initial_catchup_days=1)
    restarted.check_once()
    assert harness.runs == ["2026-09-01"]


def test_future_and_current_candidates_are_removed_and_never_run(tmp_path):
    instance, harness = worker(tmp_path)
    instance.state["stability_candidates"] = {
        "2026-09-02": {"fingerprint": "current"},
        "2026-09-03": {"fingerprint": "future"},
    }
    instance.state["stale_dates"] = ["2026-09-03"]
    instance.check_once()
    assert "2026-09-02" not in instance.state["stability_candidates"]
    assert "2026-09-03" not in instance.state["stability_candidates"]
    assert instance.state["stale_dates"] == []
    assert all(day < "2026-09-02" for day in harness.probes + harness.runs)


def test_partial_result_is_successful_but_visible_in_state(tmp_path):
    harness = Harness()
    harness.result_status = "partial"
    instance, _ = worker(tmp_path, harness)
    instance.check_once()
    result = instance.check_once()[0]
    assert result["overall_status"] == "partial"
    assert instance.state["last_successful_date"] == "2026-09-01"
    assert instance.state["last_result_status"] == "partial"
    assert instance.state["last_partial_date"] == "2026-09-01"


def test_worker_source_has_no_transport_or_live_diagnostic_calls():
    source = Path(sys.modules[DailyDiagnosticWorker.__module__].__file__).read_text()
    for forbidden in ("serial.write", "console.command", "mqtt", "Hycube",
                      "cell_store.analyse", "publish("):
        assert forbidden not in source


def test_main_lifecycle_wiring_starts_after_live_initialization_and_stops_first():
    source = (Path(__file__).resolve().parents[1] / "app/main.py").read_text()
    worker_start = source.index("daily_worker.start()")
    assert source.index("publisher = Mqtt(options)") < worker_start
    assert source.index("console = PylontechConsole(") < worker_start
    assert source.index("rs485_writer.start()") < worker_start
    assert source.index("rs485_reader.start()") < worker_start
    assert worker_start < source.index("while RUNNING:")
    worker_stop = source.index("daily_worker.stop()")
    assert source.index("finally:", source.index("while RUNNING:")) < worker_stop
    assert worker_stop < source.index("rs485_reader.stop()")
    assert worker_stop < source.index("publisher.close()")
    shutdown = source[worker_stop:source.index("rs485_reader.stop()")]
    assert "except Exception as exc:" in shutdown


def test_main_wiring_passes_existing_sources_and_contains_no_daily_domain_logic():
    source = (Path(__file__).resolve().parents[1] / "app/main.py").read_text()
    block = source[source.index("daily_worker = DailyDiagnosticWorker("):
                   source.index("daily_worker.start()")]
    for name in ("CELL_HISTORY_DIR", "DEFAULT_RS485_HISTORY_DIR",
                 "DEFAULT_POSITION_HISTORY_FILE", "CONFIG_HISTORY_FILE",
                 "DEFAULT_MAINTENANCE_EVENT_FILE", "DAILY_DIAGNOSTICS_ROOT"):
        assert name in block
    assert 'DAILY_DIAGNOSTICS_ROOT = SHARE_DIR / "diagnostics"' in source
    assert "BmsManagementEvidenceAnalyzer" not in source
    assert "probe_daily_inputs" not in source


def test_main_worker_start_failure_isolated_from_poll_loop():
    source = (Path(__file__).resolve().parents[1] / "app/main.py").read_text()
    start = source.index("try:\n        daily_worker = DailyDiagnosticWorker(")
    poll = source.index("while RUNNING:")
    block = source[start:poll]
    assert "except Exception as exc:" in block
    assert "daily_worker = None" in block
    assert "konnte nicht gestartet werden" in block
