"""Concrete, explicitly gated candidate factory over already prepared stores.

No default startup selects this factory. The layout identifies every store;
construction neither discovers history nor initializes/migrates databases.
Transports are explicitly supplied by the owner and start only via application.start.
"""
from datetime import datetime, timezone
from pathlib import Path
import json

from projection_repository import Unavailable
from history_repository_preparation import local_composition
from raw_registry import RawRegistry
from application_writer import ApplicationWriter
from application_tail import ApplicationTail
from application_catalog import CatalogTail
from diagnostic_repository import DiagnosticRepository
from publication_window_index import PublicationWindowIndex
from store_migration_manifest import MigrationManifest
from application_metadata import (MetadataTail, HeadMetadataReader, ConfirmedIdentity,
                                  ConfirmedPositionReader, ConfirmedMaintenanceReader)
from history_epoch_adapter import EpochLogConsumer
from source_catalog import SourceCatalog
from raw_registry_catalog import RegistryCatalogConsumer
from work_scheduler import WorkScheduler


class AuxiliaryAcquisition:
    def __init__(self, *, bus_writer=None, bus_reader=None, hycube=None, contract=None, bus_mqtt=None, positions=None, clock=None):
        self.bus_writer, self.bus_reader, self.hycube = bus_writer, bus_reader, hycube
        self.contract = contract or {}
        self.bus_mqtt, self.positions, self.clock = bus_mqtt, positions, clock

    def start(self):
        for component in (self.bus_writer, self.bus_reader, self.hycube):
            if component is not None:
                component.start()

    def publish_current(self):
        if self.bus_reader is not None:
            from rs485_identity import project_current_management
            management=project_current_management(self.bus_reader.management(),self.bus_reader.identities(),
                position_reader=self.positions,at=self.clock())
            self.bus_mqtt.publish(self.bus_reader.status(),management,self.bus_writer.status())

    def close(self):
        errors=[]
        for component in (self.bus_reader, self.bus_writer, self.hycube):
            if component is not None:
                try:
                    component.stop()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup('Auxiliary acquisition shutdown failed',errors)


def open_candidate(*, root, layout, console, publisher, supervisor_api, cursor_secret,
                   wall_clock, monotonic, parse_bat, bus_port_resolver=None,
                   bus_serial_opener=None, hycube_opener=None, inspect_binding=False):
    """Reopen exact prepared paths, compose real services, then enforce manifest.

    Layout is migration output, not inferred from directories or current time.
    The caller retains transport ownership if construction fails before return.
    """
    if layout.get('contract') != 'guardian-prepared-candidate-1':
        raise ValueError('explicit candidate contract required')
    # Mapping order is not part of the persisted JSON contract. Canonicalize
    # before constructing ordered store/worker bindings, including offline bind.
    layout=json.loads(json.dumps(layout,sort_keys=True,allow_nan=False))
    root=Path(root).resolve()
    def path(value, *, existing=False):
        result=(root/value).resolve()
        if not result.is_relative_to(root):
            raise ValueError('candidate path outside explicit root')
        if existing and not result.exists():
            raise Unavailable('prepared candidate path missing: '+str(result))
        return result
    paths=layout['paths']
    preparation=local_composition(repository_path=path(paths['repository'],existing=True),
        catalog_path=path(paths['cell_catalog'],existing=True),epoch_path=path(paths['epochs'],existing=True))
    manifest=MigrationManifest(preparation.repository,layout['manifest'])
    if not inspect_binding and manifest.state()['status']!='complete':
        raise Unavailable('explicit candidate migration incomplete')
    scheduler=WorkScheduler(**layout['scheduler'])
    writers={}
    for name,spec in layout['writers'].items():
        registry=RawRegistry(path(spec['registry'],existing=True))
        writers[name]=ApplicationWriter(registry,path(spec['root'],existing=True),
            stream=spec['stream'],writer_epoch=spec['epoch'],scheduler=scheduler)
    cell=writers['cell'];metadata_writer=writers['metadata']
    options_path=path(paths['options'],existing=True)
    with options_path.open('rb') as handle:options_raw=handle.read(65537)
    if len(options_raw)>65536:raise Unavailable('candidate options exceed budget')
    from config_ui import DEFAULTS
    options={**DEFAULTS,**json.loads(options_raw)}
    metadata_spec=layout['metadata']
    metadata_tail=MetadataTail(preparation.repository,metadata_writer.registry,
        head=metadata_spec['head'],mapping=metadata_spec['mapping'],origin_proof=metadata_spec['origin'])
    metadata_tail.current()
    metadata=HeadMetadataReader(preparation.repository,metadata_spec['head'])
    epoch=EpochLogConsumer(metadata_writer.registry,preparation.epochs)
    epoch.cursor()
    from config_history import ConfigHistory
    from position_history import PositionHistoryLog, PositionHistoryService
    from maintenance import MaintenanceEventLog
    from maintenance_service import MaintenanceRepository, MaintenanceService
    now=lambda:datetime.fromtimestamp(wall_clock(),timezone.utc)
    maintenance_path=path(paths['maintenance'],existing=True)
    position_path=path(paths['positions'],existing=True)
    maintenance=MaintenanceService(MaintenanceRepository(
        MaintenanceEventLog(maintenance_path,append_backend=metadata_writer),
        read_backend=ConfirmedMaintenanceReader(metadata_tail,metadata,metadata_writer,maintenance_path)),clock=now)
    positions=PositionHistoryService(PositionHistoryLog(position_path,append_backend=metadata_writer),
        maintenance,clock=now,read_backend=ConfirmedPositionReader(metadata_tail,metadata,metadata_writer,position_path))
    config=ConfigHistory(path(paths['config'],existing=True),append_backend=metadata_writer,clock=now)
    from prepared_config_service import PreparedConfigService
    config_service=PreparedConfigService(options_path=options_path,config_history=config,supervisor_api=supervisor_api)
    from prepared_collector import PreparedCollector
    from soc_crash_events import SocCrashEventStore
    collector=PreparedCollector(options=options,publisher=publisher,config_history=config,
        position_service=positions,maintenance_service=maintenance,
        soc_store=SocCrashEventStore(path(paths['soc'])),technical_writer=metadata_writer,
        technical_path=path(paths['technical']),state_path=path(paths['state']),
        trend_path=path(paths['trends']),incident_path=path(paths['incidents']),
        wall_clock=wall_clock,monotonic=monotonic,config_service=config_service)
    catalogs={};tails=[];consumers={}
    for name,spec in layout['catalogs'].items():
        catalog=SourceCatalog(path(spec['path'],existing=True))
        consumer=RegistryCatalogConsumer(writers[spec['writer']].registry,catalog,
            record_format=spec['format'],source_formats=spec.get('source_formats'))
        item=CatalogTail(consumer,name=spec['tail']);item.state();consumer.cursor()
        catalogs[name]=catalog;consumers[name]=consumer;tails.append(item)
    policy_reader=None;hycube=None;bus_writer=None;bus_reader=None
    from confirmed_prefix_reader import ConfirmedPrefixReader
    if options.get('hycube_evidence_enabled',False):
        from prepared_policy_reader import PreparedPolicyReader
        from hycube_evidence import HycubePolicyHistory,HycubeEvidenceWriter,collector_from_options
        from hycube_projection import HycubeProjectionStore
        policy_reader=PreparedPolicyReader(catalogs['policy'],
            prefix_reader=ConfirmedPrefixReader(writers['policy'].registry),consumer=consumers['policy'])
        policy=HycubePolicyHistory(writers['policy'].root,append_backend=writers['policy'],window_backend=policy_reader)
        projection=HycubeProjectionStore(writers['hycube_projection'].root,
            append_backend=writers['hycube_projection'],inventory_path=path(paths['hycube_inventory'],existing=True))
        hycube=collector_from_options(options,writers['hycube'].root,writers['policy'].root,
            writers['hycube_projection'].root,writer=HycubeEvidenceWriter(writers['hycube'].root,append_backend=writers['hycube']),
            policy_writer=policy,projection_store=projection,opener=hycube_opener,clock=wall_clock,monotonic=monotonic)
    if options.get('rs485_sniffer_enabled',False):
        if bus_port_resolver is None or bus_serial_opener is None:
            raise ValueError('candidate passive bus requires explicit port and transport')
        from rs485_evidence import Rs485EvidenceWriter,Rs485EvidencePipeline
        from main import create_rs485_reader
        bus_writer=Rs485EvidenceWriter(writers['rs485'].root,append_backend=writers['rs485'])
        pipeline=Rs485EvidencePipeline(bus_writer,wall_clock=wall_clock,position_reader=positions)
        bus_reader=create_rs485_reader(options,console.port,pipeline,port_resolver=bus_port_resolver,
            serial_opener=bus_serial_opener,wall_clock=wall_clock,monotonic=monotonic)
        from prepared_rs485_identity import restore_identities
        restored=restore_identities(catalogs['rs485'],consumers['rs485'])
        bus_reader.restore_identities(restored)
        pipeline.restore_identities(restored)

    diagnostics=DiagnosticRepository(preparation.repository,preparation.catalog,preparation.epochs)
    index=PublicationWindowIndex(preparation.repository)
    tail=ApplicationTail(preparation,diagnostics,index,name=layout['diagnostics'])
    from research_api import ResearchPaths
    research_paths=ResearchPaths(**{name:None if value is None else path(value)
                                  for name,value in layout['research_paths'].items()})
    from local_history_composition import compose_local_history
    composition=compose_local_history(paths=research_paths,cell_catalog=preparation.catalog,
        metadata=metadata,publication_index=index,canonical_repository=preparation.canonical,
        display_repository=preparation.display,streams=layout['streams'],scheduler=scheduler,
        cursor_secret=cursor_secret,cell_registry=cell.registry,
        hycube_catalog=catalogs.get('hycube'),hycube_registry=writers['hycube'].registry if 'hycube' in catalogs else None,
        rs485_catalog=catalogs.get('rs485'),rs485_registry=writers['rs485'].registry if 'rs485' in catalogs else None,
        policy_reader=policy_reader,current_config_provider=lambda:options)
    from daily_indexed_inputs import DailyIndexedInputs
    from daily_diagnostics import DailyDiagnosticSources
    from daily_diagnostic_worker import DailyDiagnosticWorker
    daily_spec=layout['daily']
    daily_worker=None
    if daily_spec is not None:
        daily_catalogs={key:preparation.catalog if name=='cell' else catalogs[name] if name is not None else None
                        for key,name in daily_spec['catalogs'].items()}
        daily_registries={key:cell.registry if name=='cell' else
            writers[layout['catalogs'][name]['writer']].registry if name is not None else None
            for key,name in daily_spec['catalogs'].items()}
        daily_worker=DailyDiagnosticWorker(DailyDiagnosticSources(input_backend=DailyIndexedInputs(daily_catalogs,registries=daily_registries)),
            research_paths.daily_diagnostics,scheduler=scheduler,clock=now,**daily_spec['options'])
    from rs485_mqtt import Rs485MqttProjection
    from cell_history import CellHistoryWriter
    from prepared_application import PreparedApplication
    auxiliary=AuxiliaryAcquisition(bus_writer=bus_writer,bus_reader=bus_reader,hycube=hycube,
        contract={'layout':layout,'enabled':{'rs485':bus_reader is not None,'hycube':hycube is not None}},
        bus_mqtt=Rs485MqttProjection(publisher,wall_clock=wall_clock) if bus_reader is not None else None,positions=positions,clock=wall_clock)
    application=PreparedApplication(root=root,manifest=manifest,preparation=preparation,tail=tail,
        composition=composition,scheduler=scheduler,cell_writer=CellHistoryWriter(cell.root,append_backend=cell),
        identity=ConfirmedIdentity(metadata_tail,metadata),console=console,parse_bat=parse_bat,
        wall_clock=wall_clock,poll_seconds=layout['poll_seconds'],metadata_tail=metadata_tail,
        epoch_consumers=(epoch,),catalog_tails=tails,collector=collector,auxiliary=auxiliary,daily_worker=daily_worker)
    from prepared_maintenance_mutation import PreparedMaintenanceMutation
    metadata_subscribers=[epoch]+[item for item in tails
        if item.consumer.registry_id==metadata_writer.registry.config['registry_id']]
    callbacks=[metadata_tail.step,lambda:epoch.step(expected_cursor=epoch.cursor(),max_records=8)]
    callbacks.extend(item.step for item in metadata_subscribers if hasattr(item,'consumer'))
    progress_turn=[0]
    def mutation_progress():
        from research_budget import budget_lock
        with budget_lock(application._step_lock):
            callback=callbacks[progress_turn[0]]
            progress_turn[0]=(progress_turn[0]+1)%len(callbacks)
            return callback()
    mutation=PreparedMaintenanceMutation(maintenance.repository.log,maintenance.repository.read_backend,
        subscribers=metadata_subscribers,progress=mutation_progress)
    maintenance.repository.log.mutation_backend=mutation
    application.maintenance_mutation=mutation
    application.steps.append(mutation.step)
    import hashlib
    from raw_registry import encoded
    application.binding=hashlib.sha256(encoded({'application':application.binding,
        'maintenance_mutation':'guardian-maintenance-delete-1','retired_ledger':str(mutation.ledger)})).hexdigest()
    if not inspect_binding:
        application.require_ready()
    return application


class DeferredPublisher:
    """Keep MQTT disconnected until the application has passed its start gate."""
    def __init__(self, factory):
        self.factory=factory
        self.instance=None
        self.derived_mqtt_worker=None

    def discovery(self, count):
        if self.instance is None:
            self.instance=self.factory()
            self.instance.derived_mqtt_worker=self.derived_mqtt_worker
        return self.instance.discovery(count)

    def derived_sink(self):
        from types import SimpleNamespace
        return SimpleNamespace(publish_derived_job=lambda job:self.instance.derived_sink().publish_derived_job(job))

    @property
    def maintenance_events(self):
        from types import SimpleNamespace
        return SimpleNamespace(publish_if_live=lambda event:self.instance.maintenance_events.publish_if_live(event))

    @property
    def _derived_reconnect_epoch(self):
        return 0 if self.instance is None else self.instance._derived_reconnect_epoch

    def __getattr__(self,name):
        if self.instance is None:
            raise AttributeError('MQTT is not active: '+name)
        return getattr(self.instance,name)

    def close(self):
        if self.instance is not None:
            self.instance.close()


def main(argv=None):
    """Separate opt-in entry; never called from the existing default run script.

    --check reopens the prepared candidate and checks its gate without opening
    serial ports, MQTT, HTTP listeners or collector threads. --run additionally
    requires an explicit complete layout and owns all component shutdowns.
    """
    import argparse
    import signal
    import time
    import main as legacy
    from rs485_sniffer import open_passive_serial
    from config_ui import _api
    parser=argparse.ArgumentParser(description='Explicit prepared Guardian candidate')
    parser.add_argument('--root',required=True)
    parser.add_argument('--layout',required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true')
    mode.add_argument('--run',action='store_true')
    args=parser.parse_args(argv)
    # Same sealed build contract as the default application. Validate before
    # opening stores (which may recover a durable intent), ports or services.
    source_commit=legacy.require_source_commit()
    legacy.LOG.info('Guardian Battery %s; source_commit=%s; diagnostic_engine=%s; prepared candidate',
        legacy.GUARDIAN_VERSION,source_commit,legacy.DIAGNOSTIC_ENGINE_VERSION)
    root=Path(args.root).resolve()
    layout_path=Path(args.layout).resolve()
    if not layout_path.is_relative_to(root):raise ValueError('layout outside candidate root')
    with layout_path.open('rb') as handle:raw=handle.read(65537)
    if len(raw)>65536:raise ValueError('candidate layout exceeds budget')
    layout=json.loads(raw)
    runtime=layout['runtime']
    options_path=(root/layout['paths']['options']).resolve()
    secret_path=(root/runtime['cursor_secret_file']).resolve()
    if not options_path.is_relative_to(root) or not secret_path.is_relative_to(root):
        raise ValueError('candidate input outside explicit root')
    with options_path.open('rb') as handle:raw_options=handle.read(65537)
    if len(raw_options)>65536:raise ValueError('options exceed budget')
    from config_ui import DEFAULTS
    options={**DEFAULTS,**json.loads(raw_options)}
    with secret_path.open('rb') as handle:secret=handle.read(257)
    if not 16<=len(secret)<=256:raise ValueError('bounded explicit cursor secret required')
    console_port=runtime['console_port']
    if not Path(console_port).is_absolute():raise ValueError('explicit absolute console port required')
    console=legacy.PylontechConsole(console_port,int(options['baudrate']),float(options['command_timeout_seconds']))
    publisher=DeferredPublisher(lambda:legacy.Mqtt(options))
    app=None
    try:
        app=open_candidate(root=root,layout=layout,console=console,publisher=publisher,
            supervisor_api=_api,cursor_secret=secret,wall_clock=time.time,monotonic=time.monotonic,
            parse_bat=legacy.parse_bat,bus_port_resolver=lambda:runtime['bus_port'],bus_serial_opener=open_passive_serial)
        if args.check:
            print(json.dumps(app.readiness(),sort_keys=True))
            return
        for name in (signal.SIGINT,signal.SIGTERM):
            signal.signal(name,lambda *unused:app.stop.set())
        app.start(http_port=runtime['http_port'],bind_host=runtime['bind_host'])
        app.collector.run(app.stop)
    finally:
        if app is not None:app.close()
        else:
            console.close()
            publisher.close()


if __name__=='__main__':
    main()
