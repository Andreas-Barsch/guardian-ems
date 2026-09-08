"""Bounded single-worker execution for rebuildable cell diagnostics."""
from __future__ import annotations

import logging
import threading
import time

from cell_diagnostics import CellDiagnosticStore
from collector_timing import ProfilingTimer
from config_history import config_id, diagnostic_parameters


def build_analysis_snapshot(cell_store, aggregate_store, module_positions,
                            options, maintenance_events, writer_status):
    """Capture bounded analysis inputs without copying immutable sample arrays."""
    positions = [int(position) for position in module_positions]
    current_identities = {
        position: cell_store.current_serial(position) for position in positions
    }
    samples = []
    for position in positions:
        samples.extend(cell_store.values_for_module(position))
    return {
        "max_samples": cell_store.max_samples,
        # Sample dictionaries and their nested arrays are append-only after add().
        # The outer list freezes membership while acquisition continues.
        "samples": samples,
        "current_identities": current_identities,
        "modules": positions,
        "options": dict(options),
        "maintenance_events": tuple(
            dict(event) if isinstance(event, dict) else event
            for event in maintenance_events),
        "aggregates": {
            position: aggregate_store.for_identity(
                position, current_identities[position])
            for position in positions
        },
        "aggregate_records_global": len(aggregate_store.records),
        "config_id": config_id(diagnostic_parameters(options)),
        "snapshot_sample_count": len(samples),
        "writer_status": {
            "derived_writer_active": bool(writer_status.get("active")),
            "derived_writer_pending": bool(writer_status.get("pending")),
        },
    }


def analyse_cell_snapshot(payload):
    """Rebuild and analyze one consistent collector snapshot."""
    store = CellDiagnosticStore.in_memory(payload["max_samples"])
    store.replace_samples(payload["samples"])
    store.set_current_identities(payload["current_identities"])
    results = {}
    profiles = []
    for module in payload["modules"]:
        profiler = ProfilingTimer()
        total_started = profiler.start()
        serial_started = profiler.start()
        latest_serial = store.current_serial(module)
        profiler.finish("current_serial", serial_started)
        aggregates = payload["aggregates"].get(module, ())
        profiler.counter("aggregate_for_identity_seconds", 0.0)
        store_profiler = profiler.child()
        analysis_started = profiler.start()
        analysis = store.analyse(
            module, payload["options"], payload["maintenance_events"],
            aggregates, profiler=store_profiler)
        profiler.finish("store_analyse", analysis_started)
        assembly_started = profiler.start()
        results[module] = {**analysis, "physical_module_serial": latest_serial}
        profiler.finish("result_assembly", assembly_started)
        profiler.finish("module_analysis_total", total_started)
        profiler.counter("module_position", module)
        profiler.counter("physical_serial", latest_serial)
        profiler.counter("sample_count", analysis.get("sample_count"))
        profiler.counter("aggregate_record_count", len(aggregates))
        profiler.section("store", store_profiler.snapshot())
        profiles.append(profiler.snapshot())
    return results, profiles, {
        **store.profiling_counts(),
        "aggregate_records_global": payload["aggregate_records_global"],
        "maintenance_event_count": len(payload["maintenance_events"]),
        **payload["writer_status"],
    }


def project_latest_analysis(latest, current_serial, *, now=time.time):
    """Decorate only results that still belong to the observed identity."""
    if latest is None:
        return {}
    age = max(0.0, now() - latest["analyzed_at"])
    return {
        position: {
            **result,
            "analysis_generation": latest["generation"],
            "analysis_source_sample_at": latest["source_sample_at"],
            "analysis_analyzed_at": latest["analyzed_at"],
            "analysis_age_seconds": age,
        }
        for position, result in latest["results"].items()
        if result.get("physical_module_serial") == current_serial(position)
    }


class CellAnalysisWorker:
    """Run one analysis at a time and retain only the newest pending generation."""

    def __init__(self, analyze, *, clock=time.monotonic, wall_clock=time.time,
                 logger=None):
        self.analyze = analyze
        self.clock = clock
        self.wall_clock = wall_clock
        self.log = logger or logging.getLogger("guardian_battery.cell_analysis_worker")
        self._condition = threading.Condition()
        self._pending = None
        self._active_generation = None
        self._latest = None
        self._stop = False
        self._thread = None
        self.submitted = 0
        self.coalesced = 0
        self.failures = 0
        self.last_duration = None
        self.last_completed_at = None
        self.last_error = None
        self.last_success = None
        self.latest_submitted_generation = None

    def _monotonic(self):
        try:
            return float(self.clock())
        except Exception:
            return None

    def _wall_time(self):
        try:
            return float(self.wall_clock())
        except Exception:
            return time.time()

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                return False
            self._stop = False
            self._thread = threading.Thread(
                target=self._run, name="guardian-cell-analysis", daemon=True)
            self._thread.start()
            return True

    def submit(self, generation, source_sample_at, payload):
        with self._condition:
            if self._stop:
                return False
            task = {
                "generation": int(generation),
                "created_at": self._wall_time(),
                "source_sample_at": float(source_sample_at),
                "payload": payload,
            }
            if self._pending is not None:
                self.coalesced += 1
            self.submitted += 1
            self.latest_submitted_generation = task["generation"]
            self._pending = task
            self._condition.notify()
            return True

    def latest(self):
        with self._condition:
            return None if self._latest is None else {
                **self._latest, "results": dict(self._latest["results"])}

    def status(self):
        with self._condition:
            latest_generation = (None if self._latest is None
                                 else self._latest["generation"])
            age = (None if self.last_completed_at is None else
                   max(0.0, self._wall_time() - self.last_completed_at))
            return {
                "active": self._active_generation is not None,
                "pending": self._pending is not None,
                "generation_active": self._active_generation,
                "generation_latest": latest_generation,
                "generation_submitted": self.latest_submitted_generation,
                "last_duration_seconds": self.last_duration,
                "last_completed_at": self.last_completed_at,
                "age_seconds": age,
                "coalesced_count": self.coalesced,
                "failure_count": self.failures,
                "submitted_count": self.submitted,
                "last_error": self.last_error,
                "last_success": self.last_success,
            }

    def stop(self, timeout=10.0):
        with self._condition:
            self._stop = True
            self._pending = None
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._stop:
                    self._condition.wait()
                if self._pending is None and self._stop:
                    return
                task = self._pending
                self._pending = None
                self._active_generation = task["generation"]
            started = self._monotonic()
            try:
                results, profiles, global_profile = self.analyze(task["payload"])
                completed_at = self._wall_time()
                finished = self._monotonic()
                duration = (None if started is None or finished is None else
                            max(0.0, finished - started))
                metadata = (task["payload"]
                            if isinstance(task["payload"], dict) else {})
                candidate = {
                    "generation": task["generation"],
                    "created_at": task["created_at"],
                    "source_sample_at": task["source_sample_at"],
                    "analyzed_at": completed_at, "results": results,
                    "profiles": profiles, "global_profile": global_profile,
                    "snapshot_sample_count": metadata.get(
                        "snapshot_sample_count"),
                    "config_id": metadata.get("config_id"),
                    "module_identities": dict(
                        metadata.get("current_identities", {})),
                    "success": True,
                }
                with self._condition:
                    if (self._latest is None
                            or candidate["generation"] >= self._latest["generation"]):
                        self._latest = candidate
                    self.last_duration = duration
                    self.last_completed_at = completed_at
                    self.last_error = None
                    self.last_success = True
            except Exception as exc:
                with self._condition:
                    self.failures += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.last_success = False
                self.log.exception("Cell analysis generation %s failed",
                                   task["generation"])
            finally:
                with self._condition:
                    self._active_generation = None
