"""SOC gap detection against the current second-lowest observed module."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from soc_crash_events import SocGapEvent, make_gap_event

DEFAULT_GAP_PP = 5.0
DEFAULT_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class SocObservation:
    module: int
    soc: float
    timestamp: float
    physical_serial: str | None = None

    def __post_init__(self):
        if type(self.module) is not int or not 1 <= self.module <= 6:
            raise ValueError("module must be 1..6")
        for value in (self.soc, self.timestamp):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("SOC and timestamp must be finite numbers")
        if not 0 <= self.soc <= 100:
            raise ValueError("SOC must be 0..100")
        if self.physical_serial is not None and (
                not isinstance(self.physical_serial, str) or not self.physical_serial.strip()
                or len(self.physical_serial) > 128):
            raise ValueError("invalid documented serial")


class SocCrashDetector:
    """Current SOC gap to the second-lowest observed module; one candidate.

    Unique initial minimum seeds the role only. Tied new minima wait for a
    unique minimum. Fewer than two observations suspend comparison, not time.
    Duplicate/older frames are ignored; startup never replays raw history.
    """

    def __init__(self, x_pp=DEFAULT_GAP_PP, y_seconds=DEFAULT_WINDOW_SECONDS):
        if (isinstance(x_pp, bool) or not isinstance(x_pp, (int, float))
                or not math.isfinite(x_pp) or not 0.01 <= x_pp <= 100):
            raise ValueError("soc_crash_gap_pp must be 0.01..100")
        if type(y_seconds) is not int or not 1 <= y_seconds <= 86400:
            raise ValueError("soc_crash_window_seconds must be 1..86400")
        self.x_pp, self.y_seconds = float(x_pp), y_seconds
        self.weakest_module = None
        self._last_timestamp = None
        self._awaiting_unique = False
        self._pending = None
        self.candidates_started = 0

    @classmethod
    def from_options(cls, options):
        import logging
        if "soc_crash_drop_pp" in options:
            logging.getLogger(__name__).warning(
                "Legacy soc_crash_drop_pp is not used for gap detection; "
                "soc_crash_gap_pp controls the new rule (default 5 pp)")
        return cls(options.get("soc_crash_gap_pp", 5.0),
                   options.get("soc_crash_window_seconds", 300))

    def _start(self, lowest, previous):
        self.weakest_module = lowest.module
        self._awaiting_unique = False
        self._pending = (lowest, previous)
        self.candidates_started += 1

    def observe(self, observations: list[SocObservation]) -> list[SocGapEvent]:
        if not observations:
            return []
        samples = {s.module: s for s in observations}
        if len(samples) != len(observations) or len({s.timestamp for s in observations}) != 1:
            raise ValueError("one PWR frame requires unique modules and one sample timestamp")
        timestamp = observations[0].timestamp
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            return []
        self._last_timestamp = timestamp
        if self._pending is not None and timestamp-self._pending[0].timestamp > self.y_seconds:
            self._pending = None
        if len(samples) < 2:
            return []  # retain role/start; do not infer removal from partial input
        minimum = min(s.soc for s in samples.values())
        minima = [s for s in samples.values() if s.soc == minimum]
        incumbent = samples.get(self.weakest_module)
        if incumbent is not None and incumbent.soc == minimum:
            pass  # later ties retain the incumbent and candidate start
        elif len(minima) > 1:
            self.weakest_module = None
            self._pending = None
            self._awaiting_unique = True
            return []
        else:
            lowest = minima[0]
            if incumbent is not None or self._awaiting_unique:
                self._start(lowest, incumbent)
            else:
                # Initial unique minimum or missing incumbent: existing baseline path.
                self.weakest_module = lowest.module
                self._pending = None
        if self._pending is None:
            return []
        start, previous = self._pending
        current = samples[start.module]
        second = min(s.soc for s in samples.values() if s.module != current.module)
        gap = Decimal(str(second)) - Decimal(str(current.soc))
        if gap >= Decimal(str(self.x_pp)):
            references = [s for s in samples.values() if s.module != current.module and s.soc == second]
            event = make_gap_event(start, previous, current, references, self.x_pp, self.y_seconds)
            self._pending = None
            return [event]
        return []
