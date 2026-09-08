"""Single-writer, coalescing persistence for rebuildable diagnostic state."""
from __future__ import annotations

import logging
import threading
import time


class DerivedPersistenceWorker:
    """Persist immutable snapshots; only the newest pending generation matters."""

    def __init__(self, cell_store, aggregate_store, *, timing=None, logger=None):
        self.cell_store = cell_store
        self.aggregate_store = aggregate_store
        self.timing = timing
        self.log = logger or logging.getLogger("guardian_battery.derived_persistence")
        self._condition = threading.Condition()
        self._pending = None
        self._active = False
        self._stop = False
        self._thread = None
        self.submitted = 0
        self.persisted = 0
        self.coalesced = 0
        self.last_error = None
        self._generation = 0
        self._active_generation = None
        self._last_completed = None

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return False
            self._stop = False
            self._thread = threading.Thread(target=self._run,
                                            name="guardian-derived-persistence",
                                            daemon=True)
            self._thread.start()
            return True

    def submit(self, cell_payload, aggregate_payload):
        with self._condition:
            if self._pending is not None:
                self.coalesced += 1
            self.submitted += 1
            self._generation += 1
            self._pending = {
                "generation": self._generation, "created_at": time.time(),
                "cell_payload": cell_payload, "aggregate_payload": aggregate_payload,
            }
            self._condition.notify()

    def stop(self, timeout=10.0):
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def status(self):
        with self._condition:
            return {"submitted": self.submitted, "persisted": self.persisted,
                    "coalesced": self.coalesced, "pending": self._pending is not None,
                    "active": self._active,
                    "generation_active": self._active_generation,
                    "generation_latest": self._generation,
                    "last_completed": (None if self._last_completed is None
                                       else dict(self._last_completed)),
                    "last_error": self.last_error}

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stop:
                    self._condition.wait()
                if self._pending is None and self._stop:
                    return
                task = self._pending
                self._pending = None
                self._active = True
                self._active_generation = task["generation"]
            try:
                started = time.monotonic()
                started_at = time.time()
                cpu_started = time.thread_time()
                cell_started = time.monotonic()
                self.cell_store.persist_payload(task["cell_payload"])
                cell_seconds = time.monotonic() - cell_started
                aggregate_seconds = None
                if task["aggregate_payload"] is not None:
                    aggregate_started = time.monotonic()
                    self.aggregate_store.persist_payload(task["aggregate_payload"])
                    aggregate_seconds = time.monotonic() - aggregate_started
                self.persisted += 1
                self.last_error = None
                completed_at = time.time()
                completed = {
                    "generation": task["generation"],
                    "created_at": task["created_at"],
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "diagnostic_store_save_seconds": cell_seconds,
                    "aggregate_write_seconds": aggregate_seconds,
                    "wall_duration_seconds": time.monotonic() - started,
                    "thread_cpu_duration_seconds": time.thread_time() - cpu_started,
                    "success": True,
                }
                with self._condition:
                    self._last_completed = completed
            except Exception as exc:
                completed_at = time.time()
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.exception("Derived diagnostic persistence failed")
                with self._condition:
                    self._last_completed = {
                        "generation": task["generation"],
                        "created_at": task["created_at"],
                        "started_at": started_at,
                        "completed_at": completed_at, "success": False,
                        "wall_duration_seconds": time.monotonic() - started,
                        "thread_cpu_duration_seconds": time.thread_time() - cpu_started,
                        "error": self.last_error,
                    }
            finally:
                with self._condition:
                    self._active = False
                    self._active_generation = None
