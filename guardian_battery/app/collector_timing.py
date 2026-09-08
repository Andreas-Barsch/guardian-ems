"""Bounded timing state and drift-free deadlines for Guardian acquisition."""
from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict, deque


class ProfilingTimer:
    """Best-effort monotonic subtimings that never affect measured work."""

    def __init__(self, *, clock=time.monotonic, enabled=True):
        self.clock = clock
        self.enabled = bool(enabled)
        self.values = {}

    def start(self):
        if not self.enabled:
            return None
        try:
            return self.clock()
        except Exception:
            return None

    def finish(self, name, started):
        if started is None:
            return None
        try:
            value = max(0.0, float(self.clock() - started))
            self.values[f"{name}_seconds"] = value
            return value
        except Exception:
            return None

    def counter(self, name, value):
        try:
            if value is None or isinstance(value, (bool, int, float, str)):
                self.values[name] = value
        except Exception:
            pass

    def section(self, name, values):
        try:
            self.values[name] = dict(values)
        except Exception:
            pass

    def child(self):
        return ProfilingTimer(clock=self.clock, enabled=self.enabled)

    def snapshot(self):
        try:
            return dict(self.values)
        except Exception:
            return {}


class PeriodicDeadline:
    """A periodic deadline that skips missed slots without catch-up bursts."""

    def __init__(self, interval_seconds: float, *, clock=time.monotonic,
                 start_immediately: bool = True):
        self.interval = float(interval_seconds)
        if self.interval <= 0:
            raise ValueError("interval_seconds must be positive")
        self.clock = clock
        now = self.clock()
        self.next_deadline = now if start_immediately else now + self.interval

    def due(self, now: float | None = None) -> bool:
        return (self.clock() if now is None else float(now)) >= self.next_deadline

    def consume(self, now: float | None = None) -> int:
        """Advance to the first future slot and return missed extra slots."""
        current = self.clock() if now is None else float(now)
        if current < self.next_deadline:
            return 0
        slots = int((current - self.next_deadline) // self.interval) + 1
        self.next_deadline += slots * self.interval
        return max(0, slots - 1)

    def delay(self, now: float | None = None) -> float:
        return max(0.0, self.next_deadline -
                   (self.clock() if now is None else float(now)))


class CollectorTiming:
    """Thread-safe current/rolling metrics; no unbounded timing history."""

    def __init__(self, poll_target: float, cell_target: float, *, rolling_size=60):
        self.poll_target = float(poll_target)
        self.cell_target = float(cell_target)
        self.rolling_size = int(rolling_size)
        self._lock = threading.Lock()
        self._last_cycle_start = None
        self._last_cell_start = None
        self._last_cell_by_serial = {}
        self._current = {}
        self._last_completed_cycle = {}
        self._current_cell = None
        self._last_completed_cell = {}
        self._cycle_id = 0
        self._observability_errors = 0
        self._rolling = defaultdict(lambda: deque(maxlen=self.rolling_size))
        self._cycle_overruns = 0
        self._cycle_overrun_max = 0.0
        self._cell_overruns = 0
        self._cell_overrun_max = 0.0
        self._cell_analysis_profiles = []
        self._cell_analysis_global = {}
        self._analysis_worker = {}
        self._persistence_worker = {}

    def cycle_started(self, wall_time: float, monotonic_time: float) -> None:
        with self._lock:
            interval = (None if self._last_cycle_start is None else
                        monotonic_time - self._last_cycle_start)
            self._last_cycle_start = monotonic_time
            self._cycle_id += 1
            self._current = {"cycle_id": self._cycle_id,
                             "state": "running",
                             "cycle_started_at": float(wall_time),
                             "effective_poll_interval_seconds": interval}

    def cell_started(self, wall_time: float, monotonic_time: float,
                     planned_deadline: float | None = None) -> None:
        with self._lock:
            interval = (None if self._last_cell_start is None else
                        monotonic_time - self._last_cell_start)
            self._last_cell_start = monotonic_time
            self._current["cell_cycle_started_at"] = float(wall_time)
            self._current["effective_cell_sampling_interval_seconds"] = interval
            self._current["cell_deadline_lateness_seconds"] = max(
                0.0, monotonic_time - planned_deadline
            ) if planned_deadline is not None else None
            if interval is not None and interval > self.cell_target:
                overrun = interval - self.cell_target
                self._cell_overruns += 1
                self._cell_overrun_max = max(self._cell_overrun_max, overrun)
            self._current_cell = {
                "cycle_id": self._current.get("cycle_id"), "state": "running",
                "cell_started_at": float(wall_time),
                "actual_start_monotonic": float(monotonic_time),
                "deadline_before_start": planned_deadline,
                "deadline_lateness_seconds": self._current[
                    "cell_deadline_lateness_seconds"],
                "cell_deadline_lateness_seconds": self._current[
                    "cell_deadline_lateness_seconds"],
                "effective_cell_sampling_interval_seconds": interval,
            }

    def cell_deadline_consumed(self, next_deadline, skipped_slots):
        with self._lock:
            if self._current_cell is not None:
                self._current_cell["next_deadline_after_consume"] = float(next_deadline)
                self._current_cell["skipped_cell_slots"] = int(skipped_slots)

    def duration(self, name: str, seconds: float, *, cpu_seconds=None,
                 status="executed") -> None:
        value = max(0.0, float(seconds))
        with self._lock:
            self._current[f"{name}_duration_seconds"] = value
            self._current[f"{name}_status"] = status
            if cpu_seconds is not None:
                self._current[f"{name}_thread_cpu_seconds"] = max(
                    0.0, float(cpu_seconds))
            if self._current_cell is not None:
                self._current_cell[f"{name}_duration_seconds"] = value
                self._current_cell[f"{name}_status"] = status
                if cpu_seconds is not None:
                    self._current_cell[f"{name}_thread_cpu_seconds"] = max(
                        0.0, float(cpu_seconds))
            self._rolling[name].append(value)

    def mark_not_executed(self, name):
        with self._lock:
            self._current[f"{name}_status"] = "not_executed"
            if self._current_cell is not None:
                self._current_cell[f"{name}_status"] = "not_executed"

    def cycle_finished(self, seconds: float) -> None:
        with self._lock:
            self._current["cycle_duration_seconds"] = max(0.0, float(seconds))
            self._current["cycle_status"] = "executed"
            self._current["state"] = "completed"
            self._last_completed_cycle = dict(self._current)
            self._rolling["cycle"].append(max(0.0, float(seconds)))
            if seconds > self.poll_target:
                overrun = seconds - self.poll_target
                self._cycle_overruns += 1
                self._cycle_overrun_max = max(self._cycle_overrun_max, overrun)

    def cycle_accounting(self, total_seconds, component_names):
        with self._lock:
            accounted = sum(float(self._current.get(
                f"{name}_duration_seconds", 0.0) or 0.0) for name in component_names)
            remaining = float(total_seconds) - accounted
            self._current["remaining_other_duration_seconds"] = remaining
            self._current["remaining_other_status"] = "executed"
            if remaining < -0.001:
                self._observability_errors += 1

    def cell_finished(self, seconds: float) -> None:
        self.duration("cell_cycle", seconds)
        with self._lock:
            if self._current_cell is not None:
                self._current_cell["acquisition_duration_seconds"] = max(
                    0.0, float(seconds))
                self._current_cell["acquisition_finished_at"] = time.time()

    def cell_generation(self, generation):
        with self._lock:
            if self._current_cell is not None:
                self._current_cell["cell_generation"] = int(generation)

    def cell_post_finished(self, total_seconds, *, cpu_seconds=None,
                           finished_at=None, state="completed"):
        with self._lock:
            if self._current_cell is None:
                return
            cell = self._current_cell
            total = max(0.0, float(total_seconds))
            acquisition = float(cell.get("acquisition_duration_seconds", 0.0))
            cell["cell_post_processing_duration_seconds"] = max(
                0.0, total - acquisition)
            cell["cell_total_main_thread_duration_seconds"] = total
            if cpu_seconds is not None:
                cell["cell_total_main_thread_thread_cpu_seconds"] = max(
                    0.0, float(cpu_seconds))
            cell["post_processing_finished_at"] = float(
                time.time() if finished_at is None else finished_at)
            # cell_cycle is the existing acquisition/persistence envelope; its
            # detailed stages are subtimings and must not be counted twice.
            accounting = (
                "cell_cycle", "maintenance_refresh", "analysis_snapshot_build",
                "analysis_submit",
                "result_adoption", "mqtt_projection", "topology_position")
            accounted = sum(float(cell.get(f"{name}_duration_seconds", 0.0) or 0.0)
                            for name in accounting)
            remaining = total - accounted
            cell["cell_remaining_other_duration_seconds"] = remaining
            cell["cell_remaining_other_status"] = "executed"
            if remaining < -0.001:
                self._observability_errors += 1
            cell["state"] = state
            self._last_completed_cell = dict(cell)
            self._current_cell = None

    def cell_aborted(self, total_seconds, *, cpu_seconds=None, finished_at=None):
        """Close a partial cell slot without presenting it as successful."""
        self.cell_post_finished(total_seconds, cpu_seconds=cpu_seconds,
                                finished_at=finished_at, state="failed")

    def cell_samples(self, samples) -> None:
        """Record stack spread and bounded per-identity effective intervals."""
        values = [(serial, float(timestamp)) for serial, timestamp in samples]
        if not values:
            return
        with self._lock:
            timestamps = [timestamp for _serial, timestamp in values]
            spread = max(timestamps) - min(timestamps)
            self._current["cell_stack_sample_spread_seconds"] = spread
            per_serial = {}
            for serial, timestamp in values:
                if not serial:
                    continue
                previous = self._last_cell_by_serial.get(serial)
                interval = None if previous is None else max(0.0, timestamp - previous)
                self._last_cell_by_serial[serial] = timestamp
                if interval is not None:
                    self._rolling[f"cell_interval:{serial}"].append(interval)
                history = self._rolling.get(f"cell_interval:{serial}")
                per_serial[serial] = {
                    "last_seconds": interval,
                    "median_seconds": statistics.median(history) if history else None,
                    "max_seconds": max(history) if history else None,
                }
            self._current["effective_cell_intervals_by_serial"] = per_serial
            if self._current_cell is not None:
                self._current_cell["cell_stack_sample_spread_seconds"] = spread
                self._current_cell["effective_cell_intervals_by_serial"] = per_serial

    def cell_analysis_profiles(self, profiles, global_profile=None) -> None:
        """Store only the latest bounded, scalar profiling projection."""
        try:
            bounded = []
            for profile in list(profiles)[:6]:
                if not isinstance(profile, dict):
                    continue
                bounded.append(profile)
            global_value = dict(global_profile or {})
        except Exception:
            return
        with self._lock:
            self._cell_analysis_profiles = bounded
            self._cell_analysis_global = global_value

    def analysis_worker_status(self, status) -> None:
        """Publish the latest bounded worker lifecycle counters."""
        try:
            value = dict(status)
        except Exception:
            return
        with self._lock:
            self._analysis_worker = value

    def persistence_worker_status(self, status) -> None:
        try:
            value = dict(status)
        except Exception:
            return
        with self._lock:
            self._persistence_worker = value

    def snapshot_details(self, details):
        """Attach bounded scalar/per-module snapshot profiling to the cell slot."""
        with self._lock:
            if self._current_cell is not None:
                self._current_cell["analysis_snapshot"] = dict(details)
                if (float(details.get("other_seconds", 0.0) or 0.0) < -0.001
                        or float(details.get(
                            "other_thread_cpu_seconds", 0.0) or 0.0) < -0.001):
                    self._observability_errors += 1

    def snapshot(self) -> dict:
        with self._lock:
            # Legacy top-level fields deliberately represent one completed cycle.
            result = dict(self._last_completed_cycle)
            result.update({
                "poll_target_s": self.poll_target,
                "cell_target_s": self.cell_target,
                "cycle_overrun_count": self._cycle_overruns,
                "cycle_overrun_max_seconds": self._cycle_overrun_max,
                "cell_overrun_count": self._cell_overruns,
                "cell_overrun_max_seconds": self._cell_overrun_max,
            })
            result["rolling"] = {
                name: {"count": len(values),
                       "median_seconds": statistics.median(values),
                       "max_seconds": max(values)}
                for name, values in self._rolling.items() if values
            }
            result["cell_analysis_profiling"] = {
                "modules": list(self._cell_analysis_profiles),
                "global": dict(self._cell_analysis_global),
            }
            result["cell_analysis_worker"] = dict(self._analysis_worker)
            result["derived_persistence_worker"] = dict(self._persistence_worker)
            result["current_cycle"] = dict(self._current)
            result["last_completed_cycle"] = dict(self._last_completed_cycle)
            result["last_completed_cell_cycle"] = dict(self._last_completed_cell)
            result["observability_error_count"] = self._observability_errors
            return result
