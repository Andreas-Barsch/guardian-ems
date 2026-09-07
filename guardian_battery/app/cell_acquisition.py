"""Priority-zero sequential Cell/BAT acquisition."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

from cell_diagnostics import CellSample
from cell_history import cell_history_timing


LOG = logging.getLogger("guardian_battery")


def acquire_cell_round(modules, console, identity_resolver, cell_history, *,
                       parse_bat_fn, wall_clock=time.time, monotonic=time.monotonic,
                       timing=None, identity_resolution_base=0.0):
    """Read one module at a time and append each successful raw sample immediately."""
    acquired = []
    bat_seconds = history_seconds = 0.0
    identity_seconds = float(identity_resolution_base)
    sample_times = []
    for module in modules:
        try:
            command_started = monotonic()
            try:
                raw = console.command(f"bat {module.module}")
            finally:
                command_seconds = monotonic() - command_started
                bat_seconds += command_seconds
                if timing:
                    timing.duration("bat_request", command_seconds)
            rows = parse_bat_fn(raw)
            if not rows:
                continue
            sample_time = wall_clock()
            try:
                identity_started = monotonic()
                if identity_resolver is None:
                    raise RuntimeError("position history unavailable")
                serial, position_history_id = identity_resolver.identity_at(
                    module.module, datetime.fromtimestamp(sample_time, timezone.utc))
                identity_seconds += monotonic() - identity_started
            except Exception as identity_exc:
                LOG.warning("Physische Identität Modul %s unklar: %s",
                            module.module, identity_exc)
                serial, position_history_id = None, None
            sample = CellSample(
                sample_time, module.module,
                [row["voltage_mv"] for row in rows], module.current_a,
                module.soc_percent, [row["temperature_c"] for row in rows],
                [row["balancing"] for row in rows], serial, position_history_id)
            try:
                history_started = monotonic()
                cell_history.append({
                    **asdict(sample), "module_serial": serial,
                    "position_history_id": position_history_id,
                    "identity_source": "position_history" if serial else "unknown",
                    **cell_history_timing(sample_time, module.pwr_sample_at),
                })
                history_seconds += monotonic() - history_started
            except Exception as history_exc:
                LOG.warning("Cell History Modul %s: %s", module.module, history_exc)
            acquired.append(sample)
            sample_times.append((sample.module_serial, sample.timestamp))
        except Exception as exc:
            LOG.warning("Zelldiagnostik Modul %s: %s", module.module, exc)
    if timing:
        timing.duration("bat_requests_total", bat_seconds)
        timing.duration("identity_resolution", identity_seconds)
        timing.duration("cell_history_write", history_seconds)
        timing.cell_samples(sample_times)
    return acquired
