"""Deterministic phase intervals with explicit configuration provenance modes."""
from __future__ import annotations
from cell_diagnostics import classify_phases

PHASE_MODES = frozenset({"historical", "current", "what_if"})
PHASE_PARAMETER_KEYS = ("cell_diag_low_soc_percent", "cell_diag_high_soc_percent",
                        "cell_diag_charge_current_a", "cell_diag_discharge_current_a")

class PhaseEngineError(ValueError): pass

class PhaseEngine:
    def __init__(self, config_history, current_config_provider):
        self.config_history = config_history
        self.current_config_provider = current_config_provider

    def _historical_options(self, timestamp):
        matches = [item for item in self.config_history.records() if item["timestamp"] <= timestamp]
        return matches[-1]["parameters"] if matches else None

    def _options(self, mode, timestamp, what_if):
        if mode == "historical": return self._historical_options(timestamp)
        if mode == "current": return self.current_config_provider()
        if not isinstance(what_if, dict) or set(what_if) != set(PHASE_PARAMETER_KEYS):
            raise PhaseEngineError("what_if requires exactly the four phase parameters")
        return what_if

    def intervals(self, samples, *, mode="historical", what_if=None, window_to=None):
        if mode not in PHASE_MODES: raise PhaseEngineError("unsupported analysis mode")
        intervals = []
        for index, sample in enumerate(samples):
            options = self._options(mode, sample["timestamp"], what_if)
            phases = classify_phases(sample, options) if options else ["unknown"]
            key = "+".join(phases)
            end = samples[index + 1]["timestamp"] if index + 1 < len(samples) else (window_to or sample["timestamp"])
            if intervals and intervals[-1]["phase"] == key:
                intervals[-1]["to"] = end
                intervals[-1]["sample_count"] += 1
            else:
                intervals.append({"from": sample["timestamp"], "to": end, "phase": key,
                                  "phases": phases, "sample_count": 1})
        return intervals
