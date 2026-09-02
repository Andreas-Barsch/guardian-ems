"""Bounded background scheduling for deterministic daily diagnostics."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from daily_diagnostics import (DEFAULT_TIMEZONE, DailyDiagnosticBusyError,
                               DailyDiagnosticSources, EvidenceParameters,
                               SourceChangedError, _atomic_json,
                               build_source_catalog, probe_daily_inputs,
                               run_daily_diagnostic)
from guardian_diagnostics import (DiagnosticsCorrupt, DiagnosticsNotFound,
                                  read_validated_daily_result)


WORKER_STATE_SCHEMA_VERSION = 1
DEFAULT_CHECK_INTERVAL_SECONDS = 300
DEFAULT_GRACE_MINUTES = 15
DEFAULT_INITIAL_CATCHUP_DAYS = 3
DEFAULT_LATE_DATA_DAYS = 3
DEFAULT_AUTOMATIC_HISTORY_DAYS = 7
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 10


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class DailyDiagnosticWorker:
    """Sequential, interruptible worker over explicit persisted source paths."""

    def __init__(
        self,
        sources: DailyDiagnosticSources,
        output_root: Path | str,
        *,
        timezone_name: str = DEFAULT_TIMEZONE,
        grace_minutes: int = DEFAULT_GRACE_MINUTES,
        check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS,
        initial_catchup_days: int = DEFAULT_INITIAL_CATCHUP_DAYS,
        late_data_days: int = DEFAULT_LATE_DATA_DAYS,
        automatic_history_days: int = DEFAULT_AUTOMATIC_HISTORY_DAYS,
        shutdown_timeout_seconds: int = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        bms_parameters: EvidenceParameters | None = None,
        clock: Callable[[], datetime] | None = None,
        run_callable: Callable[..., Mapping[str, Any]] = run_daily_diagnostic,
        probe_callable: Callable[..., Any] = probe_daily_inputs,
        logger: logging.Logger | None = None,
    ):
        self.sources = sources
        self.output_root = Path(output_root)
        self.timezone_name = timezone_name
        self.zone = ZoneInfo(timezone_name)
        self.grace = timedelta(minutes=int(grace_minutes))
        self.check_interval_seconds = int(check_interval_seconds)
        self.initial_catchup_days = max(1, int(initial_catchup_days))
        self.late_data_days = max(1, int(late_data_days))
        self.automatic_history_days = max(
            self.initial_catchup_days, int(automatic_history_days))
        self.shutdown_timeout_seconds = int(shutdown_timeout_seconds)
        self.bms_parameters = bms_parameters or EvidenceParameters(
            daily_timezone=timezone_name)
        self.clock = clock or _utc_now
        self.run_callable = run_callable
        self.probe_callable = probe_callable
        self.log = logger or logging.getLogger("guardian_battery.daily_diagnostics")
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._startup_cycle_logged = False
        self._up_to_date_logged: set[str] = set()
        self._invalid_index_logged: set[str] = set()
        self._grace_wait_logged: set[str] = set()
        self.state_path = self.output_root / "state" / "daily_job_state.json"
        self.state = self._load_state()
        self._recover_interrupted()

    @staticmethod
    def _new_state() -> dict[str, Any]:
        return {
            "schema_version": WORKER_STATE_SCHEMA_VERSION,
            "last_check_at": None,
            "currently_running_date": None,
            "currently_running_attempt": None,
            "stability_candidates": {},
            "stale_dates": [],
            "last_successful_date": None,
            "last_result_status": None,
            "last_partial_date": None,
            "catchup": {"initial_complete": False, "processed_dates": []},
            "last_error": None,
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._new_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict)
                    or value.get("schema_version") != WORKER_STATE_SCHEMA_VERSION
                    or not isinstance(value.get("stability_candidates"), dict)
                    or not isinstance(value.get("catchup"), dict)):
                raise ValueError("invalid worker state schema")
            return {**self._new_state(), **value}
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self.log.warning("Daily diagnostics state corrupt; rebuilding safely: %s", exc)
            state = self._new_state()
            state["last_error"] = {"kind": "state_corrupt", "message": str(exc)}
            return state

    def _save_state(self) -> None:
        try:
            _atomic_json(self.state_path, self.state)
        except OSError as exc:
            self.log.error("Daily diagnostics worker state write failed path=%s error=%s",
                           self.state_path, exc)
            raise

    def _recover_interrupted(self) -> None:
        running = self.state.get("currently_running_date")
        if running:
            self.state["last_error"] = {
                "kind": "interrupted", "diagnostic_date": running,
                "attempt": self.state.get("currently_running_attempt"),
            }
            self.state["currently_running_date"] = None
            self.state["currently_running_attempt"] = None
            self.state.setdefault("stability_candidates", {}).pop(running, None)
            self.log.warning("Daily diagnostics interrupted attempt recovered: %s", running)
            self._save_state()

    def start(self) -> bool:
        if self.thread is not None and self.thread.is_alive():
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._loop, name="guardian-daily-diagnostics", daemon=True)
        self.log.info(
            "Daily diagnostics worker starting output_root=%s timezone=%s grace_minutes=%s "
            "check_interval_seconds=%s initial_catchup_days=%s "
            "automatic_history_days=%s late_data_days=%s",
            self.output_root, self.timezone_name, int(self.grace.total_seconds() / 60),
            self.check_interval_seconds, self.initial_catchup_days,
            self.automatic_history_days, self.late_data_days)
        self.thread.start()
        self.log.info("Daily diagnostics worker started thread=%s",
                      self.thread.name)
        return True

    def stop(self, timeout: float | None = None) -> bool:
        self.stop_event.set()
        thread = self.thread
        if thread is None:
            return True
        thread.join(self.shutdown_timeout_seconds if timeout is None else timeout)
        return not thread.is_alive()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:  # outer worker-cycle isolation
                self.log.exception("Daily diagnostics worker cycle failed: %s", exc)
                self.state["last_error"] = {
                    "kind": "worker_exception", "message": str(exc),
                    "at": _iso(self.clock()),
                }
                try:
                    self._save_state()
                except Exception:
                    self.log.exception("Daily diagnostics state update failed")
            self.stop_event.wait(self.check_interval_seconds)

    def _index(self, diagnostic_date: str) -> Mapping[str, Any] | None:
        path = self.output_root / "daily" / diagnostic_date / "index.json"
        try:
            value, _result = read_validated_daily_result(self.output_root, diagnostic_date)
            self._invalid_index_logged.discard(diagnostic_date)
            return value
        except DiagnosticsNotFound:
            return None
        except DiagnosticsCorrupt as exc:
            if diagnostic_date not in self._invalid_index_logged:
                self.log.warning("Daily diagnostics index invalid date=%s path=%s error=%s",
                                 diagnostic_date, path, exc)
                self._invalid_index_logged.add(diagnostic_date)
            return None

    def _eligible_dates(self, now: datetime) -> list[date]:
        local_now = now.astimezone(self.zone)
        today = local_now.date()
        yesterday = today - timedelta(days=1)
        candidates = [today - timedelta(days=age)
                      for age in range(1, self.automatic_history_days + 1)]
        if yesterday in candidates:
            candidates.remove(yesterday)
            return [yesterday, *sorted(candidates)]
        return sorted(candidates)

    def _grace_elapsed(self, candidate: date, now: datetime) -> bool:
        end = datetime.combine(candidate + timedelta(days=1), datetime_time(), self.zone)
        return now.astimezone(self.zone) >= end + self.grace

    def _sanitize_state_dates(self, today: date) -> None:
        candidates = self.state.setdefault("stability_candidates", {})
        invalid = []
        for text in list(candidates):
            try:
                parsed = date.fromisoformat(text)
            except ValueError:
                parsed = today
            if parsed >= today:
                invalid.append(text)
                candidates.pop(text, None)
        stale = []
        for text in self.state.setdefault("stale_dates", []):
            try:
                if date.fromisoformat(text) < today:
                    stale.append(text)
                else:
                    invalid.append(text)
            except ValueError:
                invalid.append(text)
        self.state["stale_dates"] = sorted(set(stale))
        if invalid:
            self.log.warning("Daily diagnostics ignored current/future candidates: %s", invalid)

    def _observe(self, diagnostic_date: str, fingerprint: str,
                 now: datetime) -> bool:
        candidates = self.state.setdefault("stability_candidates", {})
        previous = candidates.get(diagnostic_date)
        if not previous or previous.get("fingerprint") != fingerprint:
            if previous:
                self.log.info(
                    "Daily diagnostics candidate fingerprint changed date=%s old=%s new=%s; "
                    "stability reset", diagnostic_date,
                    str(previous.get("fingerprint", ""))[:12], fingerprint[:12])
            candidates[diagnostic_date] = {
                "fingerprint": fingerprint, "first_seen_at": _iso(now),
                "observations": 1,
            }
            self.log.info("Daily diagnostics candidate date=%s first_observation fingerprint=%s",
                          diagnostic_date, fingerprint[:12])
            return False
        previous["observations"] = int(previous.get("observations", 1)) + 1
        previous["last_seen_at"] = _iso(now)
        if previous["observations"] == 2:
            self.log.info("Daily diagnostics candidate date=%s stable fingerprint=%s",
                          diagnostic_date, fingerprint[:12])
        return True

    def check_once(self) -> list[dict[str, Any]]:
        """Perform one bounded scheduling cycle; exposed for deterministic tests."""
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("worker clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        today = now.astimezone(self.zone).date()
        self._sanitize_state_dates(today)
        eligible = self._eligible_dates(now)
        initial = not bool(self.state.get("catchup", {}).get("initial_complete"))
        limit = self.initial_catchup_days if initial else 1
        runnable = []
        recent = {today - timedelta(days=age)
                  for age in range(1, self.late_data_days + 1)}
        source_catalog = build_source_catalog(self.sources)
        if not self._startup_cycle_logged:
            recent_order = sorted(eligible, reverse=True)
            initial_plan = recent_order[:self.initial_catchup_days]
            self.log.info(
                "Daily diagnostics catch-up candidates=%s initial_plan=%s "
                "initial_budget=%s remaining_backlog=%s automatic_history_days=%s",
                len(eligible), [item.isoformat() for item in initial_plan],
                self.initial_catchup_days, max(0, len(eligible) - len(initial_plan)),
                self.automatic_history_days)
            self._startup_cycle_logged = True
        for candidate in eligible:
            text = candidate.isoformat()
            index = self._index(text)
            # Missing days in the bounded automatic horizon and recently indexed
            # days are the only dates that receive a semantic fingerprint probe.
            if index is not None and candidate not in recent:
                continue
            try:
                probe = self.probe_callable(
                    text, self.sources, timezone_name=self.timezone_name,
                    bms_parameters=self.bms_parameters,
                    source_catalog=source_catalog)
            except SourceChangedError as exc:
                self.log.warning("Daily diagnostics source changed for %s: %s", text, exc)
                self.state["last_error"] = {"kind": "source_changed",
                                            "diagnostic_date": text, "message": str(exc)}
                self.state.setdefault("stability_candidates", {}).pop(text, None)
                continue
            except Exception as exc:
                self.log.exception("Daily diagnostics probe failed date=%s error=%s", text, exc)
                self.state["last_error"] = {"kind": "probe_failure",
                                            "diagnostic_date": text, "message": str(exc)}
                self.state.setdefault("stability_candidates", {}).pop(text, None)
                continue
            fingerprint = probe.input_fingerprint
            if index is not None and index.get("input_fingerprint") == fingerprint:
                self.state.setdefault("stability_candidates", {}).pop(text, None)
                if text in self.state.setdefault("stale_dates", []):
                    self.state["stale_dates"].remove(text)
                if text not in self._up_to_date_logged:
                    self.log.info("Daily diagnostics unchanged date=%s fingerprint=%s; skipped",
                                  text, fingerprint[:12])
                    self._up_to_date_logged.add(text)
                continue
            self._up_to_date_logged.discard(text)
            if index is not None:
                if text not in self.state.setdefault("stale_dates", []):
                    self.state["stale_dates"].append(text)
                    self.log.info("Daily diagnostics stale %s", text)
            stable = self._observe(text, fingerprint, now)
            grace_elapsed = self._grace_elapsed(candidate, now)
            if not grace_elapsed and text not in self._grace_wait_logged:
                eligible_at = datetime.combine(
                    candidate + timedelta(days=1), datetime_time(), self.zone) + self.grace
                self.log.info(
                    "Daily diagnostics candidate date=%s waiting_for_grace eligible_at=%s "
                    "stable=%s", text, eligible_at.isoformat(), stable)
                self._grace_wait_logged.add(text)
            if stable and grace_elapsed:
                self._grace_wait_logged.discard(text)
                runnable.append(text)

        if initial and runnable:
            yesterday = (today - timedelta(days=1)).isoformat()
            runnable = ([yesterday] if yesterday in runnable else []) + sorted(
                (item for item in runnable if item != yesterday), reverse=True)
        results = []
        for text in runnable[:limit]:
            if self.stop_event.is_set():
                break
            attempt = str(uuid.uuid4())
            self.state["currently_running_date"] = text
            self.state["currently_running_attempt"] = attempt
            self._save_state()
            self.log.info("Daily diagnostics started date=%s attempt_id=%s", text, attempt)
            run_started = time.monotonic()
            try:
                result = self.run_callable(
                    text, self.sources, self.output_root,
                    timezone_name=self.timezone_name,
                    bms_parameters=self.bms_parameters,
                    lock_timeout_seconds=0.0, clock=self.clock)
            except (SourceChangedError, DailyDiagnosticBusyError) as exc:
                kind = ("source_changed" if isinstance(exc, SourceChangedError)
                        else "lock_busy")
                self.log.warning("Daily diagnostics %s for %s: %s", kind, text, exc)
                self.state["last_error"] = {"kind": kind,
                                            "diagnostic_date": text, "message": str(exc)}
                self.state.setdefault("stability_candidates", {}).pop(text, None)
            except Exception as exc:
                self.log.exception("Daily diagnostics run failed %s: %s", text, exc)
                self.state["last_error"] = {"kind": "run_exception",
                                            "diagnostic_date": text, "message": str(exc)}
            else:
                results.append(dict(result))
                duration = time.monotonic() - run_started
                if result.get("overall_status") in {"complete", "partial"} and result.get("persisted"):
                    self.state["last_successful_date"] = max(
                        filter(None, (self.state.get("last_successful_date"), text)))
                    self.state["last_result_status"] = result.get("overall_status")
                    if result.get("overall_status") == "partial":
                        self.state["last_partial_date"] = text
                    self.state["last_error"] = None
                    self.state.setdefault("stability_candidates", {}).pop(text, None)
                    if text in self.state.setdefault("stale_dates", []):
                        self.state["stale_dates"].remove(text)
                    processed = self.state.setdefault("catchup", {}).setdefault(
                        "processed_dates", [])
                    if text not in processed:
                        processed.append(text)
                    component = result.get("components", {}).get("bms_management", {})
                    self.log.info(
                        "Daily diagnostics completed date=%s attempt_id=%s result_id=%s "
                        "status=%s duration_seconds=%.3f fingerprint=%s component=%s "
                        "component_status=%s events=%s quality=%s coverage=%s",
                        text, attempt, result.get("daily_result_id"),
                        result.get("overall_status"), duration,
                        str(result.get("input_fingerprint", ""))[:12],
                        component.get("component_name", "bms_management"),
                        component.get("status"),
                        component.get("events", {}).get("count"),
                        component.get("quality"), component.get("coverage"))
                else:
                    self.state["last_error"] = {"kind": "run_failed",
                                                "diagnostic_date": text}
                    self.log.error(
                        "Daily diagnostics run failed date=%s attempt_id=%s status=%s "
                        "duration_seconds=%.3f", text, attempt,
                        result.get("overall_status"), duration)
            finally:
                self.state["currently_running_date"] = None
                self.state["currently_running_attempt"] = None
                self._save_state()

        if initial and (results or not self.state.get("stability_candidates")):
            self.state.setdefault("catchup", {})["initial_complete"] = True
        self.state["last_check_at"] = _iso(now)
        self._save_state()
        return results
