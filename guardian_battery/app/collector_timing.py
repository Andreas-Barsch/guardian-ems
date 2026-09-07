"""Bounded timing state and drift-free deadlines for Guardian acquisition."""
from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict, deque


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
            return result
