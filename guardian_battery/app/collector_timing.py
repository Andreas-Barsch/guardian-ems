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
        self._rolling = defaultdict(lambda: deque(maxlen=self.rolling_size))
        self._cycle_overruns = 0
        self._cycle_overrun_max = 0.0
        self._cell_overruns = 0
        self._cell_overrun_max = 0.0
        self._cell_analysis_profiles = []
        self._cell_analysis_global = {}
        self._analysis_worker = {}

    def cycle_started(self, wall_time: float, monotonic_time: float) -> None:
        with self._lock:
            interval = (None if self._last_cycle_start is None else
                        monotonic_time - self._last_cycle_start)
            self._last_cycle_start = monotonic_time
            self._current = {"cycle_started_at": float(wall_time),
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

    def duration(self, name: str, seconds: float) -> None:
        value = max(0.0, float(seconds))
        with self._lock:
            self._current[f"{name}_duration_seconds"] = value
            self._rolling[name].append(value)

    def cycle_finished(self, seconds: float) -> None:
        self.duration("cycle", seconds)
        with self._lock:
            if seconds > self.poll_target:
                overrun = seconds - self.poll_target
                self._cycle_overruns += 1
                self._cycle_overrun_max = max(self._cycle_overrun_max, overrun)

    def cell_finished(self, seconds: float) -> None:
        self.duration("cell_cycle", seconds)

    def cell_samples(self, samples) -> None:
        """Record stack spread and bounded per-identity effective intervals."""
        values = [(serial, float(timestamp)) for serial, timestamp in samples]
        if not values:
            return
        with self._lock:
            timestamps = [timestamp for _serial, timestamp in values]
            self._current["cell_stack_sample_spread_seconds"] = max(timestamps) - min(timestamps)
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
            for source, target in (
                ("active", "analysis_worker_active"),
                ("pending", "analysis_worker_pending"),
                ("generation_active", "analysis_worker_generation_active"),
                ("generation_latest", "analysis_worker_generation_latest"),
                ("last_duration_seconds", "analysis_worker_last_duration_seconds"),
                ("last_completed_at", "analysis_worker_last_completed_at"),
                ("age_seconds", "analysis_worker_age_seconds"),
                ("coalesced_count", "analysis_coalesced_count"),
                ("failure_count", "analysis_failure_count"),
            ):
                self._current[target] = value.get(source)

    def snapshot(self) -> dict:
        with self._lock:
            result = dict(self._current)
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
            return result
