"""Explicit local composition; main never imports/selects this factory.

Only references/captured generations are passed to the existing diagnostic
workers. Scheduler ownership is per composition, never a global singleton.
"""
from work_scheduler import WorkCancelled


def worker_call(scheduler, kind, callback, *, cancelled=None):
    try:
        return scheduler.execute(kind, callback, cancelled=cancelled)
    except WorkCancelled as exc:
        # Convert only after unwinding the full scientific operation. Internal
        # best-effort catches must not publish a cancellation as partial success.
        raise RuntimeError(f'{kind} cancelled: {exc}') from exc


class ScheduledWriter:
    def __init__(self, scheduler, writer):
        self.scheduler, self.writer = scheduler, writer
        self.cancelled = None

    def persist_payload(self, reference):
        return worker_call(self.scheduler, 'persistence',
                           lambda: self.writer.persist_payload(reference), cancelled=self.cancelled)


class LocalWorkComposition:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    def diagnostic_workers(self, diagnostics, *, max_persistence_steps=64):
        from diagnostic_repository import RepositoryPairWriter
        from derived_persistence import DerivedPersistenceWorker
        from cell_analysis_worker import CellAnalysisWorker
        writer = RepositoryPairWriter(diagnostics, max_steps=max_persistence_steps)
        scheduled = ScheduledWriter(self.scheduler, writer)
        persistence = DerivedPersistenceWorker(scheduled, scheduled)
        scheduled.cancelled = lambda: persistence._stop
        analysis = CellAnalysisWorker(lambda ref: worker_call(
            self.scheduler, 'analysis', lambda: diagnostics.analyse_reference(ref),
            cancelled=lambda: analysis._stop))
        return persistence, analysis, writer

    def daily_worker(self, *args, **kwargs):
        from daily_diagnostic_worker import DailyDiagnosticWorker
        return DailyDiagnosticWorker(*args, scheduler=self.scheduler, **kwargs)

    def display_worker(self, *args, **kwargs):
        from display_history_projection import DisplayHistoryProjectionWorker
        return DisplayHistoryProjectionWorker(*args, scheduler=self.scheduler, **kwargs)

    def mqtt_worker(self, publish_job, **kwargs):
        from derived_mqtt_worker import DerivedMqttWorker
        worker = DerivedMqttWorker(lambda job: worker_call(
            self.scheduler, 'mqtt', lambda: publish_job(job),
            cancelled=lambda: worker._stop), **kwargs)
        return worker

    def preparation_step(self, preparation, budget):
        from diagnostic_startup_io import repository_step
        return self.scheduler.execute('startup_preparation',
            lambda: repository_step(preparation, budget), cancelled=budget.stop.is_set)

    def canonical_step(self, callback, *, cancelled=None):
        return self.scheduler.execute('canonical', callback, cancelled=cancelled)

    def request(self, api, method, target, *, cancelled=None, deadline=None):
        """One caller/request; waits internally, never retries HTTP or callback.

        This is a local entry only, not activation of production HistoryAPI or
        proof that all legacy query routes are physically bounded.
        """
        from history_live_priority import historical_slot
        from research_budget import cancellation_scope, query_budget, ResearchQueryError
        from history_api import ApiResponse, error_json
        from work_scheduler import AdmissionError
        def run():
            with historical_slot(), cancellation_scope(cancelled), query_budget(deadline):
                return api._handle(method, target)
        try:
            return self.scheduler.execute('manual_read', run, lane='read',
                                          cancelled=cancelled, deadline=deadline)
        except (WorkCancelled, AdmissionError, ResearchQueryError) as exc:
            code = getattr(exc, 'code', 'cancelled' if isinstance(exc, WorkCancelled) else 'scheduler_full')
            body = error_json(code, str(exc))
            body['coverage'] = {'quality': 'unavailable', 'reason': code}
            body['budget'] = {'queue': 'bounded_fifo', 'retry': 'none',
                              'deadline_basis': 'request_deadline'}
            return ApiResponse(503, body)
