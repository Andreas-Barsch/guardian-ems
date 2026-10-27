"""Explicit application lifecycle for a prepared, local candidate.

No production option selects this class. Main accepts this injected lifecycle
for integration/compatibility tests. The unchanged default startup remains
legacy until the separate runtime/release gates have passed.
"""
import hashlib
import json
import threading
from pathlib import Path
from cell_acquisition import acquire_cell_round
from projection_repository import Unavailable
from raw_registry import encoded


class PreparedApplication:
    def __init__(self, *, root, manifest, preparation, tail, composition,
                 scheduler, cell_writer, identity, console, parse_bat,
                 wall_clock, poll_seconds, metadata_tail=None, epoch_consumers=(),
                 catalog_tails=(), daily_worker=None, collector=None, auxiliary=None):
        self.root = Path(root).resolve()
        self.manifest = manifest
        self.preparation = preparation
        self.tail = tail
        self.composition = composition
        self.scheduler = scheduler
        self.cell_writer = cell_writer
        self.identity = identity
        self.console = console
        self.parse_bat = parse_bat
        self.wall_clock = wall_clock
        if not 0 < poll_seconds < float('inf'):
            raise ValueError('explicit finite background scheduling interval required')
        self.poll_seconds = poll_seconds
        self.metadata_tail = metadata_tail
        self.epoch_consumers = tuple(epoch_consumers)
        self.catalog_tails = tuple(catalog_tails)
        self.daily_worker = daily_worker
        self.collector = collector
        self.auxiliary = auxiliary
        if daily_worker is not None and (daily_worker.scheduler is not scheduler or daily_worker.sources.input_backend is None):
            raise ValueError('Daily requires the shared scheduler and explicitly prepared indexed inputs')
        self.steps = [preparation.step, tail.step]
        if metadata_tail is not None:
            self.steps.append(metadata_tail.step)
        for consumer in self.epoch_consumers:
            self.steps.append(lambda consumer=consumer: consumer.step(expected_cursor=consumer.cursor(), max_records=8))
        self.steps.extend(item.step for item in self.catalog_tails)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.server = None
        self.failure = None
        self.closed = False
        self.turn = 0
        self._step_lock = threading.RLock()
        backend = cell_writer.append_backend
        if backend is None or backend.scheduler is not scheduler:
            raise ValueError('confirmed cell writer and shared scheduler required')
        paths = [preparation.repository.path, preparation.catalog.path,
                 preparation.epochs.path, backend.registry.root, backend.root]
        store_paths = list(paths)
        from dataclasses import fields
        paths.extend(getattr(composition.research.paths, item.name) for item in fields(composition.research.paths)
                     if getattr(composition.research.paths, item.name) is not None)
        if metadata_tail is not None:
            paths.extend((metadata_tail.repository.path, metadata_tail.registry.root))
            store_paths.extend((metadata_tail.repository.path, metadata_tail.registry.root))
        for consumer in self.epoch_consumers:
            paths.extend((consumer.store.path, consumer.registry.root))
            store_paths.extend((consumer.store.path, consumer.registry.root))
        for item in self.catalog_tails:
            paths.extend((item.catalog.path, item.consumer.registry.root))
            store_paths.extend((item.catalog.path, item.consumer.registry.root))
        if daily_worker is not None:
            paths.append(daily_worker.output_root)
            paths.extend(window.resolver.catalog.path for window in daily_worker.sources.input_backend.windows.values()
                         if window is not None)
        auxiliary_readers = {}
        for name, series in (('rs485', composition.history.rs485_series),
                             ('hycube', composition.history.hycube_series)):
            if series is None:
                continue
            resolver = series.window_backend.resolver
            paths.append(resolver.catalog.path)
            store_paths.append(resolver.catalog.path)
            guard = resolver.prefix_reader
            auxiliary_readers[name] = {'catalog': str(Path(resolver.catalog.path).resolve()),
                'registry': None if guard is None else guard.registry.config['registry_id'],
                'max_proof_pages': None if guard is None else guard.MAX_PROOF_PAGES}
            if guard is not None:
                paths.append(guard.registry.root)
                store_paths.append(guard.registry.root)
        policy = composition.research.policy_reader
        if policy is not None:
            resolver = policy.window.resolver
            if resolver.prefix_reader is None:
                raise ValueError('prepared policy requires confirmed-prefix registry')
            paths.extend((resolver.catalog.path,resolver.prefix_reader.registry.root))
            store_paths.extend((resolver.catalog.path,resolver.prefix_reader.registry.root))
            auxiliary_readers['policy']={'catalog':str(Path(resolver.catalog.path).resolve()),
                'registry':resolver.prefix_reader.registry.config['registry_id'],
                'record_format':policy.consumer.format_contract}
        for path in paths:
            if not Path(path).resolve().is_relative_to(self.root):
                raise ValueError('candidate store outside explicit application root')
        self._runtime_stores = tuple((Path(path), Path(path).stat().st_dev, Path(path).stat().st_ino)
                                     for path in store_paths)
        from application_tail import SCHEMA as tail_schema
        from application_metadata import SCHEMA as metadata_schema
        from application_migration import SCHEMA as adoption_schema
        from application_catalog import SCHEMA as catalog_schema
        from publication_window_index import SCHEMA as read_index_schema
        from metadata_revision import ROOTS, SEMANTICS
        application_schema = hashlib.sha256((tail_schema+metadata_schema+adoption_schema+catalog_schema+read_index_schema+repr(ROOTS)+SEMANTICS).encode()).hexdigest()
        prefix_reader = composition.research.window_backend.resolver.prefix_reader
        self.binding = hashlib.sha256(encoded({
            'root': str(self.root), 'stores': [str(Path(path).resolve()) for path in paths],
            'store_identities': [[str(Path(path).resolve()), Path(path).stat().st_dev, Path(path).stat().st_ino]
                                 for path in store_paths],
            'registry': backend.registry.config['registry_id'], 'diagnostics': tail.name,
            'writer_epoch': backend.writer_epoch,
            'namespace': 'legacy-local', 'contract': 'guardian-local-application-1',
            'application_schema': application_schema,
            'auxiliary_readers': auxiliary_readers,
            'confirmed_prefix_reader': None if prefix_reader is None else {
                'registry':prefix_reader.registry.config['registry_id'],
                'max_proof_pages':prefix_reader.MAX_PROOF_PAGES},
            'metadata': None if metadata_tail is None else {'head': metadata_tail.head,
                'mapping': metadata_tail.mapping, 'origin': metadata_tail.origin_proof},
            'catalog_formats': [item.consumer.format_contract for item in self.catalog_tails],
            'diagnostic_options': json.loads(tail.diagnostics.state(tail.name)['options']),
        })).hexdigest()

        if collector is not None:
            contract = collector.bind(self)
            self.binding = hashlib.sha256(encoded({'application':self.binding, 'collector':contract})).hexdigest()
            self.steps.append(collector.analysis_step)
        if auxiliary is not None:
            self.binding = hashlib.sha256(encoded({'application':self.binding,
                'auxiliary':auxiliary.contract})).hexdigest()


    def readiness(self):
        state = self.manifest.state()
        contract = json.loads(state['contract'])
        if state['status'] != 'complete':
            return {'state': 'preparation_required', 'reason': 'migration_incomplete'}
        if contract['provenance'].get('application_binding') != self.binding:
            return {'state': 'preparation_required', 'reason': 'application_binding_mismatch'}
        # These reads prove the existing pair and subscriber, without creating
        # a store, rebuilding history or asserting unobserved Raw coverage.
        self.tail.state()
        self.tail.index.require_schema()
        if self.tail.diagnostics.state(self.tail.name)['bundle'] is None:
            raise Unavailable('complete diagnostic pair not prepared')
        if self.metadata_tail is not None:
            self.metadata_tail._consumer(self.metadata_tail.current()).cursor()
        for consumer in self.epoch_consumers:
            consumer.cursor()
        for item in self.catalog_tails:
            item.state()
        return {'state': 'ready_local_candidate', 'migration': self.manifest.identifier,
                'coverage': 'confirmed prefixes only', 'production_approved': False,
                'utc_writer_enabled': False}

    def require_ready(self):
        state = self.readiness()
        if state['state'] != 'ready_local_candidate':
            raise Unavailable(state['reason'])
        return state

    def collect_once(self, modules, *, timing=None):
        """Reuse already obtained PWR modules; only the existing BAT round runs."""
        with self.scheduler.raw_section():
            self.require_ready()
            return acquire_cell_round(modules, self.console, self.identity, self.cell_writer,
                                      parse_bat_fn=self.parse_bat, wall_clock=self.wall_clock,
                                      timing=timing)

    def step(self):
        from research_budget import budget_lock
        with budget_lock(self._step_lock):return self._step()

    def _step(self):
        if self.thread is None:
            self.require_ready()
        else:
            # Full schema/subscriber checks run before starting any component.
            # Reopening every auxiliary database before every single work page
            # causes connection churn under concurrent Daily reads. Each child
            # validates its own schema/cursor; here retain the manifest and file
            # identity gate without reopening unrelated auxiliary stores.
            state = self.manifest.state()
            if (state['status'] != 'complete' or
                    json.loads(state['contract'])['provenance'].get('application_binding') != self.binding):
                raise Unavailable('application migration binding changed during runtime')
            for path, device, inode in self._runtime_stores:
                stat = path.stat()
                if (stat.st_dev, stat.st_ino) != (device, inode):
                    raise Unavailable('application store replaced during runtime')
        callback = self.steps[self.turn]
        self.turn = (self.turn + 1) % len(self.steps)
        return self.scheduler.execute('application_tail', callback,
                                      cancelled=self.stop.is_set)

    def _background(self):
        from work_scheduler import WorkCancelled
        try:
            while not self.stop.is_set():
                self.step()
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
        except WorkCancelled:
            if not self.stop.is_set():
                self.failure = 'background work cancelled'
        except Exception as exc:
            # Preserve the durable cursor. No implicit repair, rebuild or retry.
            self.failure = type(exc).__name__ + ': ' + str(exc)

    def start(self, *, http_port=None, bind_host='127.0.0.1'):
        self.require_ready()
        if self.thread is not None:
            raise RuntimeError('application already started')
        if self.stop.is_set():
            raise RuntimeError('closed application cannot restart')
        try:
            if http_port is not None:
                from config_ui import start_config_server
                self.server = start_config_server(http_port, bind_host=bind_host,
                                                  history_composition=self.composition, application=self)
            if self.auxiliary is not None:
                self.auxiliary.start()
            if self.collector is not None:
                self.collector.start()
            self.cell_writer.append_backend.notify = self.wake.set
            self.thread = threading.Thread(target=self._background, daemon=True,
                                           name='guardian-prepared-tail')
            self.thread.start()
            if self.daily_worker is not None:
                self.daily_worker.start()
        except BaseException:
            # A failed start still owns every resource already opened. Keep the
            # startup exception; report a separate cleanup failure as its note.
            import sys
            failure = sys.exception()
            try:
                self.close()
            except Exception as cleanup:
                failure.add_note('Startup cleanup failed: ' + str(cleanup))
            raise
        return self

    def run(self):
        """Main's injected entry. Acquisition transport ownership stays explicit."""
        self.start()
        try:
            if self.collector is not None:
                self.collector.run(self.stop)
                return
            while not self.stop.wait(self.poll_seconds):
                if self.failure:
                    raise RuntimeError(self.failure)
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.wake.set()
        errors = []
        def finish(callback):
            try:
                callback()
            except Exception as exc:
                errors.append(exc)
        if self.thread is not None and self.thread.ident is not None:
            finish(self.thread.join)
        if self.server is not None:
            finish(self.server.shutdown)
            finish(self.server.server_close)
        if self.daily_worker is not None:
            finish(self.daily_worker.stop)
        if self.auxiliary is not None:
            finish(self.auxiliary.close)
        if self.collector is not None:
            finish(self.collector.close)
        finish(self.scheduler.close)
        if errors:
            raise ExceptionGroup('Application shutdown failed', errors)
