"""Bounded, cycle-independent observability for History HTTP requests."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class HistoryRequestTimingState:
    def __init__(self, *, wall=time.time, monotonic=time.monotonic,
                 thread_cpu=time.thread_time):
        self.wall = wall; self.monotonic = monotonic; self.thread_cpu = thread_cpu
        self._lock = threading.Lock(); self._local = threading.local()
        self._next_id = 0; self._current = None; self._last = None
        self._active = {}
        self.observability_errors = 0

    def _observability_error(self):
        try:
            with self._lock:
                self.observability_errors += 1
        except Exception:
            pass

    def begin(self, metadata):
        try:
            with self._lock:
                self._next_id += 1; request_id = self._next_id
            context = {"request_id": request_id, "status": "running",
                       "started_at": self.wall(), "_wall": self.monotonic(),
                       "_cpu": self.thread_cpu(), "components": {}, "counts": {},
                       **metadata}
            self._local.context = context
            with self._lock:
                self._active[request_id] = context
                self._current = self._public(context, running=True)
            return context
        except Exception:
            self._observability_error()
            self._local.context = None
            return None

    def context(self): return getattr(self._local, "context", None)

    @contextmanager
    def stage(self, name, status="executed"):
        context = self.context()
        if context is None:
            yield; return
        try:
            wall = self.monotonic(); cpu = self.thread_cpu()
        except Exception:
            self._observability_error(); yield; return
        try:
            yield
        finally:
            try:
                context["components"][name] = {
                    "status": status, "wall_seconds": self.monotonic() - wall,
                    "thread_cpu_seconds": self.thread_cpu() - cpu}
            except Exception:
                self._observability_error()

    def not_executed(self, *names):
        try:
            context = self.context()
            if context:
                for name in names:
                    context["components"][name] = {"status": "not_executed"}
        except Exception:
            self._observability_error()

    def unavailable(self, *names):
        try:
            context = self.context()
            if context:
                for name in names:
                    context["components"][name] = {"status": "not_available"}
        except Exception:
            self._observability_error()

    def record(self, name, wall_seconds, cpu_seconds=None, *, accounting=True):
        try:
            context = self.context()
            if context:
                value = {"status": "executed", "wall_seconds": float(wall_seconds),
                         "accounting": accounting}
                if cpu_seconds is not None:
                    value["thread_cpu_seconds"] = float(cpu_seconds)
                context["components"][name] = value
        except Exception:
            self._observability_error()

    def counts(self, **values):
        try:
            context = self.context()
            if context: context["counts"].update(values)
        except Exception:
            self._observability_error()

    def note_error(self, error):
        try:
            context = self.context()
            if context:
                context["error_type"] = type(error).__name__[:80]
                context["error_message"] = str(error)[:160]
        except Exception:
            self._observability_error()

    def complete(self, *, failed=False, error=None):
        context = self.context()
        if context is None: return
        try:
            total_wall = self.monotonic() - context["_wall"]
            total_cpu = self.thread_cpu() - context["_cpu"]
            known_wall = sum(item.get("wall_seconds", 0.0)
                             for item in context["components"].values()
                             if item.get("accounting", True))
            known_cpu = sum(item.get("thread_cpu_seconds", 0.0)
                            for item in context["components"].values()
                            if item.get("accounting", True))
            other_wall = total_wall - known_wall; other_cpu = total_cpu - known_cpu
            if other_wall < -1e-6 or other_cpu < -1e-6:
                self._observability_error()
            context["components"]["other"] = {"status": "executed",
                "wall_seconds": other_wall, "thread_cpu_seconds": other_cpu,
                "accounting": False}
            context.update(status="failed" if failed else "completed",
                           completed_at=self.wall(), history_total_wall_seconds=total_wall,
                           history_total_thread_cpu_seconds=total_cpu)
            if error:
                context.setdefault("error_type", (type(error).__name__
                                   if isinstance(error, BaseException) else str(error))[:80])
                context.setdefault("error_message", str(error)[:160])
            public = self._public(context)
            with self._lock:
                self._last = public
                self._active.pop(context["request_id"], None)
                newest = max(self._active, default=None)
                self._current = (self._public(self._active[newest], running=True)
                                 if newest is not None else None)
        except Exception:
            self._observability_error()
            try:
                with self._lock:
                    self._active.pop(context.get("request_id"), None)
                    newest = max(self._active, default=None)
                    self._current = (self._public(self._active[newest], running=True)
                                     if newest is not None else None)
            except Exception:
                pass
        finally:
            try: del self._local.context
            except (AttributeError, TypeError): pass

    def snapshot(self):
        with self._lock:
            current = None if self._current is None else dict(self._current)
            last = None if self._last is None else dict(self._last)
            errors = self.observability_errors
        if current:
            current["current_wall_seconds"] = self.monotonic() - current["_wall"]
            current.pop("_wall", None)
        return {"current_request": current, "last_completed_request": last,
                "observability_error_count": errors}

    @staticmethod
    def _public(context, running=False):
        result = {key: value for key, value in context.items()
                  if key not in {"_cpu"} and (running or key != "_wall")}
        result["components"] = {key: dict(value) for key, value in
                                context.get("components", {}).items()}
        result["counts"] = dict(context.get("counts", {}))
        return result
