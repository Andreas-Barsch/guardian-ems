"""Window-scoped configuration, unchanged PhaseEngine scientific methods."""
from contextvars import ContextVar
from types import SimpleNamespace
from phase_engine import PhaseEngine
from research_budget import ResearchQueryError


class IndexedPhaseEngine(PhaseEngine):
    def __init__(self, metadata, current_config_provider):
        self.metadata = metadata
        self._records = ContextVar('phase_window_config', default=())
        def current():
            if current_config_provider is None:
                raise ResearchQueryError('history_preparation_required',
                                         'Current configuration was not explicitly bound', 503)
            return current_config_provider()
        super().__init__(SimpleNamespace(records=lambda: list(self._records.get())), current)

    def analyse(self, samples, *, mode='historical', what_if=None, window_to=None):
        records = ()
        if mode == 'historical' and samples:
            records = self.metadata.config_window(min(item['timestamp'] for item in samples),
                                                   window_to or max(item['timestamp'] for item in samples))
        token = self._records.set(records)
        try:
            return super().analyse(samples, mode=mode, what_if=what_if, window_to=window_to)
        finally:
            self._records.reset(token)
