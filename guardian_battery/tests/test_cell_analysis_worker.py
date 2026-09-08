from __future__ import annotations

import threading
import time

from cell_analysis_worker import (CellAnalysisWorker, analyse_cell_snapshot,
                                  build_analysis_snapshot,
                                  project_latest_analysis)
from cell_diagnostics import CellDiagnosticStore, CellSample
from diagnostic_aggregates import DiagnosticAggregateStore
from collector_timing import PeriodicDeadline


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_worker_publishes_generation_and_provenance():
    worker = CellAnalysisWorker(lambda value: ({1: value}, [{"module": 1}], {"n": 1}))
    worker.start()
    worker.submit(1, 123.0, "result")
    assert wait_for(lambda: worker.latest() is not None)
    latest = worker.latest()
    assert latest["generation"] == 1
    assert latest["source_sample_at"] == 123.0
    assert latest["results"] == {1: "result"}
    assert latest["created_at"] is not None
    assert latest["analyzed_at"] >= 123.0
    assert latest["success"] is True
    assert worker.stop()


def test_worker_has_one_active_and_only_latest_pending_generation():
    entered = threading.Event()
    release = threading.Event()

    def analyze(value):
        if value == 1:
            entered.set()
            release.wait(1)
        return ({1: value}, [], {})

    worker = CellAnalysisWorker(analyze)
    worker.start()
    worker.submit(1, 1, 1)
    assert entered.wait(1)
    worker.submit(2, 2, 2)
    worker.submit(3, 3, 3)
    assert worker.status()["pending"] is True
    assert worker.status()["coalesced_count"] == 1
    release.set()
    assert wait_for(lambda: worker.latest() and worker.latest()["generation"] == 3)
    assert worker.latest()["results"] == {1: 3}
    assert worker.stop()


def test_sixty_second_acquisition_schedule_is_independent_of_very_slow_worker():
    entered = threading.Event()
    release = threading.Event()
    processed = []

    def analyze(generation):
        processed.append(generation)
        if generation == 1:
            entered.set()
            release.wait(1)
        return ({}, [], {})

    clock = [0.0]
    deadline = PeriodicDeadline(60, clock=lambda: clock[0])
    worker = CellAnalysisWorker(analyze)
    worker.start()
    starts = []
    for generation, second in enumerate((0, 60, 120, 180), 1):
        clock[0] = second
        assert deadline.due()
        starts.append(second)
        deadline.consume()
        worker.submit(generation, second, generation)
        if generation == 1:
            assert entered.wait(1)
    assert starts == [0, 60, 120, 180]
    assert worker.status()["coalesced_count"] == 2
    release.set()
    assert wait_for(lambda: worker.latest() and worker.latest()["generation"] == 4)
    assert processed == [1, 4]
    assert worker.stop()


def test_failure_is_isolated_and_later_generation_recovers():
    def analyze(value):
        if value == "fail":
            raise RuntimeError("broken")
        return ({1: value}, [], {})

    worker = CellAnalysisWorker(analyze)
    worker.start()
    worker.submit(1, 1, "fail")
    assert wait_for(lambda: worker.status()["failure_count"] == 1)
    assert worker.latest() is None
    worker.submit(2, 2, "ok")
    assert wait_for(lambda: worker.latest() is not None)
    assert worker.latest()["generation"] == 2
    assert worker.status()["last_error"] is None
    assert worker.stop()


def test_stop_is_bounded_while_analysis_is_stuck():
    entered = threading.Event()
    release = threading.Event()

    def analyze(_value):
        entered.set()
        release.wait(1)
        return ({}, [], {})

    worker = CellAnalysisWorker(analyze)
    worker.start()
    worker.submit(1, 1, None)
    assert entered.wait(1)
    assert worker.stop(timeout=0.01) is False
    release.set()
    assert worker.stop(timeout=1) is True
    assert worker.submit(2, 2, None) is False


def test_shutdown_discards_pending_rebuildable_generation():
    entered = threading.Event()
    release = threading.Event()
    analyzed = []

    def analyze(value):
        analyzed.append(value)
        if value == 1:
            entered.set()
            release.wait(1)
        return ({}, [], {})

    worker = CellAnalysisWorker(analyze)
    worker.start()
    worker.submit(1, 1, 1)
    assert entered.wait(1)
    worker.submit(2, 2, 2)
    assert worker.stop(timeout=0.01) is False
    release.set()
    assert worker.stop(timeout=1) is True
    assert analyzed == [1]


def test_latest_returns_a_copy_of_results_mapping():
    worker = CellAnalysisWorker(lambda _value: ({1: {"status": "NORMAL"}}, [], {}))
    worker.start()
    worker.submit(1, 1, None)
    assert wait_for(lambda: worker.latest() is not None)
    result = worker.latest()
    result["results"].clear()
    assert worker.latest()["results"] == {1: {"status": "NORMAL"}}
    assert worker.stop()


def test_status_exposes_bounded_observability_contract():
    worker = CellAnalysisWorker(lambda _value: ({}, [], {}))
    worker.start()
    worker.submit(7, 10, None)
    assert wait_for(lambda: worker.status()["generation_latest"] == 7)
    status = worker.status()
    assert status["active"] is False
    assert status["pending"] is False
    assert status["generation_active"] is None
    assert status["last_duration_seconds"] >= 0
    assert status["last_completed_at"] is not None
    assert status["age_seconds"] >= 0
    assert status["coalesced_count"] == 0
    assert status["failure_count"] == 0
    assert worker.stop()


def test_profiling_clock_failure_does_not_kill_analysis():
    def broken_clock():
        raise RuntimeError("clock unavailable")

    worker = CellAnalysisWorker(
        lambda _value: ({1: "ok"}, [], {}), clock=broken_clock)
    worker.start()
    worker.submit(1, 1, None)
    assert wait_for(lambda: worker.latest() is not None)
    assert worker.latest()["results"] == {1: "ok"}
    assert worker.status()["failure_count"] == 0
    assert worker.status()["last_duration_seconds"] is None
    assert worker.stop()


def test_projection_rejects_result_for_replaced_physical_identity():
    latest = {
        "generation": 4, "source_sample_at": 90.0, "analyzed_at": 100.0,
        "results": {
            1: {"status": "NORMAL", "physical_module_serial": "OLD"},
            2: {"status": "NORMAL", "physical_module_serial": "SAME"},
        },
    }
    current = {1: "NEW", 2: "SAME"}
    projected = project_latest_analysis(latest, current.get, now=lambda: 125.0)
    assert 1 not in projected
    assert projected[2]["analysis_generation"] == 4
    assert projected[2]["analysis_age_seconds"] == 25.0


def options():
    return {
        "cell_diag_low_soc_percent": 30, "cell_diag_high_soc_percent": 80,
        "cell_diag_charge_current_a": 0.8, "cell_diag_discharge_current_a": 0.8,
        "cell_diag_min_phase_samples": 1,
        "cell_diag_confidence_medium_samples": 2,
        "cell_diag_confidence_high_samples": 3,
        "cell_diag_observe_deviation_mv": 10,
        "cell_diag_warning_deviation_mv": 20,
        "cell_diag_critical_deviation_mv": 40,
    }


def test_snapshot_result_matches_direct_analysis_and_freezes_membership(tmp_path):
    store = CellDiagnosticStore(tmp_path / "cells.json")
    sample = CellSample(10, 1, [3300] * 14 + [3320], -1.0, 50.0,
                        [25.0] * 15, [False] * 15, "SERIAL-1")
    store.add(sample)
    aggregates = DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases)
    aggregates.add(sample, options())
    snapshot = build_analysis_snapshot(
        store, aggregates, [1], options(), (), {"active": False, "pending": True})
    direct = store.analyse(1, options(), (), aggregates.for_identity(1, "SERIAL-1"))
    store.add(CellSample(11, 1, [3290] + [3300] * 14, -1.0, 50.0,
                         [25.0] * 15, [False] * 15, "SERIAL-1"))
    results, profiles, global_profile = analyse_cell_snapshot(snapshot)
    def without_runtime_timestamp(value):
        if isinstance(value, dict):
            return {key: without_runtime_timestamp(item)
                    for key, item in value.items() if key != "evaluated_at"}
        if isinstance(value, list):
            return [without_runtime_timestamp(item) for item in value]
        return value

    assert without_runtime_timestamp(results[1]) == without_runtime_timestamp(
        {**direct, "physical_module_serial": "SERIAL-1"})
    assert results[1]["sample_count"] == 1
    assert len(profiles) == 1
    assert global_profile["derived_writer_pending"] is True


def test_snapshot_contains_only_current_identity_and_freezes_context(tmp_path):
    store = CellDiagnosticStore(tmp_path / "cells.json")
    for timestamp, serial in ((1, "OLD"), (2, "CURRENT")):
        store.add(CellSample(timestamp, 1, [3300] * 15, 0, 50,
                             [25] * 15, [False] * 15, serial))
    store.add(CellSample(3, 2, [3300] * 15, 0, 50,
                         [25] * 15, [False] * 15, None))
    store.set_current_identities({1: "CURRENT"})
    aggregates = DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases)
    opts = options()
    maintenance = {"maintenance_event_id": "MEV-1", "revision": 1}
    snapshot = build_analysis_snapshot(
        store, aggregates, [1], opts, [maintenance], {})
    maintenance["revision"] = 2
    opts["cell_diag_warning_deviation_mv"] = 999
    assert snapshot["snapshot_sample_count"] == 1
    assert {sample.get("module_serial") for sample in snapshot["samples"]} == {"CURRENT"}
    assert snapshot["maintenance_events"][0]["revision"] == 1
    assert snapshot["options"]["cell_diag_warning_deviation_mv"] == 20
    assert snapshot["config_id"]
