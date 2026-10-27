"""The production-module factory reopens real prepared synthetic stores."""
from pathlib import Path
import json
from types import SimpleNamespace
import pytest
from prepared_candidate import open_candidate
from store_migration_manifest import MigrationManifest
from test_prepared_application import setup,prepare
from test_prepared_collector import factory,Publisher,pwr
from test_cell_acquisition import rows
from projection_repository import Unavailable


class CandidatePublisher(Publisher):
    discovery_enabled=False
    prefix='synthetic'
    def _publish(self,*args,**kwargs):self.live.append((args,kwargs))
    def state(self,*args):self.live.append(('state',args))
    def attributes(self,*args):self.live.append(('attributes',args))


def candidate_fixture(tmp_path, customize=None):
    env,old,clock,calls=setup(tmp_path,collector_factory=factory)
    prepare(env,old)
    collector=old.collector
    options=tmp_path/'options.json';options.write_text(json.dumps(collector.options))
    rel=lambda path:str(Path(path).relative_to(tmp_path))
    def writer(backend):return dict(registry=rel(backend.registry.root),root=rel(backend.root),stream=backend.stream,epoch=backend.writer_epoch)
    from dataclasses import fields
    layout=dict(contract='guardian-prepared-candidate-1',manifest='factory-migration',
        diagnostics=old.tail.name,streams=['serial-a'],scheduler={},poll_seconds=.001,
        writers={'cell':writer(old.cell_writer.append_backend),'metadata':writer(collector.technical_writer)},catalogs={},daily=None,
        metadata={'head':old.metadata_tail.head,'mapping':old.metadata_tail.mapping,'origin':old.metadata_tail.origin_proof},
        research_paths={field.name:None if getattr(old.composition.research.paths,field.name) is None
                        else rel(getattr(old.composition.research.paths,field.name)) for field in fields(old.composition.research.paths)},
        paths=dict(repository=rel(env['repo'].path),cell_catalog=rel(env['catalog'].path),epochs=rel(old.preparation.epochs.path),
            options=rel(options),maintenance=rel(collector.maintenance_service.repository.log.path),positions=rel(collector.position_service.log.path),
            config=rel(collector.config_history.path),soc=rel(collector.soc_store.path),technical=rel(collector.technical_path),
            state=rel(collector.state_path),trends=rel(collector.trend_path),incidents=rel(collector.incident_path)))
    transport_overrides={} if customize is None else customize(tmp_path,layout,old)
    # Explicit test preparation, outside runtime factory. No invented Raw origin.
    manifest=MigrationManifest(env['repo'],layout['manifest']);manifest.initialize()
    def create(**kw):
        return open_candidate(root=tmp_path,layout=layout,console=SimpleNamespace(port=str(tmp_path/'console'),command=lambda command:pwr() if command=='pwr' else command,close=lambda:None),
            publisher=CandidatePublisher(),supervisor_api=lambda *a:pytest.fail('unexpected supervisor call'),
            cursor_secret=b'synthetic-test',wall_clock=lambda:clock[0],monotonic=lambda:clock[0],parse_bat=rows,**transport_overrides,**kw)
    candidate=create(inspect_binding=True)
    state=env['catalog'].progress('raw','r1')
    manifest.begin([(env['catalog'],dict(origin='synthetic frozen scope',source='raw',revision='r1',frontier=state['frontier'],receipt=state['receipt']))],
        provenance={'application_binding':candidate.binding},rollback={'synthetic_preserved_originals':True})
    for _ in range(5):
        if manifest.step(env['catalog'],expected_version=manifest.state()['version'])['complete']:break
    return create,layout,clock,candidate


def test_concrete_factory_reopens_and_starts_existing_collector_and_http(tmp_path,monkeypatch):
    import config_ui
    from urllib.request import urlopen
    create,layout,clock,inspected=candidate_fixture(tmp_path)
    inspected.close()
    app=create()
    monkeypatch.setattr(config_ui.Handler,'_ingress_allowed',lambda self:True)
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('runtime history discovery'))
    monkeypatch.setattr(config_ui,'_read_options',lambda:pytest.fail('default options path'))
    try:
        app.start(http_port=0)
        assert app.collector.cycle()['modules']==1
        url='http://127.0.0.1:'+str(app.server.server_port)+'/api/config'
        with urlopen(url,timeout=5) as response:body=json.load(response)
        assert body['current']['module_count']==1
        assert body['config_id']
        assert app.readiness()['state']=='ready_local_candidate'
    finally:app.close()
    assert not app.thread.is_alive()
    assert app.collector.publisher.closed
    reopened=create()
    assert reopened.binding==app.binding
    reopened.close()


def test_factory_rejects_changed_binding_before_transport_start(tmp_path):
    create,layout,clock,inspected=candidate_fixture(tmp_path)
    inspected.close()
    layout['poll_seconds']=.02
    with pytest.raises(Unavailable,match='binding_mismatch'):create()


def test_concrete_factory_owns_multisource_start_stop_and_restart(tmp_path,monkeypatch):
    import threading
    import time
    from raw_registry import RawRegistry
    from source_catalog import SourceCatalog
    from raw_registry_catalog import RegistryCatalogConsumer
    from application_catalog import CatalogTail
    from test_rs485_evidence import frame,management
    from test_hycube_evidence import Response,PAYLOAD
    wires=[];requests=[]
    class Wire:
        def __init__(self):
            self.closed=threading.Event()
            self.payload=b''.join(value.raw_frame for value in (
                frame(code=0x93,info=b'\x02'),frame(code=0,info=b'\x02Y225004C32250226'),
                frame(code=0x92),frame(code=0,info=management())))
        def read(self,size):
            if self.payload:
                result,self.payload=self.payload,b''
                return result
            self.closed.wait(.01);return b''
        def close(self):self.closed.set()
        def write(self,*a):pytest.fail('passive wire write')
    class Transport:
        def open(self,request,timeout):
            requests.append(request.full_url)
            return Response(PAYLOAD if request.full_url.endswith('/data_row/') else
                dict(normalMode=82,bufferMode=3,emergency=10,batProtection=5))
    def customize(root,layout,old):
        options_path=root/layout['paths']['options']
        options=json.loads(options_path.read_text());options.update(rs485_sniffer_enabled=True,
            hycube_evidence_enabled=True,hycube_base_url='http://localhost')
        options_path.write_text(json.dumps(options))
        for name,record_format in [('rs485','rs485'),('hycube','hycube_system_observation'),
                                   ('policy','hycube_policy_observation'),('hycube_projection','hycube_history_projection')]:
            registry=RawRegistry.create(root/(name+'-registry'))
            (root/name).mkdir()
            catalog=SourceCatalog(root/(name+'-catalog'))
            CatalogTail(RegistryCatalogConsumer(registry,catalog,record_format=record_format),name=name).initialize()
            layout['writers'][name]=dict(registry=name+'-registry',root=name,stream=name,epoch='synthetic')
            layout['catalogs'][name]=dict(path=name+'-catalog',writer=name,format=record_format,tail=name)
        for kind in ('position','config','maintenance'):
            name='daily_'+kind
            catalog=SourceCatalog(root/(name+'-catalog'))
            formats={source: name if mapped==kind else None for source,mapped in old.metadata_tail.mapping.items()}
            consumer=RegistryCatalogConsumer(old.collector.technical_writer.registry,catalog,
                record_format=name,source_formats=formats)
            prepared_tail=CatalogTail(consumer,name=name)
            prepared_tail.initialize()
            for _ in range(100):
                if prepared_tail.step().get('status')=='caught_up':break
            else:pytest.fail('explicit initial metadata preparation did not complete')
            layout['catalogs'][name]=dict(path=name+'-catalog',writer='metadata',format=name,source_formats=formats,tail=name)
        layout['daily']={'catalogs':{'cell':'cell','rs485':'rs485','position_history':'daily_position',
            'config_history':'daily_config','maintenance_history':'daily_maintenance'},
            'options':{'automatic_history_days':1,'initial_catchup_days':1,'late_data_days':1,'check_interval_seconds':1}}
        layout['research_paths'].update(rs485_history='rs485',hycube_history='hycube',hycube_projection='hycube_projection',hycube_policy='policy')
        inventory=root/'hycube-inventory.json'
        inventory.write_text(json.dumps(dict(directory=str(root/'hycube_projection'),origin={'synthetic':True},
            counts=dict(projection_days=0,projection_bytes=0,complete_days=0,open_days=0,invalid_days=0))))
        layout['paths']['hycube_inventory']=inventory.name
        def opener(*args,**kwargs):
            wire=Wire();wires.append(wire);return wire
        return dict(bus_port_resolver=lambda:str(root/'wire'),bus_serial_opener=opener,hycube_opener=Transport())
    create,layout,clock,inspected=candidate_fixture(tmp_path,customize)
    inspected.close()
    app=create()
    try:
        app.start()
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            assert app.failure is None,app.failure
            if app.auxiliary.bus_writer.status()['records_written']>=4 and len(requests)>=2:
                break
            time.sleep(.01)
        else:pytest.fail('auxiliary acquisition did not finish synthetic first round')
        assert app.auxiliary.bus_reader.identities()[2]['serial_string']=='Y225004C32250226'
        assert app.auxiliary.hycube.status()['failures']==0
        app.collector.cycle()
        from datetime import datetime,timezone
        day=datetime.fromtimestamp(clock[0],timezone.utc).date().isoformat()
        clock[0]+=86400
        # Finish this finite input batch before testing unchanged warm reuse.
        # A new late observation legitimately invalidates the cached result.
        app.auxiliary.close()
        # Capture a fixed as-of frontier; continuous acquisition need not reach
        # global silence. Every selected confirmed prefix must be consumed.
        frontiers={item.name:item.consumer.registry._head()['seq'] for item in app.catalog_tails}
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            assert app.failure is None,app.failure
            if all(item.consumer.cursor()[0]>frontiers[item.name] and not item.catalog.status()['pending']
                   for item in app.catalog_tails) and app.preparation.status()['state']=='caught_up':
                break
            time.sleep(.25)
        else:pytest.fail(str({'preparation':app.preparation.status(),'catalogs':[(item.name,item.state(),item.catalog.status()) for item in app.catalog_tails],'daily':app.daily_worker.state,'scheduler':app.scheduler.status()}))
        result=app.daily_worker.output_root/'daily'/day/'index.json'
        deadline=time.monotonic()+45
        while (not result.exists() or app.daily_worker.state.get('currently_running_date') is not None
               or day not in app.daily_worker.state.get('prepared_inputs',{})
               or app.daily_worker.state['prepared_inputs'][day]['guard']!=app.daily_worker._prepared_guard(day)) and time.monotonic()<deadline:
            assert app.failure is None,app.failure
            time.sleep(.05)
        assert result.exists() and day in app.daily_worker.state.get('prepared_inputs',{}) and app.daily_worker.state.get('currently_running_date') is None,app.daily_worker.state
        assert app.daily_worker.sources.input_backend.metrics['cell']['decodes']>0
        assert app.daily_worker.sources.input_backend.metrics['rs485']['decodes']>0
        assert app.daily_worker.sources.input_backend.metrics['position_history']['decodes']>0

    finally:app.close()
    assert all(wire.closed.is_set() for wire in wires)
    # Acquisition remains active while the fake clock crosses midnight. Its
    # next scheduled poll may have arrived before shutdown; every actual Raw
    # day must have exactly the matching projected day, with no invented day.
    raw_days={path.stem for path in app.auxiliary.hycube.writer.directory.glob('*.jsonl')}
    projection_days={path.stem for path in app.auxiliary.hycube.projection_store.directory.glob('*.jsonl')}
    assert raw_days==projection_days
    assert day in raw_days and raw_days<={day,datetime.fromtimestamp(clock[0],timezone.utc).date().isoformat()}
    assert app.auxiliary.hycube.projection_store.status()['projection_days']==len(raw_days)
    reopened=create()
    assert reopened.binding==app.binding
    assert reopened.auxiliary.hycube.projection_store.status()['projection_bytes']>0
    reopened.close()
    # New interpreter: same actual application factory, no fixture recreation.
    import subprocess,sys,os
    layout_file=tmp_path/'restart-layout.json';layout_file.write_text(json.dumps(layout))
    command='import sys;sys.path.insert(0,sys.argv[1]);from test_prepared_candidate import fresh_process_probe;fresh_process_probe(sys.argv[2],sys.argv[3],float(sys.argv[4]))'
    result=subprocess.run([sys.executable,'-c',command,str(Path(__file__).parent),str(tmp_path),str(layout_file),str(clock[0])],
        capture_output=True,text=True,timeout=60,env=dict(os.environ))
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    evidence=json.loads(result.stdout.splitlines()[-1])
    assert evidence['warm_raw_bytes']==0
    assert evidence['warm_bundles_unchanged'] and evidence['warm_daily_unchanged']
    assert evidence['restored_identity']=='Y225004C32250226'
    assert evidence['started_daily_raw_decodes']==0
    assert evidence['new_cell_records']==1 and evidence['completed_frontiers']


def test_runtime_gate_checks_replacement_without_opening_unrelated_catalogs(tmp_path,monkeypatch):
    create,layout,clock,app=candidate_fixture(tmp_path)
    # Start has already checked the full binding. Select the runtime gate while
    # executing one real bounded step synchronously for deterministic injection.
    app.require_ready()
    app.thread=object()
    try:
        monkeypatch.setattr(app,'require_ready',lambda:pytest.fail('repeated global readiness scan'))
        app.step()
        original=app.preparation.catalog.path
        original.rename(original.with_suffix('.preserved'))
        original.write_bytes(b'explicit synthetic replacement')
        with pytest.raises(Unavailable,match='store replaced'):app.step()
    finally:
        app.thread=None
        app.close()


def test_explicit_cli_check_never_opens_transports_or_services(tmp_path,monkeypatch,capsys):
    import main as legacy
    import config_ui
    from prepared_candidate import main
    def customize(root,layout,old):
        options=root/layout['paths']['options']
        values=json.loads(options.read_text());values.update(baudrate=115200,command_timeout_seconds=1)
        options.write_text(json.dumps(values))
        (root/'cursor-key').write_bytes(b'synthetic-only-cursor-key')
        layout['runtime']=dict(console_port=str(root/'console'),bus_port=str(root/'wire'),
            cursor_secret_file='cursor-key',http_port=0,bind_host='127.0.0.1')
        return {}
    create,layout,clock,app=candidate_fixture(tmp_path,customize)
    app.close()
    target=tmp_path/'candidate.json';target.write_text(json.dumps(layout))
    monkeypatch.setattr(legacy,'Mqtt',lambda *a:pytest.fail('check connected MQTT'))
    monkeypatch.setattr(legacy.serial,'Serial',lambda *a,**k:pytest.fail('check opened serial port'))
    monkeypatch.setattr(config_ui,'_api',lambda *a:pytest.fail('check called Supervisor'))
    monkeypatch.setattr(config_ui,'start_config_server',lambda *a,**k:pytest.fail('check started HTTP'))
    monkeypatch.setattr(legacy,'require_source_commit',lambda:'a'*40)
    main(['--root',str(tmp_path),'--layout',str(target),'--check'])
    output=json.loads(capsys.readouterr().out)
    assert output['state']=='ready_local_candidate' and not output['production_approved']


def test_deferred_mqtt_preserves_worker_and_maintenance_contract():
    from prepared_candidate import DeferredPublisher
    events=[]
    instance=SimpleNamespace(discovery=lambda count:events.append(('discovery',count)),
        maintenance_events=SimpleNamespace(publish_if_live=lambda event:events.append(('maintenance',event))),
        derived_sink=lambda:SimpleNamespace(publish_derived_job=lambda job:events.append(('derived',job))),
        _derived_reconnect_epoch=3,close=lambda:events.append(('closed',)))
    publisher=DeferredPublisher(lambda:(events.append(('connect',)),instance)[1])
    maintenance=publisher.maintenance_events;sink=publisher.derived_sink()
    publisher.derived_mqtt_worker=object()
    assert events==[] and publisher._derived_reconnect_epoch==0
    publisher.discovery(6);maintenance.publish_if_live('event');sink.publish_derived_job('job')
    assert instance.derived_mqtt_worker is publisher.derived_mqtt_worker
    assert publisher._derived_reconnect_epoch==3
    publisher.close()
    assert events==[('connect',),('discovery',6),('maintenance','event'),('derived','job'),('closed',)]


def fresh_process_probe(root,layout_file,clock):
    """Executed only in the dedicated child interpreter, with synthetic paths."""
    import hashlib,time,threading
    from raw_registry import RawRegistry
    from test_hycube_evidence import Response,PAYLOAD
    root=Path(root);layout=json.loads(Path(layout_file).read_text())
    requests=[]
    class SilentWire:
        def read(self,size):time.sleep(.01);return b''
        def close(self):pass
        def write(self,*a):raise AssertionError('passive transport wrote')
    class Transport:
        def open(self,request,timeout):
            requests.append(request.full_url)
            return Response(PAYLOAD if request.full_url.endswith('/data_row/') else
                dict(normalMode=82,bufferMode=3,emergency=10,batProtection=5))
    app=open_candidate(root=root,layout=layout,
        console=SimpleNamespace(port=str(root/'console'),command=lambda command:pwr() if command=='pwr' else command,close=lambda:None),
        publisher=CandidatePublisher(),supervisor_api=lambda *a:(_ for _ in ()).throw(AssertionError('Supervisor access')),
        cursor_secret=b'synthetic-test',wall_clock=lambda:clock+60,monotonic=time.monotonic,parse_bat=rows,
        bus_port_resolver=lambda:str(root/'wire'),bus_serial_opener=lambda *a:SilentWire(),hycube_opener=Transport())
    def caught_up():
        from raw_registry_catalog import RegistryCatalogConsumer
        cell_registry=app.cell_writer.append_backend.registry
        cell_consumer=RegistryCatalogConsumer(cell_registry,app.preparation.catalog)
        with app.preparation.repository._connection() as db:
            latest=db.execute('SELECT COALESCE(MAX(id),0) FROM preparation_job').fetchone()[0]
        tail_state=app.tail.state()
        return (cell_consumer.cursor()[0]>cell_registry._head()['seq']
            and not app.preparation.catalog.status()['pending']
            and tail_state['job'] is None and tail_state['after_job']>=latest
            and app.preparation.status()['state']=='caught_up' and all(
                item.consumer.cursor()[0]>item.consumer.registry._head()['seq'] and not item.catalog.status()['pending']
                for item in app.catalog_tails))
    # Shutdown can have confirmed the final projection flush after the old
    # background thread stopped. Consume only that explicit pending suffix.
    for _ in range(400):
        app.step()
        if caught_up():break
    else:raise AssertionError('restart pending suffix did not finish')
    def bundles():
        with app.preparation.repository._connection() as db:
            return list(map(tuple,db.execute('SELECT name,bundle_id FROM head ORDER BY name')))
    def daily():
        path=app.daily_worker.output_root/'daily'/'2026-01-01'/'index.json'
        return hashlib.sha256(path.read_bytes()).hexdigest()
    old_bundles=bundles();old_daily=daily();raw_bytes=0
    read=RawRegistry._raw_range
    def measured(self,path,start,end):
        nonlocal raw_bytes
        raw_bytes+=end-start
        return read(self,path,start,end)
    RawRegistry._raw_range=measured
    for _ in range(2*len(app.steps)):app.step()
    RawRegistry._raw_range=read
    evidence={'warm_raw_bytes':raw_bytes,'warm_bundles_unchanged':bundles()==old_bundles,
        'warm_daily_unchanged':daily()==old_daily,
        'restored_identity':app.auxiliary.bus_reader.identities()[2]['serial_string']}
    with app.preparation.catalog.connection() as db:
        before=db.execute('SELECT count(*) FROM entry').fetchone()[0]
    from cell_source_resolver import CellSourceResolver
    raw_refs=CellSourceResolver.read_refs;daily_decodes=[]
    def track_daily(self,refs,**kwargs):
        result=raw_refs(self,refs,**kwargs)
        if threading.current_thread().name=='guardian-daily-diagnostics':daily_decodes.append(result['decodes'])
        return result
    CellSourceResolver.read_refs=track_daily
    try:
        previous_check=app.daily_worker.state['last_check_at']
        app.start()
        deadline=time.monotonic()+10
        while app.daily_worker.state['last_check_at']==previous_check and time.monotonic()<deadline:
            assert app.failure is None,app.failure
            time.sleep(.01)
        assert app.daily_worker.state['last_check_at']!=previous_check
        evidence['started_daily_raw_decodes']=sum(daily_decodes)
        app.collector.cycle()
        deadline=time.monotonic()+8
        while len(requests)<2 and time.monotonic()<deadline:time.sleep(.01)
        assert len(requests)>=2
        # End this finite synthetic input batch. Continuous Live/HTTP overlap is
        # covered separately; catching up must not require an idle real system.
        app.auxiliary.close();assert app.daily_worker.stop()
        frontiers={item.name:item.consumer.registry._head()['seq'] for item in app.catalog_tails}
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            assert app.failure is None,app.failure
            if caught_up():break
            time.sleep(.01)
        else:raise AssertionError('new finite delta failed to finish: '+str(app.preparation.status()))
        evidence['completed_frontiers']=all(item.consumer.cursor()[0]>frontiers[item.name] for item in app.catalog_tails)
        with app.preparation.catalog.connection() as db:
            evidence['new_cell_records']=db.execute('SELECT count(*) FROM entry').fetchone()[0]-before
    finally:app.close()
    print(json.dumps(evidence,sort_keys=True))


def test_actual_factory_http_delete_and_restart_preserve_other_events_and_retry_guard(tmp_path,monkeypatch):
    import time,uuid,config_ui
    from urllib.request import Request,urlopen
    from maintenance_service import MaintenanceNotFoundError
    create,layout,clock,inspected=candidate_fixture(tmp_path);inspected.close()
    app=create();monkeypatch.setattr(config_ui.Handler,'_ingress_allowed',lambda self:True)
    request=str(uuid.uuid4())
    fields=dict(occurred_at='2026-01-01T00:00:10Z',category='maintenance',title='explicit synthetic deletion',affected_system='Stack')
    try:
        app.start(http_port=0)
        deadline=time.monotonic()+8
        while not app.maintenance_mutation._caught_up() and time.monotonic()<deadline:time.sleep(.01)
        service=app.collector.maintenance_service
        preserved=service.list()
        event,_=service.create_once(request,**fields)
        deadline=time.monotonic()+8
        while not app.maintenance_mutation._caught_up() and time.monotonic()<deadline:time.sleep(.01)
        assert app.maintenance_mutation._caught_up()
        url='http://127.0.0.1:'+str(app.server.server_port)+'/api/maintenance/events/'+event.maintenance_event_id
        query=Request(url,data=b'{"expected_revision":1}',headers={'Content-Type':'application/json'},method='DELETE')
        with urlopen(query,timeout=10) as response:assert json.load(response)=={'deleted':True}
        assert service.list()==preserved
        assert app.failure is None
    finally:app.close()
    reopened=create()
    try:
        assert reopened.collector.maintenance_service.list()==preserved
        with pytest.raises(MaintenanceNotFoundError,match='deleted'):
            reopened.collector.maintenance_service.create_once(request,**fields)
    finally:reopened.close()


@pytest.mark.parametrize('mode',['--check','--run'])
@pytest.mark.parametrize('reason',['missing provenance','invalid source commit'])
def test_cli_rejects_invalid_provenance_before_candidate_mutation_or_transport(tmp_path,monkeypatch,mode,reason):
    import main as legacy
    import prepared_candidate
    def reject():raise RuntimeError(reason)
    monkeypatch.setattr(legacy,'require_source_commit',reject)
    monkeypatch.setattr(prepared_candidate,'open_candidate',lambda **kw:pytest.fail('opened candidate before provenance'))
    monkeypatch.setattr(legacy,'PylontechConsole',lambda *a:pytest.fail('created console before provenance'))
    monkeypatch.setattr(legacy,'Mqtt',lambda *a:pytest.fail('created MQTT before provenance'))
    root=tmp_path/'absent'
    with pytest.raises(RuntimeError,match=reason):
        prepared_candidate.main(['--root',str(root),'--layout',str(root/'layout'),mode])
    assert not root.exists()
