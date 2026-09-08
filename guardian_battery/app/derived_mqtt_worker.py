"""Bounded single worker for rebuildable derived MQTT projections."""
from __future__ import annotations

import threading
import time


class DerivedMqttWorker:
    def __init__(self, publish_job, *, wall_clock=time.time,
                 monotonic=time.monotonic):
        self.publish_job = publish_job
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self._condition = threading.Condition()
        self._active = None
        self._pending = None
        self._last_completed = None
        self._last_successful_key = None
        self._stop = False
        self._thread = None
        self.coalesced = 0
        self.failures = 0
        self.reconnect_invalidated = False

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return False
            self._stop = False
            self._thread = threading.Thread(
                target=self._run, name="guardian-derived-mqtt", daemon=True)
            self._thread.start()
            return True

    def submit(self, job):
        value = dict(job)
        value["created_at"] = float(value.get("created_at", self.wall_clock()))
        key = value["key"]
        with self._condition:
            if self._stop or key == self._last_successful_key:
                return False
            if ((self._active and self._active["key"] == key)
                    or (self._pending and self._pending["key"] == key)):
                return False
            if self._pending is not None:
                self.coalesced += 1
            self._pending = value
            self._condition.notify()
            return True

    def invalidate_reconnect(self):
        with self._condition:
            self._last_successful_key = None
            self.reconnect_invalidated = True

    def status(self):
        with self._condition:
            return {
                "active": self._active is not None,
                "pending": self._pending is not None,
                "generation_active": None if self._active is None else self._active["generation"],
                "generation_pending": None if self._pending is None else self._pending["generation"],
                "generation_last_completed": None if self._last_completed is None else self._last_completed["generation"],
                "last_successfully_published_generation": None if self._last_successful_key is None else self._last_successful_key[0],
                "last_completed": None if self._last_completed is None else dict(self._last_completed),
                "coalesced_count": self.coalesced,
                "failure_count": self.failures,
                "reconnect_invalidated": self.reconnect_invalidated,
            }

    def stop(self, timeout=2.0):
        with self._condition:
            self._stop = True
            self._pending = None
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stop:
                    self._condition.wait()
                if self._stop:
                    return
                job = self._pending
                self._pending = None
                self._active = job
            started_at = self.wall_clock(); started = self.monotonic()
            cpu_started = time.thread_time(); success = False; error = None
            details = {}
            try:
                details = dict(self.publish_job(job) or {})
                success = bool(details.get("success", True))
                if not success:
                    error = details.get("error", "publish_failed")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            completed_at = self.wall_clock()
            completed = {
                "generation": job["generation"],
                "created_at": job["created_at"], "started_at": started_at,
                "completed_at": completed_at,
                "wall_duration_seconds": self.monotonic() - started,
                "thread_cpu_duration_seconds": time.thread_time() - cpu_started,
                "success": success, "error": error,
                **details,
            }
            with self._condition:
                if success:
                    self._last_successful_key = job["key"]
                    self.reconnect_invalidated = False
                else:
                    self.failures += 1
                self._last_completed = completed
                self._active = None
