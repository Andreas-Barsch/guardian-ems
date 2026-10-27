"""Historical work borrows only the configured live collector's idle slot.

No guessed HA latency target: the runtime provides its actual next deadline.
Outside the collector process (isolated tests/offline tools) the gate is inactive.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import weakref
from functools import wraps
from research_budget import ResearchQueryError, query_budget, check_budget

_lock=threading.Lock()
_jobs=threading.BoundedSemaphore(1)
_raw_writers=weakref.WeakSet()
_raw_active=0
_read_waiters=0
_enabled=False
_active=False
_next_due=None
_cooperative_read=ContextVar("guardian_cooperative_read",default=False)
_historical=ContextVar("guardian_historical_work",default=False)


def cycle_started():
    global _enabled,_active,_next_due
    with _lock: _enabled=True;_active=True;_next_due=None


def idle_until(deadline):
    global _active,_next_due
    with _lock: _active=False;_next_due=deadline


def check_live_priority():
    from work_scheduler import active, checkpoint
    if active():
        checkpoint()
        return
    if not _historical.get(): return
    if _cooperative_read.get():
        _wait_for_live_idle()
        return
    with _lock:
        busy=_enabled and (_active or _raw_active or any(writer._queue.qsize() for writer in _raw_writers))
    if busy:
        raise ResearchQueryError("live_acquisition_priority", "Live acquisition is active; historical work paused, submit again manually",503)


def _wait_for_live_idle():
    # Called only by the bounded readers of existing products, without raw-writer locks.
    # No retry/restart of the query: resume at its current record checkpoint.
    from research_budget import check_request_budget
    while True:
        check_request_budget()
        with _lock:
            busy = _enabled and (_active or _raw_active or
                _next_due is None or time.monotonic() >= _next_due or
                any(writer._queue.qsize() for writer in _raw_writers))
        if not busy:
            return
        time.sleep(0.01)


@contextmanager
def measurement_slot():
    """One bounded product read, paused during live work within its budget.

    Raw writers never acquire _jobs. Holding it while paused excludes only
    historical background work, not collection or persistence. No live-shared
    file/memory locks may be acquired by readers using this context.
    """
    global _read_waiters
    from work_scheduler import active
    from research_budget import check_request_budget
    if active():
        with historical_slot():
            yield
        return
    acquired = False
    with _lock:
        _read_waiters += 1
    try:
        while not acquired:
            check_request_budget()
            acquired = _jobs.acquire(blocking=False)
            if not acquired:
                time.sleep(0.01)
    finally:
        with _lock:
            _read_waiters -= 1
    historical = _historical.set(True)
    cooperative = _cooperative_read.set(True)
    try:
        check_budget()
        yield
        check_budget()
    finally:
        _cooperative_read.reset(cooperative)
        _historical.reset(historical)
        _jobs.release()


@contextmanager
def historical_slot():
    from work_scheduler import active, checkpoint
    if active():
        token = _historical.set(True)
        try:
            checkpoint()
            yield
            checkpoint()
        finally:
            _historical.reset(token)
        return
    if not _jobs.acquire(blocking=False):
        raise ResearchQueryError("history_busy","Another historical job is running; submit again manually",503)
    token=_historical.set(True)
    try:
        check_live_priority()
        with _lock: deadline=_next_due if _enabled else None
        with query_budget(deadline):
            yield
            check_budget()
    finally:
        _historical.reset(token)
        _jobs.release()


def register_raw_writer(writer):
    with _lock: _raw_writers.add(writer)


def raw_priority(function):
    @wraps(function)
    def wrapped(*args,**kwargs):
        global _raw_active
        with _lock: _raw_active+=1
        try:
            from work_scheduler import raw_section
            with raw_section():
                return function(*args,**kwargs)
        finally:
            with _lock: _raw_active-=1
    return wrapped


def collector_stopped():
    global _enabled,_active,_next_due
    with _lock: _enabled=False;_active=False;_next_due=None


def historical_work_active():
    return _historical.get()


def bounded_metadata_lines(handle):
    """Legacy unindexed context is limited to one 64-KiB checkpoint.

    This structural guard is not a measured HA release budget. Never truncate
    metadata silently: a larger checkpoint requires an indexed offline path.
    """
    from diagnostic_startup_io import active as startup_active, lines as startup_lines
    if startup_active():
        yield from startup_lines(handle)
        return
    if not historical_work_active():
        for line in handle:
            yield line
        return
    check_budget()
    value=handle.read(65537)
    if len(value.encode("utf-8"))>65536:
        raise ResearchQueryError("history_context_index_required",
            "Unindexed metadata exceeds the 65536-byte context bound; no complete coverage can be returned",503)
    for line in value.splitlines():
        check_budget()
        yield line


def bounded_metadata_paths(directory):
    """Bound legacy policy discovery before allocating a complete inventory."""
    if not historical_work_active():
        return sorted(directory.glob("*.jsonl"))
    import os
    paths=[]; size=0
    with os.scandir(directory) as entries:
        for number,entry in enumerate(entries,1):
            check_budget()
            if number>256:
                raise ResearchQueryError("history_context_index_required",
                    "Policy inventory exceeds 256 entries; indexed context is required",503)
            if entry.name.endswith(".jsonl") and entry.is_file():
                size+=entry.stat().st_size
                if size>65536:
                    raise ResearchQueryError("history_context_index_required",
                        "Unindexed policy context exceeds 65536 bytes; indexed context is required",503)
                paths.append(directory/entry.name)
    return sorted(paths)


def live_coordination_ready():
    """Explicit preparation must not run in an uncoordinated CLI process."""
    with _lock:
        return _enabled
