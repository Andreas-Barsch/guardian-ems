"""Cooperative, bounded I/O for the private diagnostic startup worker.

Inactive outside this worker: existing offline/research semantics are unchanged.
Structural limits fail closed, never publish a truncated diagnostic cache.
"""
from contextvars import ContextVar
from contextlib import contextmanager
import json
import os
import time

_context = ContextVar('diagnostic_startup_io', default=None)
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_TOKEN_BYTES = 65536
MAX_NODES = 8000000
MAX_FILES = 512
MAX_SOURCE_BYTES = 16 * 1024 * 1024

class StartupIncomplete(BaseException):
    """Must not be swallowed by legacy best-effort load/backfill handlers."""

class StartupBudget:
    def __init__(self, stop, *, gate=True):
        self.stop = stop
        self.gate = gate
        self.lease = False
        self.checks = 0
        self.bytes_read = 0
        self.stage = 'waiting_for_live'

    def checkpoint(self):
        self.checks += 1
        if self.stop.is_set():
            raise StartupIncomplete('cancelled')
        from work_scheduler import active, checkpoint
        if active():
            if self.lease:
                self.close()
            checkpoint()
            return
        if not self.gate:
            return
        import history_live_priority as priority
        if self.lease:
            priority._jobs.release()
            self.lease = False
        while not self.stop.is_set():
            with priority._lock:
                allowed = (not priority._read_waiters and priority._enabled and not priority._active
                           and not priority._raw_active
                           and priority._next_due is not None
                           and time.monotonic() < priority._next_due
                           and not any(w._queue.qsize() for w in priority._raw_writers))
            if allowed and priority._jobs.acquire(blocking=False):
                self.lease = True
                return
            self.stop.wait(0.05)
        raise StartupIncomplete('cancelled')

    def close(self):
        if self.lease:
            import history_live_priority as priority
            priority._jobs.release()
            self.lease = False

@contextmanager
def startup_context(budget):
    token = _context.set(budget)
    try:
        yield
    finally:
        budget.close()
        _context.reset(token)

def active():
    return _context.get() is not None

def checkpoint():
    budget = _context.get()
    if budget is not None:
        budget.checkpoint()

def paths(directory):
    if not active():
        return sorted(directory.glob('*.jsonl'))
    result = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            checkpoint()
            if index >= MAX_FILES:
                raise StartupIncomplete('source_inventory_budget')
            if entry.is_file() and entry.name.endswith('.jsonl'):
                if entry.stat().st_size > MAX_SOURCE_BYTES:
                    raise StartupIncomplete('source_file_budget')
                result.append(directory / entry.name)
    return sorted(result)

def lines(handle):
    if not active():
        yield from handle
        return
    # Freeze this append-only source at entry; ongoing live samples are replayed
    # from the bounded startup queue before the stores can be adopted.
    end = os.fstat(handle.fileno()).st_size
    if end > MAX_SOURCE_BYTES:
        raise StartupIncomplete("source_file_budget")
    while handle.tell() < end:
        checkpoint()
        remaining = end - handle.tell()
        line = handle.readline(min(MAX_TOKEN_BYTES + 1, remaining))
        if len(line.encode('utf-8')) > MAX_TOKEN_BYTES:
            raise StartupIncomplete('source_line_budget')
        if not line.endswith('\n'):
            raise StartupIncomplete('source_partial_line')
        _context.get().bytes_read += len(line.encode('utf-8'))
        yield line

class _JsonReader:
    """Decode containers incrementally, never one giant json.loads call."""
    def __init__(self, handle):
        self.handle = handle
        self.buffer = ''
        self.pos = 0
        self.eof = False
        self.nodes = 0
        self.bytes_read = 0
        self.decoder = json.JSONDecoder()

    def fill(self):
        checkpoint()
        self.buffer = self.buffer[self.pos:]
        self.pos = 0
        chunk = self.handle.read(8192)
        self.eof = not chunk
        self.buffer += chunk
        chunk_size = len(chunk.encode('utf-8'))
        self.bytes_read += chunk_size
        if self.bytes_read > MAX_CACHE_BYTES:
            raise StartupIncomplete('cache_byte_budget')
        _context.get().bytes_read += chunk_size
        if len(self.buffer.encode('utf-8')) > MAX_TOKEN_BYTES:
            raise StartupIncomplete('json_token_budget')

    def peek(self):
        while True:
            while self.pos < len(self.buffer) and self.buffer[self.pos].isspace():
                self.pos += 1
            if self.pos < len(self.buffer):
                return self.buffer[self.pos]
            if self.eof:
                return ''
            self.fill()

    def value(self, depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > 32:
            raise StartupIncomplete('json_materialization_budget')
        if self.nodes % 64 == 0:
            checkpoint()
        char = self.peek()
        if char in ('{', '['):
            self.pos += 1
            result = {} if char == '{' else []
            end = '}' if char == '{' else ']'
            if self.peek() == end:
                self.pos += 1
                return result
            while True:
                if char == '{':
                    key = self.value(depth + 1)
                    if not isinstance(key, str) or self.peek() != ':':
                        raise ValueError('invalid JSON object')
                    self.pos += 1
                    result[key] = self.value(depth + 1)
                else:
                    result.append(self.value(depth + 1))
                delimiter = self.peek()
                self.pos += 1
                if delimiter == end:
                    return result
                if delimiter != ',':
                    raise ValueError('invalid JSON separator')
        while True:
            try:
                result, end = self.decoder.raw_decode(self.buffer, self.pos)
                # A scalar at a chunk boundary may continue (e.g. 1e+12).
                if end == len(self.buffer) and not self.eof:
                    self.fill()
                    continue
                if end < len(self.buffer) and self.buffer[end] not in ' \t\r\n,:]}':
                    if not self.eof:
                        self.fill()
                        continue
                    raise ValueError('invalid JSON scalar')
                self.pos = end
                return result
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self.fill()

def load(path):
    if not active():
        return json.loads(path.read_text())
    checkpoint()
    if path.stat().st_size > MAX_CACHE_BYTES:
        raise StartupIncomplete('cache_byte_budget')
    with path.open(encoding='utf-8') as handle:
        reader = _JsonReader(handle)
        result = reader.value()
        if reader.peek():
            raise ValueError('trailing JSON data')
        return result


def repository_step(preparation, budget, *, fault=lambda _: None):
    """One durable unit under the existing live-priority/cancellation contract."""
    with startup_context(budget):
        budget.stage = 'repository_catchup'
        checkpoint()
        result = preparation.step(fault=fault)
        budget.bytes_read += result.get('raw_bytes', result.get('bytes', 0))
        return result
