"""Cooperative request-local budget; no worker, timer, or background scan."""
from contextlib import contextmanager
from contextvars import ContextVar
import time


class ResearchQueryError(ValueError):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


_cancelled = ContextVar("guardian_research_cancelled", default=None)


_deadline = ContextVar("guardian_research_deadline", default=None)


def check_budget(deadline=None):
    from work_scheduler import active, checkpoint, WorkCancelled, in_atomic
    if in_atomic():
        return
    from lifecycle_leases import validate_current
    validate_current()
    if active():
        checkpoint()
        # The request deadline remains an outer bound, never a live idle slot.
        cancelled = _cancelled.get()
        limit = _deadline.get()
        if cancelled is not None and cancelled():
            raise WorkCancelled("cancelled")
        if ((limit is not None and time.monotonic() >= limit) or
                (deadline is not None and time.monotonic() >= deadline)):
            raise WorkCancelled("deadline exceeded")
        return
    from history_live_priority import check_live_priority
    check_live_priority()
    check_request_budget(deadline)


def check_request_budget(deadline=None):
    """Check cancellation/deadline without re-entering live coordination."""
    cancelled = _cancelled.get()
    if cancelled is not None and cancelled():
        raise ResearchQueryError("cancelled", "history request was cancelled", 503)
    active = _deadline.get()
    if active is not None:
        deadline = active if deadline is None else min(active, deadline)
    if deadline is not None and time.monotonic() >= deadline:
        raise ResearchQueryError("timeout", "research query timed out", 503)


@contextmanager
def query_budget(deadline):
    outer = _deadline.get()
    if outer is not None:
        deadline = outer if deadline is None else min(outer, deadline)
    token = _deadline.set(deadline)
    try:
        check_budget()
        yield
        check_budget()
    finally:
        _deadline.reset(token)


def budgeted(items):
    """Check between records, including comprehensions and lazy generators."""
    for item in items:
        check_budget()
        yield item
    check_budget()


@contextmanager
def budget_lock(lock):
    deadline = _deadline.get()
    from work_scheduler import active
    if active():
        # A paused reader may hold this lock. Never occupy its scheduler lane
        # while blocking on acquire: give it a chance to finish and release.
        while not lock.acquire(blocking=False):
            check_budget()
            time.sleep(0.001)
        try:
            check_budget()
            yield
        finally:
            lock.release()
        return
    if deadline is None:
        with lock:
            yield
        return
    check_budget()
    acquired = lock.acquire(timeout=max(0, deadline - time.monotonic()))
    if not acquired:
        raise ResearchQueryError("timeout", "research query timed out", 503)
    try:
        check_budget()
        yield
    finally:
        lock.release()


def acquire_shared_file_lock(handle):
    """Keep existing blocking behavior outside Research requests."""
    import fcntl
    deadline = _deadline.get()
    from work_scheduler import active
    if deadline is None and not active():
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        return
    while True:
        check_budget()
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(0.01 if deadline is None else min(0.05, max(0, deadline - time.monotonic())))


@contextmanager
def cancellation_scope(cancelled):
    """Propagate disconnect checks through existing per-record budget checks."""
    token = _cancelled.set(cancelled)
    try:
        check_budget()
        yield
        check_budget()
    finally:
        _cancelled.reset(token)
