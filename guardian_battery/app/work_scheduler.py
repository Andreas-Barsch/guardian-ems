"""Opt-in cooperative scheduler; no threads, collectors or implicit activation.

Quanta count checkpoints, not HA latency promises. A yield retains the Python
stack. Published reads have a separate lane that remains admitted during transport I/O;
Raw pauses derivation, not verified immutable reads. Each lane is FIFO round-robin.
Queue limits include running work. Raw sections never wait for a derive lease.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import deque
from dataclasses import dataclass, field
import threading
import time

_current = ContextVar('guardian_scheduled_job', default=None)
_selected = ContextVar('guardian_local_scheduler', default=None)


class WorkCancelled(BaseException):
    """Cannot be converted into a partial success by best-effort analyzers."""


class AdmissionError(RuntimeError):
    pass


@dataclass(eq=False)
class Job:
    kind: str
    lane: str
    cancelled: object
    deadline: object
    admitted: float = field(default_factory=time.monotonic)
    checks: int = 0
    quantum_checks: int = 0
    yields: int = 0
    wait_seconds: float = 0
    atomic_depth: int = 0
    atomic_max_seconds: float = 0
    state: str = 'queued'
    counters: dict = field(default_factory=dict)


class WorkScheduler:
    """Explicit local owner, bounded active requests and bounded status history.

    No suspended stack is persisted. On process restart the caller must reattach
    a durable repository job/cursor explicitly; there is no automatic replay.
    """
    def __init__(self, *, capacity=16, quantum_checks=32, history_limit=64):
        if min(capacity, quantum_checks, history_limit) < 1:
            raise ValueError('positive structural limits required')
        self.capacity, self.quantum_checks = capacity, quantum_checks
        self._condition = threading.Condition()
        self._queues = {'read': deque(), 'derive': deque()}
        self._history = deque(maxlen=history_limit)
        self._raw = 0
        self._closed = False
        self._rejected = 0

    def _cancel(self, job):
        if self._closed or (job.cancelled is not None and job.cancelled()):
            raise WorkCancelled('cancelled')
        if job.deadline is not None and time.monotonic() >= job.deadline:
            raise WorkCancelled('deadline exceeded')

    def checkpoint(self, job):
        # Never suspend while holding a shared write transaction. Cancellation
        # is deferred until its bounded commit/rollback has released the lock.
        if job.atomic_depth:
            return
        with self._condition:
            self._cancel(job)
            queue = self._queues[job.lane]
            job.checks += 1
            job.quantum_checks += 1
            if queue[0] is job and job.quantum_checks >= self.quantum_checks:
                if len(queue) > 1:
                    queue.rotate(-1)
                    job.yields += 1
                    self._condition.notify_all()
                job.quantum_checks = 0
            start = time.monotonic()
            try:
                while (self._raw and job.lane == 'derive') or queue[0] is not job:
                    job.state = 'waiting'
                    self._cancel(job)
                    self._condition.wait(0.025)
                self._cancel(job)
                job.state = 'running'
            finally:
                job.wait_seconds += time.monotonic() - start

    def execute(self, kind, function, *, lane='derive', cancelled=None, deadline=None):
        if lane not in self._queues:
            raise ValueError('unknown lane')
        parent = _current.get()
        if parent is not None:
            if parent[0] is not self:
                raise AdmissionError('nested scheduler owners are not allowed')
            # Nested work stays in the existing admission, never a second lane,
            # but its additional cancellation/deadline still applies to checks.
            job = parent[1]
            old_cancel, old_deadline = job.cancelled, job.deadline
            job.cancelled = lambda: ((old_cancel is not None and old_cancel()) or
                                     (cancelled is not None and cancelled()))
            limits = [value for value in (old_deadline, deadline) if value is not None]
            job.deadline = min(limits) if limits else None
            try:
                self.checkpoint(job)
                result = function()
                self.checkpoint(job)
                return result
            finally:
                job.cancelled, job.deadline = old_cancel, old_deadline
        job = Job(kind, lane, cancelled, deadline)
        with self._condition:
            if self._closed or sum(map(len, self._queues.values())) >= self.capacity:
                self._rejected += 1
                raise AdmissionError('scheduler closed or capacity exhausted')
            self._queues[lane].append(job)
        token = _current.set((self, job))
        selected = _selected.set(self)
        cpu = time.thread_time()
        try:
            self.checkpoint(job)
            result = function()
            self.checkpoint(job)
            job.state = 'complete'
            return result
        except WorkCancelled:
            job.state = 'cancelled'
            raise
        except BaseException:
            job.state = 'failed'
            raise
        finally:
            _current.reset(token)
            _selected.reset(selected)
            with self._condition:
                self._queues[lane].remove(job)
                self._history.append(self._snapshot(job) | {
                    'wall_seconds': time.monotonic() - job.admitted,
                    'thread_cpu_seconds': time.thread_time() - cpu})
                self._condition.notify_all()

    @staticmethod
    def _snapshot(job):
        return {'kind': job.kind, 'lane': job.lane, 'state': job.state,
                'checkpoints': job.checks, 'yields': job.yields,
                'wait_seconds': job.wait_seconds,
                'age_seconds': time.monotonic() - job.admitted,
                'atomic_max_seconds': job.atomic_max_seconds,
                'counters': dict(job.counters)}

    def status(self):
        with self._condition:
            return {'active': [self._snapshot(j) for q in self._queues.values() for j in q],
                    'recent': list(self._history), 'rejected': self._rejected,
                    'raw_active': self._raw, 'closed': self._closed,
                    'physical_io_bytes': None, 'ram_bytes': None}

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @contextmanager
    def select(self):
        """Explicitly associate narrow raw operations in this calling thread."""
        token = _selected.set(self)
        try:
            yield
        finally:
            _selected.reset(token)

    @contextmanager
    def raw_section(self):
        with self._condition:
            self._raw += 1
        try:
            yield
        finally:
            with self._condition:
                self._raw -= 1
                self._condition.notify_all()


def active():
    return _current.get() is not None


def checkpoint():
    current = _current.get()
    if current is not None:
        current[0].checkpoint(current[1])


def account(**counters):
    current = _current.get()
    if current:
        for key, value in counters.items():
            current[1].counters[key] = current[1].counters.get(key, 0) + value


@contextmanager
def atomic():
    current = _current.get()
    if current is None:
        yield
        return
    job = current[1]
    started = time.monotonic()
    job.atomic_depth += 1
    try:
        yield
    finally:
        job.atomic_depth -= 1
        job.atomic_max_seconds = max(job.atomic_max_seconds, time.monotonic()-started)


@contextmanager
def raw_section():
    scheduler = _selected.get()
    if scheduler is None:
        yield
    else:
        with scheduler.raw_section():
            yield


def in_atomic():
    current = _current.get()
    return current is not None and current[1].atomic_depth > 0


def current_status():
    """Small current-request observation; no admission or background work."""
    current = _current.get()
    return None if current is None else current[0]._snapshot(current[1])
