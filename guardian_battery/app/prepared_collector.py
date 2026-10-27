"""Existing live collector operations bound to explicit prepared application stores.

No production paths, device discovery, implicit history replay or default
activation. The owner supplies the existing console and MQTT transports.
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import threading
import time

from collector_timing import CollectorTiming, PeriodicDeadline
from cell_analysis_worker import CellAnalysisWorker, project_latest_analysis
from local_work_composition import LocalWorkComposition, worker_call
from soc_crash_detector import SocCrashDetector
from raw_registry import encoded, hashed


class PreparedCollector:
    def __init__(self, *, options, publisher, config_history, position_service,
                 maintenance_service, soc_store, technical_writer, technical_path,
                 state_path, trend_path, incident_path, wall_clock, monotonic, config_service=None):
        self.config_service = config_service
        self.options = dict(options)
        self.publisher = publisher
        self.config_history = config_history
        self.position_service = position_service
        self.maintenance_service = maintenance_service
        self.soc_store = soc_store
        self.technical_writer = technical_writer
        self.technical_path = Path(technical_path)
        self.state_path, self.trend_path, self.incident_path = map(Path, (state_path, trend_path, incident_path))
        self.clock, self.monotonic = wall_clock, monotonic
        self.detector = SocCrashDetector.from_options(options)
        self.pending_events = {}
        self.timing = CollectorTiming(float(options['poll_interval_seconds']), float(options['cell_diagnostics_interval_seconds']))
        self.poll = PeriodicDeadline(float(options['poll_interval_seconds']), clock=monotonic)
        self.cell = PeriodicDeadline(float(options['cell_diagnostics_interval_seconds']), clock=monotonic)
        self.last_stat = self.last_info = 0.0
        self.bms_stat, self.module_infos = {}, {}
        self._lock = threading.Lock()
        self.modules = ()
        self.sample_at = None
        self.analysis_key = None
        self.analysis_generation = 0
        self.started = False
        self.application = None

    def bind(self, application):
        if self.application is not None:
            raise ValueError('collector already bound')
        if self.technical_writer.scheduler is not application.scheduler:
            raise ValueError('technical writer must share application scheduler')
        if self.config_history.append_backend is None or self.config_history.append_backend.scheduler is not application.scheduler:
            raise ValueError('confirmed configuration writer required')
        paths = [self.technical_path, self.state_path, self.trend_path, self.incident_path,
                 self.config_history.path, self.position_service.log.path,
                 self.maintenance_service.repository.log.path, self.soc_store.path,
                 self.technical_writer.registry.root]
        if self.config_service is not None:
            if self.config_service.history is not self.config_history:
                raise ValueError('configuration service must use collector provenance')
            paths.append(self.config_service.options_path)
        for path in paths:
            if not Path(path).resolve().is_relative_to(application.root):
                raise ValueError('collector store outside explicit application root')
        from config_history import diagnostic_parameters
        prepared_options = json.loads(application.tail.diagnostics.state(application.tail.name)['options'])
        if diagnostic_parameters(prepared_options) != diagnostic_parameters(self.options):
            raise ValueError('collector options require explicitly matching prepared diagnostic configuration')
        if self.position_service.read_backend is None or self.maintenance_service.repository.read_backend is None:
            raise ValueError('collector services require confirmed prepared metadata readers')
        self.application = application
        self.contract = {'options': self.options, 'paths': [str(Path(p).resolve()) for p in paths],
            'technical_registry': self.technical_writer.registry.config['registry_id'],
            'technical_epoch': self.technical_writer.writer_epoch}
        from main import load_persistent_state
        self.persistent = load_persistent_state(path=self.state_path)
        diagnostics = application.tail.diagnostics
        self.analysis = CellAnalysisWorker(lambda ref: worker_call(application.scheduler, 'analysis',
            lambda: diagnostics.analyse_reference(ref), cancelled=lambda: self.analysis._stop),
            clock=self.monotonic, wall_clock=self.clock)
        self.mqtt = LocalWorkComposition(application.scheduler).mqtt_worker(
            self.publisher.derived_sink().publish_derived_job, wall_clock=self.clock, monotonic=self.monotonic)
        self.publisher.derived_mqtt_worker = self.mqtt
        from maintenance_api import MaintenanceApi
        from position_history_api import PositionHistoryApi
        from application_metadata import PreparedServiceApi
        self.maintenance_api = PreparedServiceApi(MaintenanceApi(self.maintenance_service,
            live_publisher=self.publisher.maintenance_events), application.scheduler)
        self.position_api = PreparedServiceApi(PositionHistoryApi(self.position_service,
            module_count_provider=lambda:int(self.options['module_count'])), application.scheduler)
        return self.contract

    def start(self):
        self.application.require_ready()
        self.config_history.record_if_changed(self.options)
        self.publisher.discovery(int(self.options['module_count']))
        self.analysis.start()
        self.mqtt.start()
        self.started = True

    def analysis_step(self):
        """Submit references to a confirmed pair; never reconstruct raw rings."""
        if not self.started:
            return {'status':'collector_not_started'}
        with self._lock:
            modules, sample_at = self.modules, self.sample_at
        if not modules or sample_at is None:
            return {'status':'awaiting_live_sample'}
        diagnostics = self.application.tail.diagnostics
        state = diagnostics.state(self.application.tail.name)
        if state['work'] or state['bundle'] is None:
            return {'status':'awaiting_confirmed_pair'}
        # The existing scientific method selects maintenance inside the retained
        # sample period. One bounded first page per selected identity supplies
        # its lower bound; do not invent a new lookback or scan old Raw files.
        beginnings = []
        with diagnostics.view(self.application.tail.name, bundle=state['bundle']) as (view, _):
            for module in modules:
                page, _, _ = view.sample_page(module)
                if page:
                    beginnings.append(min(value['timestamp'] for value in page))
        if not beginnings:
            return {'status':'awaiting_confirmed_samples'}
        first = datetime.fromtimestamp(min(beginnings), timezone.utc).isoformat()
        last = datetime.fromtimestamp(sample_at, timezone.utc).isoformat()
        metadata = self.application.composition.research.identity
        from contextlib import nullcontext
        with metadata.capture() if hasattr(metadata, 'capture') else nullcontext():
            events = []
            for event in metadata.maintenance_window(first, last):
                raw = event.to_dict()
                serial = raw.get('module_serial')
                identity_status = 'explicit' if serial else 'unknown'
                if not serial and raw.get('module_number') is not None:
                    serial = metadata.serial_at(int(raw['module_number']), raw['occurred_at'])['physical_serial']
                    identity_status = 'position_history' if serial else 'unknown'
                raw.update(resolved_module_serial=serial, identity_status=identity_status)
                events.append(raw)
        key = (state['bundle'], modules, hashed(encoded(events)))
        if key == self.analysis_key:
            return {'status':'analysis_current'}
        job = diagnostics.begin_analysis(self.application.tail.name, list(modules), maintenance_events=events, bundle=state['bundle'])
        reference = diagnostics.analysis_reference(job)
        self.analysis_generation += 1
        self.analysis.submit(self.analysis_generation, sample_at, reference)
        self.analysis_key = key
        return {'status':'analysis_submitted','pair':state['bundle']}

    def cycle(self):
        """One PWR/stat/info/BAT cycle using the existing methods and deadlines."""
        import main
        from position_history import update_observed_stack, current_presence, history_observation_ready
        from config_ui import record_stable_observed_positions
        app = self.application
        app.require_ready()
        started = self.monotonic()
        self.poll.consume(started)
        self.timing.cycle_started(self.clock(), started)
        options = self.options
        modules = []
        try:
            with app.scheduler.raw_section():
                begin = self.monotonic()
                raw = app.console.command(options['command'])
                self.timing.duration('pwr_request', self.monotonic()-begin)
                pwr_time = self.clock()
                if options.get('raw_log', False):
                    (app.root/'last_raw_pwr.txt').write_text(raw, encoding='utf-8')
                modules = main.parse_pwr(raw, int(options['module_count']))
                for module in modules:
                    module.pwr_sample_at = pwr_time
                main.record_soc_crash_observations(modules, self.detector, self.soc_store,
                                                  self.pending_events, app.identity)
                if modules:
                    status, alarms = main.evaluate(modules, options)
                else:
                    (app.root/'last_unparsed_pwr.txt').write_text(raw, encoding='utf-8')
                    status, alarms = 'critical', [{'level':'critical','code':'communication_or_parser_error',
                        'module':None,'message':'Keine Moduldaten aus der Pylontech-Antwort erkannt.'}]
                main.update_events(status, alarms, self.persistent,
                    event_sink=lambda event: main.append_event(event, path=self.technical_path, append_backend=self.technical_writer),
                    state_sink=lambda value: main.save_persistent_state(value, path=self.state_path), clock=self.clock)
                trends = main.update_trends(modules, options, path=self.trend_path, clock=self.clock)
                incident = main.update_incident_state(status, alarms, options, path=self.incident_path, clock=self.clock)
                main.log_result(modules, status, alarms, bool(options.get('detailed_log', False)))
                now = self.clock()
                if modules and now-self.last_stat >= int(options['bms_stat_interval_seconds']):
                    try:
                        parsed = main.parse_stat(app.console.command('stat'))
                        if parsed: self.bms_stat = parsed
                    except Exception as exc:
                        main.LOG.warning('BMS stat: %s', exc)
                    self.last_stat = now
                if modules and now-self.last_info >= int(options['bms_stat_interval_seconds']):
                    for module in modules:
                        try:
                            parsed = main.parse_info(app.console.command(f'info {module.module}'))
                            if parsed: self.module_infos[module.module] = parsed
                        except Exception as exc:
                            main.LOG.warning('BMS info Modul %s: %s', module.module, exc)
                    self.last_info = now
                update_observed_stack(self.module_infos, present_positions={m.module for m in modules},
                    communication_healthy=bool(modules), expected_module_count=int(options['module_count']),
                    observed_at=now, confirm_history=False)
                if options['cell_diagnostics_enabled'] and modules and self.cell.due():
                    cell_start = self.monotonic()
                    self.timing.cell_started(self.clock(), cell_start, self.cell.next_deadline)
                    skipped = self.cell.consume(cell_start)
                    self.timing.cell_deadline_consumed(self.cell.next_deadline, skipped)
                    samples = app.collect_once(modules, timing=self.timing)
                    if samples:
                        with self._lock:
                            self.sample_at = max(s.timestamp for s in samples)
                    self.timing.cell_finished(self.monotonic()-cell_start)
                    if self.cell.due(): self.cell.consume()
                with self._lock:
                    self.modules = tuple(m.module for m in modules)
            latest = self.analysis.latest()
            def current_serial(position):
                return app.identity.identity_at(position, datetime.fromtimestamp(self.clock(), timezone.utc))[0]
            results = project_latest_analysis(latest, current_serial, now=self.clock)
            self.publisher.publish(modules, status, alarms, options, self.persistent, trends, incident,
                results, self.bms_stat, self.module_infos,
                current_presence(expected_module_count=int(options['module_count']), now=self.clock()),
                analysis_context=latest, include_derived=False)
            if latest is not None and results:
                identities = tuple(sorted((position, result.get('physical_module_serial')) for position, result in results.items()))
                self.mqtt.submit({'key':(latest['generation'],latest.get('config_id'),identities,self.publisher._derived_reconnect_epoch),
                    'generation':latest['generation'], 'config_id':latest.get('config_id'), 'identities':identities,
                    'reconnect_epoch':self.publisher._derived_reconnect_epoch,
                    'positions':tuple(m.module for m in modules if m.module in results),
                    'results':results,'created_at':self.clock()})
            update_observed_stack(self.module_infos, present_positions={m.module for m in modules},
                communication_healthy=bool(modules), expected_module_count=int(options['module_count']),
                observed_at=now, confirm_history=True)
            if history_observation_ready():
                try:
                    record_stable_observed_positions(position_service=self.position_service,
                        maintenance_service=self.maintenance_service,
                        clock=lambda: datetime.fromtimestamp(self.clock(), timezone.utc))
                except Exception as exc:
                    main.LOG.warning('Positionshistorie konnte nicht fortgeschrieben werden: %s', exc)
            if app.auxiliary is not None:
                app.auxiliary.publish_current()
            return {'status':status, 'modules':len(modules)}
        except Exception:
            update_observed_stack({}, present_positions=set(), communication_healthy=False,
                expected_module_count=int(options['module_count']), confirm_history=False)
            app.console.close()
            raise
        finally:
            self.timing.cycle_finished(self.monotonic()-started)
            if self.poll.due(): self.poll.consume()

    def run(self, stop):
        import main
        while not stop.is_set():
            if self.application.failure:
                raise RuntimeError(self.application.failure)
            try:
                self.cycle()
            except Exception as exc:
                main.LOG.exception('Abfrage fehlgeschlagen: %s', exc)
            stop.wait(self.poll.delay())

    def close(self):
        errors = []
        for callback in (self.analysis.stop, self.mqtt.stop,
                         self.application.console.close, self.publisher.close):
            try:
                if callback() is False:
                    raise RuntimeError('Collector worker did not stop')
            except Exception as exc:
                errors.append(exc)
        self.started = False
        if errors:
            raise ExceptionGroup('Collector shutdown failed', errors)
