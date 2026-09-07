import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cell_diagnostics import CellDiagnosticStore, CellSample
from cell_history import CellHistoryWriter
from collector_timing import CollectorTiming, PeriodicDeadline
from derived_persistence import DerivedPersistenceWorker
from diagnostic_aggregates import DiagnosticAggregateStore
from position_history import (DocumentedIdentityResolver, PositionHistoryLog,
                              PositionSnapshot)


class Clock:
    def __init__(self, value=0.0):
        self.value = float(value)

    def __call__(self):
        return self.value


def test_deadline_recovers_after_overrun_without_catchup_storm():
    clock = Clock()
    deadline = PeriodicDeadline(10, clock=clock)
    assert deadline.due()
    assert deadline.consume() == 0
    assert deadline.next_deadline == 10

    clock.value = 12
    assert deadline.due()
    assert deadline.consume() == 0
    assert deadline.next_deadline == 20
    assert deadline.delay() == 8

    clock.value = 55
    assert deadline.consume() == 3
    assert deadline.next_deadline == 60
    assert deadline.delay() == 5


def test_end_of_cycle_skip_selects_a_future_deadline():
    clock = Clock()
    deadline = PeriodicDeadline(10, clock=clock)
    deadline.consume()  # cycle starts at zero; next nominal slot is 10
    clock.value = 12
    assert deadline.due()
    deadline.consume()  # end-of-cycle recovery used by main
    assert deadline.next_deadline == 20
    assert deadline.delay() == 8


def test_cell_deadline_is_start_to_start_and_recovers_after_timeout():
    clock = Clock(100)
    deadline = PeriodicDeadline(60, clock=clock)
    deadline.consume(100)
    assert deadline.next_deadline == 160

    # A long current round does not turn the contract into end + 60 seconds.
    clock.value = 171
    assert deadline.due()
    deadline.consume()
    assert deadline.next_deadline == 220
    assert deadline.delay() == 49


def test_multiple_timeouts_skip_missed_slots_without_back_to_back_cell_rounds():
    clock = Clock(0)
    deadline = PeriodicDeadline(60, clock=clock)
    deadline.consume()
    clock.value = 190  # more than three nominal slots after the first start
    assert deadline.consume() == 2
    assert deadline.next_deadline == 240
    assert deadline.delay() == 50
    assert not deadline.due(190.001)


def test_timing_state_is_bounded_and_counts_overruns():
    timing = CollectorTiming(10, 60, rolling_size=3)
    timing.cycle_started(1000, 0)
    timing.duration("bat_request", 0.4)
    timing.duration("bat_request", 0.5)
    timing.duration("bat_request", 5.0)
    timing.duration("bat_request", 0.6)
    timing.cycle_finished(12.5)
    timing.cell_started(1000, 0)
    timing.cell_started(1072, 72)
    timing.cell_finished(8)
    timing.cell_samples((("SERIAL-1", 1000), ("SERIAL-2", 1003)))
    timing.cell_samples((("SERIAL-1", 1062), ("SERIAL-2", 1066)))

    state = timing.snapshot()
    assert state["cycle_overrun_count"] == 1
    assert state["cycle_overrun_max_seconds"] == pytest.approx(2.5)
    assert state["cell_overrun_count"] == 1
    assert state["cell_overrun_max_seconds"] == pytest.approx(12)
    assert state["cell_stack_sample_spread_seconds"] == pytest.approx(4)
    assert state["effective_cell_intervals_by_serial"]["SERIAL-1"][
        "last_seconds"] == pytest.approx(62)
    assert state["rolling"]["bat_request"] == {
        "count": 3, "median_seconds": 0.6, "max_seconds": 5.0}


def sample(timestamp=1.0):
    return CellSample(timestamp, 1, [3300] * 15, 0.0, 50.0,
                      [25.0] * 15, [False] * 15, "SERIAL-1", None)


def test_background_persistence_preserves_restart_recovery(tmp_path):
    cells = CellDiagnosticStore(tmp_path / "cells.json")
    aggregates = DiagnosticAggregateStore(
        tmp_path / "aggregates.json", CellDiagnosticStore.phases)
    cells.add(sample())
    options = {
        "cell_diag_low_soc_percent": 30,
        "cell_diag_high_soc_percent": 80,
        "cell_diag_charge_current_a": 0.8,
        "cell_diag_discharge_current_a": -0.8,
    }
    aggregates.add(sample(), options)

    worker = DerivedPersistenceWorker(cells, aggregates)
    worker.start()
    worker.submit(cells.persistence_payload(), aggregates.persistence_payload())
    assert worker.stop(timeout=5)
    assert worker.status()["persisted"] == 1

    restored = CellDiagnosticStore(cells.path)
    assert len(restored.identity_samples["SERIAL-1"]) == 1
    assert json.loads(aggregates.path.read_text())["records"]


def test_worker_coalesces_only_derived_generations():
    entered = threading.Event()
    release = threading.Event()

    class Cells:
        def __init__(self):
            self.values = []

        def persist_payload(self, value):
            if not self.values:
                entered.set()
                release.wait(2)
            self.values.append(value)

    class Aggregates:
        def persist_payload(self, value):
            pass

    cells = Cells()
    worker = DerivedPersistenceWorker(cells, Aggregates())
    worker.start()
    worker.submit(1, None)
    assert entered.wait(1)
    worker.submit(2, None)
    worker.submit(3, None)
    release.set()
    assert worker.stop(timeout=5)
    assert cells.values == [1, 3]
    assert worker.status()["coalesced"] == 1


def test_slow_derived_writer_does_not_block_next_raw_append(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowCells:
        def persist_payload(self, value):
            entered.set()
            release.wait(2)

    class Aggregates:
        def persist_payload(self, value):
            pass

    history = CellHistoryWriter(tmp_path / "history")
    worker = DerivedPersistenceWorker(SlowCells(), Aggregates())
    worker.start()
    worker.submit({}, None)
    assert entered.wait(1)
    history.append({"timestamp": 1.0, "module": 1, "module_serial": "SERIAL-1"})
    history.append({"timestamp": 2.0, "module": 2, "module_serial": "SERIAL-2"})
    records = (tmp_path / "history" / "1970-01-01.jsonl").read_text().splitlines()
    assert len(records) == 2
    release.set()
    assert worker.stop(5)


def test_worker_exception_is_visible_and_worker_accepts_later_generation():
    class Cells:
        def __init__(self):
            self.calls = 0

        def persist_payload(self, value):
            self.calls += 1
            if self.calls == 1:
                raise OSError("synthetic failure")

    class Aggregates:
        def persist_payload(self, value):
            pass

    cells = Cells()
    worker = DerivedPersistenceWorker(cells, Aggregates())
    worker.start()
    worker.submit(1, None)
    for _ in range(100):
        if worker.status()["last_error"]:
            break
        threading.Event().wait(0.005)
    assert "synthetic failure" in worker.status()["last_error"]
    worker.submit(2, None)
    assert worker.stop(5)
    assert cells.calls == 2
    assert worker.status()["persisted"] == 1


def test_position_history_is_loaded_once_for_six_time_correct_lookups(
        tmp_path, monkeypatch):
    path = tmp_path / "positions.jsonl"
    before = PositionSnapshot(
        1, "PHS-11111111-1111-4111-8111-111111111111",
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        "MEV-before", {str(i): f"OLD-{i}" for i in range(1, 7)})
    after = PositionSnapshot(
        1, "PHS-22222222-2222-4222-8222-222222222222",
        "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
        "MEV-after", {str(i): f"NEW-{i}" for i in range(1, 7)})
    log = PositionHistoryLog(path)
    log.append(before)
    log.append(after)
    calls = 0
    original = PositionHistoryLog.read_all

    def counted(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(PositionHistoryLog, "read_all", counted)
    resolver = DocumentedIdentityResolver.from_path(path)
    old_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    new_time = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    assert [resolver.identity_at(i, old_time)[0] for i in range(1, 7)] == [
        f"OLD-{i}" for i in range(1, 7)]
    assert resolver.identity_at(6, new_time)[0] == "NEW-6"
    assert calls == 1


def test_main_keeps_console_single_owner_and_raw_history_before_derived_state():
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
        encoding="utf-8")
    assert "threading.Thread" not in source
    history = source.index("acquired_samples = acquire_cell_round(")
    derived = source.index("for sample in acquired_samples:")
    assert history < derived
    assert "cell_deadline.due()" in source
    assert "poll_deadline.delay()" in source
    assert "time.sleep(max(1" not in source
