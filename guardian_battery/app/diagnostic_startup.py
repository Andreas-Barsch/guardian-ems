"""Private diagnostic initialization; live acquisition never waits for history.

The only handover is a pair of complete stores. Until then original persisted
caches remain untouched and acquired samples remain durable in raw history.
"""
from collections import deque
import threading
import logging
import time
from pathlib import Path
from diagnostic_startup_io import StartupBudget, StartupIncomplete, startup_context, checkpoint
from cell_diagnostics import CellDiagnosticStore
from diagnostic_aggregates import DiagnosticAggregateStore
from current_condition_backfill import CurrentConditionBackfill
from diagnostic_backfill import DiagnosticAggregateBackfill

class DiagnosticStartup:
    MAX_PENDING_SAMPLES = 4096

    def __init__(self, cell_path, aggregate_path, history_directory, positions_path, options, *, gate=True):
        self.cell_path=Path(cell_path)
        self.aggregate_path=Path(aggregate_path)
        self.history_directory=Path(history_directory)
        self.positions_path=Path(positions_path)
        self.options=dict(options)
        self._lock=threading.Lock()
        self._stop=threading.Event()
        self._budget=StartupBudget(self._stop,gate=gate)
        self._pending=deque()
        self._stores=None
        self._thread=None
        self._state='pending'
        self._error=None
        self._reports={}
        self._started_at=None
        self._ready_at=None
        self.resume_sources={}
        self._pending_frontiers={}
        self._raw_replay_required=False
        self._pending_raw_backed=True

    def start(self):
        self._thread=threading.Thread(target=self._run,name='guardian-diagnostic-startup',daemon=True)
        self._thread.start()

    def submit(self, samples, *, frontiers=None):
        with self._lock:
            if self._state in ('blocked','cancelled','adopted'):
                return False
            # The live writer supplies receipts only after raw persistence.
            # On a long rebuild, bounded raw-tail continuation replaces an
            # overflowing in-memory queue; samples without receipts still fail closed.
            raw_backed = bool(frontiers) or not samples
            if len(self._pending)+len(samples)>self.MAX_PENDING_SAMPLES and self._pending_raw_backed and raw_backed:
                self._pending.clear()
                self._raw_replay_required=True
            if len(self._pending)+len(samples)>self.MAX_PENDING_SAMPLES:
                self._error='live_replay_queue_budget; raw history retained'
                self._state='blocked'
                self._stop.set()
                return False
            self._pending.extend(samples)
            self._pending_raw_backed = self._pending_raw_backed and raw_backed
            if frontiers is not None: self._pending_frontiers.update(frontiers)
            if samples and self._state=='ready':
                self._state='replaying_live'
            return True

    def take_ready(self):
        with self._lock:
            if self._state!='ready' or self._pending or self._raw_replay_required:
                return None
            result=self._stores
            self._stores=None
            self._state='adopted'
            self._stop.set()
            return result

    def status(self):
        with self._lock:
            return {'state':self._state,'complete':self._state in ('ready','adopted'),
                    'error':self._error,'pending_samples':len(self._pending),
                    'pending_capacity':self.MAX_PENDING_SAMPLES,
                    'started_at':self._started_at,'ready_at':self._ready_at,
                    'stage':self._budget.stage,'checkpoints':self._budget.checks,
                    'bytes_read':self._budget.bytes_read,'reports':dict(self._reports)}

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _position_signature(self):
        try:
            s=self.positions_path.stat()
            if s.st_size > 65536:
                raise StartupIncomplete("position_history_context_budget")
            return s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns
        except FileNotFoundError:
            return None

    def resume_context(self):
        from diagnostic_resume import context
        value = context(self.options, self.positions_path, self.resume_sources)
        # Never certify a changed identity history using results from its predecessor.
        value['position'] = self._resume_position
        return value

    def _preparation_checkpoint(self):
        """Durable-unit boundary, also used by restart fault-injection tests."""
        checkpoint()

    def _resume_tail(self, cell, aggregate):
        import json
        from diagnostic_resume import MAX_TAIL_BYTES, save, context
        import os
        from diagnostic_startup_io import MAX_FILES
        from history_append_receipt import signature, matches_prefix
        sources = []
        if self.history_directory.exists():
            with os.scandir(self.history_directory) as entries:
                for index, entry in enumerate(entries):
                    checkpoint()
                    if index >= MAX_FILES:
                        raise StartupIncomplete("source_inventory_budget")
                    if entry.is_file() and entry.name.endswith('.jsonl'):
                        sources.append(self.history_directory/entry.name)
        sources.sort()
        found = {path.name for path in sources}
        if set(self.resume_sources) - found:
            raise StartupIncomplete("diagnostic_source_removed: explicit affected-source preparation required")
        resolver = CurrentConditionBackfill(self.history_directory, self.positions_path)
        index = resolver._identity_index()
        for path in sources:
            old = self.resume_sources.get(path.name)
            if old is not None and not matches_prefix(path, old):
                raise StartupIncomplete("diagnostic_source_changed:" + path.name)
            offset = old.get('offset', old['size']) if old else 0
            current = signature(path)
            if current['size'] == offset: continue
            with path.open(encoding='utf-8') as handle:
                handle.seek(offset)
                unit_start = offset; unit_records = 0
                from history_preparation import CHUNK_BYTES
                while handle.tell() < current['size']:
                    checkpoint()
                    line = handle.readline(min(65537, current['size'] - handle.tell()))
                    if not line.endswith('\n') or len(line.encode()) > 65536:
                        raise StartupIncomplete("diagnostic_tail_partial_or_oversized_record")
                    self._budget.bytes_read += len(line.encode())
                    if not line.strip(): continue
                    sample = resolver._sample(json.loads(line), index)
                    if sample is not None:
                        cell.add(sample); aggregate.add(sample, self.options)
                    unit_records += 1
                    if unit_records >= 256 or handle.tell() - unit_start >= CHUNK_BYTES - 65536:
                        self.resume_sources[path.name] = {**current, 'offset': handle.tell()}
                        aggregate._dirty = True
                        save(self.cell_path, self.aggregate_path, cell.persistence_payload(),
                             aggregate.persistence_payload(), self.resume_context())
                        self._reports['resume'] = {'source': path.name, 'offset': handle.tell(),
                                                  'end': current['size'], 'mode': 'bounded_tail'}
                        self._preparation_checkpoint()
                        self._budget.close()
                        unit_start = handle.tell(); unit_records = 0
            self.resume_sources[path.name] = current
            aggregate._dirty=True
            save(self.cell_path, self.aggregate_path, cell.persistence_payload(),
                 aggregate.persistence_payload(), self.resume_context())
        self._reports['resume'] = {'sources': len(sources), 'mode': 'saved_frontier'}

    def _run(self):
        try:
            with startup_context(self._budget):
                checkpoint()
                self._started_at=time.time()
                self._state='initializing'
                position_revision=self._position_signature()
                from diagnostic_resume import identity
                self._resume_position = identity(self.positions_path)
                from diagnostic_resume import load as load_resume
                reason = 'missing_diagnostic_checkpoint'
                try:
                    saved = load_resume(self.cell_path, self.aggregate_path, self.options, self.positions_path)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    saved = None
                    reason = 'invalid_diagnostic_checkpoint: ' + str(exc)
                from diagnostic_resume import source_inventory, prepare
                from history_append_receipt import matches_prefix
                inventory = source_inventory(self.history_directory)
                if saved is not None and any(
                        name not in inventory or not matches_prefix(self.history_directory/name, sig)
                        for name, sig in saved['sources'].items()):
                    saved = None
                    reason = 'changed_or_removed_diagnostic_source; shared state is not subtractable'
                self._budget.stage='load_current_cache'
                if reason == 'missing_diagnostic_checkpoint' or saved is not None:
                    cell=CellDiagnosticStore(self.cell_path,int(self.options['cell_diag_history_max_samples']))
                    aggregate=DiagnosticAggregateStore(self.aggregate_path,CellDiagnosticStore.phases,
                        int(self.options.get('cell_diag_aggregate_retention_days',730)))
                else:
                    cell=CellDiagnosticStore.in_memory(int(self.options['cell_diag_history_max_samples']))
                    aggregate=DiagnosticAggregateStore.in_memory(CellDiagnosticStore.phases,
                        int(self.options.get('cell_diag_aggregate_retention_days',730)))
                if saved is None:
                    self._state='preparing'
                    self._budget.stage='reconcile_current_cache'
                    cell, aggregate = prepare(self, cell, aggregate, reason)
                    cell.path=self.cell_path; aggregate.path=self.aggregate_path
                else:
                    self.resume_sources = dict(saved['sources'])
                self._resume_tail(cell, aggregate)

                # Only retained keys need deduplication. Raw replay may have been
                # included by a source reader before its frozen end offset.
                keys=set()
                for value in cell.iter_all_samples():
                    checkpoint()
                    keys.add((value.get('module_serial'),int(value['module']),float(value['timestamp'])))
                self._budget.stage='replay_live'
                while not self._stop.is_set():
                    checkpoint()
                    if self._position_signature()!=position_revision:
                        raise StartupIncomplete('position_history_changed; caches not published')
                    with self._lock:
                        raw_replay = self._raw_replay_required
                        if raw_replay:
                            self._pending.clear()
                            self._raw_replay_required=False
                            self._pending_raw_backed=True
                    if raw_replay:
                        self._resume_tail(cell, aggregate)
                        keys = {(v.get('module_serial'), int(v['module']), float(v['timestamp']))
                                for v in cell.iter_all_samples()}
                    with self._lock:
                        already_ready = self._state == 'ready' and not self._pending
                        batch=[self._pending.popleft() for _ in range(min(6,len(self._pending)))]
                        if batch:
                            self._state='replaying_live'
                        frontier = dict(self._pending_frontiers) if not self._pending else None
                    if not batch:
                        if not already_ready:
                            if frontier is not None: self.resume_sources.update(frontier)
                            aggregate._dirty=True
                            from diagnostic_resume import save, context
                            # No acquisition lock is held during disk publication.
                            save(self.cell_path, self.aggregate_path, cell.persistence_payload(),
                                 aggregate.persistence_payload(), self.resume_context())
                            with self._lock:
                                if not self._pending and not self._raw_replay_required:
                                    self._stores=(cell,aggregate)
                                    self._ready_at=time.time()
                                    self._state='ready'
                        self._budget.close()
                        self._stop.wait(0.05)
                        continue
                    for sample in batch:
                        checkpoint()
                        key=(sample.module_serial,int(sample.module),float(sample.timestamp))
                        if key not in keys:
                            cell.add(sample)
                            keys.add(key)
                        aggregate.add(sample,self.options)
        except StartupIncomplete as exc:
            with self._lock:
                if self._state!='adopted' and self._state!='blocked':
                    self._state='cancelled' if self._stop.is_set() else 'blocked'
                    self._error=str(exc)
        except Exception as exc:
            with self._lock:
                if self._state not in ('blocked', 'adopted'):
                    self._state='cancelled' if self._stop.is_set() else 'blocked'
                    self._error=f'{type(exc).__name__}: {exc}'
        finally:
            self._budget.close()
            if self._state == "blocked":
                logging.getLogger("guardian_battery").warning(
                    "Diagnostic startup incomplete: %s; live raw acquisition continues", self._error)


class RepositoryStartup:
    """Local opt-in startup facade for derived repository readers only.

    It deliberately cannot satisfy DiagnosticStartup.take_ready(): converting
    legacy CurrentCondition/aggregate cache pairs is a separate integration gate.
    Construction never enumerates history, starts a thread or replays raw data.
    """
    def __init__(self, preparation, *, gate=True):
        self.preparation = preparation
        self._stop = threading.Event()
        self._budget = StartupBudget(self._stop, gate=gate)

    def status(self):
        return {**self.preparation.status(),
                'reader_scope': 'canonical_and_cell_display_repository',
                'legacy_cache_pair_ready': False,
                'bytes_read': self._budget.bytes_read}

    def step(self, *, fault=lambda _: None):
        from diagnostic_startup_io import repository_step
        try:
            return repository_step(self.preparation, self._budget, fault=fault)
        except StartupIncomplete:
            self.preparation.pause()
            return {'status': 'paused', 'reason': 'cancelled', 'raw_bytes': 0}

    def stop(self):
        self._stop.set()
        self.preparation.pause()

    def resume(self):
        self._stop.clear()
        self.preparation.resume()
