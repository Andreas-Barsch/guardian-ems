"""Single-writer, coalescing persistence for rebuildable diagnostic state."""
from __future__ import annotations

import logging
import threading


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
            self._pending = (cell_payload, aggregate_payload)
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
            try:
                import time
                started = time.monotonic()
                self.cell_store.persist_payload(task[0])
                if self.timing:
                    self.timing.duration("diagnostic_store_save",
                                         time.monotonic() - started)
                if task[1] is not None:
                    started = time.monotonic()
                    self.aggregate_store.persist_payload(task[1])
                    if self.timing:
                        self.timing.duration("aggregate_write",
                                             time.monotonic() - started)
                self.persisted += 1
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.exception("Derived diagnostic persistence failed")
            finally:
                with self._condition:
                    self._active = False
