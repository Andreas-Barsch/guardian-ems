"""Optional local diagnostic cooperation; inactive in existing runtime paths."""
from contextlib import contextmanager
from contextvars import ContextVar

_check = ContextVar('guardian_diagnostic_work_check', default=None)


def checkpoint():
    check = _check.get()
    if check is not None:
        check()


@contextmanager
def work_budget(check):
    token = _check.set(check)
    try:
        yield
    finally:
        _check.reset(token)
