"""Actual local collector/factory integration; no device or broker connection."""
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.request import urlopen
import json
import time
import pytest
from main import create_prepared_collector
from config_ui import DEFAULTS
from raw_registry import RawRegistry
from application_writer import ApplicationWriter
from config_history import ConfigHistory
from position_history import PositionHistoryLog, PositionHistoryService
from maintenance import MaintenanceEventLog
from maintenance_service import MaintenanceRepository, MaintenanceService
from soc_crash_events import SocCrashEventStore
from test_prepared_application import setup, prepare, target


class Publisher:
    _derived_reconnect_epoch = 0
    def __init__(self):self.live=[];self.derived=[];self.closed=False
    def derived_sink(self):return SimpleNamespace(publish_derived_job=self.derived.append)
    def discovery(self,count):self.count=count
    def publish(self,*args,**kwargs):self.live.append((args,kwargs))
    def maintenance_events(self,*args,**kwargs):pass
    def close(self):self.closed=True


def factory(root,scheduler,clock):
    registry=RawRegistry.create(root/'collector-registry')
    backend=ApplicationWriter(registry,root/'collector-logs',stream='collector',writer_epoch='synthetic',scheduler=scheduler)
    backend.root.mkdir()
    now=lambda:datetime.fromtimestamp(clock[0],timezone.utc)
    maintenance=MaintenanceService(MaintenanceRepository(MaintenanceEventLog(backend.root/'maintenance.jsonl')),clock=now)
    positions=PositionHistoryService(PositionHistoryLog(backend.root/'positions.jsonl',append_backend=backend),maintenance,clock=now)
    return create_prepared_collector(options=dict(DEFAULTS,command='pwr',module_count=1,raw_log=False,
        detailed_log=False,cell_diagnostics_enabled=True,poll_interval_seconds=10,cell_diagnostics_interval_seconds=10),
        publisher=Publisher(),config_history=ConfigHistory(backend.root/'config.jsonl',append_backend=backend,clock=now),
        position_service=positions,maintenance_service=maintenance,soc_store=SocCrashEventStore(root/'soc-events.jsonl'),
        technical_writer=backend,technical_path=backend.root/'technical.jsonl',state_path=root/'state.json',
        trend_path=root/'trends.json',incident_path=root/'incident.json',wall_clock=lambda:clock[0],monotonic=lambda:clock[0])


def pwr():
    return '1 48000 -1000 25000 24000 26000 3300 3310 Idle Normal Normal Normal 50%\n01-09-26 12:00:00 Normal Normal 25000 Normal'


def test_actual_collector_pwr_stat_info_bat_and_manual_http(tmp_path, monkeypatch):
    import config_ui
    monkeypatch.setattr(config_ui.Handler, '_ingress_allowed', lambda self: True)
    monkeypatch.setattr(config_ui, '_get_maintenance_api', lambda:pytest.fail('legacy maintenance factory'))
    monkeypatch.setattr(config_ui, '_get_position_history_api', lambda:pytest.fail('legacy position factory'))
    env,app,clock,calls=setup(tmp_path,collector_factory=factory)
    prepare(env,app)
    def command(value):
        calls.append(value)
        return pwr() if value=='pwr' else value
    app.console=SimpleNamespace(command=command,close=lambda:None)
    app.start(http_port=0)
    try:
        assert app.collector.cycle()['modules']==1
        assert calls==['pwr','stat','info 1','bat 1']
        assert app.collector.publisher.live[-1][1]['include_derived'] is False
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with app.tail.diagnostics.view(app.tail.name) as (view,_):
                values=view.cells.values_for_module(1)
            if values:break
            assert app.failure is None,app.failure
            time.sleep(.01)
        else:pytest.fail('collector sample did not reach persistent pair')
        port=app.server.server_address[1]
        with urlopen('http://127.0.0.1:'+str(port)+target(clock[0]),timeout=5) as response:
            body=json.load(response)
        assert body['series'][0]['points'][0]['value']==50
        for route in ('/api/position-history/current','/api/maintenance/events'):
            with urlopen('http://127.0.0.1:'+str(port)+route,timeout=5) as response:
                payload=json.load(response)
            if 'position-history' in route:
                assert payload['snapshot']['positions']['1']=='SERIAL-A'
            else:
                assert len(payload['events'])==1
        deadline=time.monotonic()+10
        while app.collector.analysis.latest() is None and time.monotonic()<deadline:
            assert app.failure is None, app.failure
            assert app.collector.analysis.status()['failure_count'] == 0, app.collector.analysis.status()
            time.sleep(.01)
        assert app.collector.analysis.latest() is not None, app.collector.analysis.status()
        first=list(calls)
        clock[0]+=10
        assert app.collector.cycle()['modules']==1
        assert calls[len(first):]==['pwr','bat 1']
        assert (tmp_path/'state.json').exists()
        assert len(app.collector.publisher.live)==2
        deadline=time.monotonic()+10
        while not app.collector.publisher.derived and time.monotonic()<deadline:
            time.sleep(.01)
        assert app.collector.publisher.derived
        assert app.collector.publisher.derived[0]['results'][1]['physical_module_serial']=='SERIAL-A'
        assert app.failure is None,app.failure
    finally:
        app.close()
    assert app.collector.publisher.closed


def test_unprepared_full_collector_does_not_touch_transport(tmp_path):
    env,app,clock,calls=setup(tmp_path,collector_factory=factory)
    from projection_repository import Unavailable
    with pytest.raises(Unavailable,match='migration_incomplete'):
        app.start()
    assert calls==[] and app.collector.publisher.live==[]
    assert not app.collector.started


def test_existing_topology_confirmation_can_reference_just_created_event(tmp_path, monkeypatch):
    import position_history
    import config_ui
    from test_application_metadata import finish
    for name in ('_OBSERVED_STACK','_OBSERVATION_CANDIDATES','_PRESENCE_SOURCES','_MISSING_CANDIDATES'):
        monkeypatch.setattr(position_history,name,{})
    monkeypatch.setattr(position_history,'_HISTORY_READY',False)
    env,app,clock,calls=setup(tmp_path,collector_factory=factory)
    prepare(env,app)
    service=app.collector.position_service
    monkeypatch.setattr(service.log,'read_all',lambda:pytest.fail('topology replayed position log'))
    monkeypatch.setattr(app.collector.maintenance_service.repository.log,'read_all',lambda:pytest.fail('topology replayed maintenance log'))
    for _ in range(3):
        clock[0]+=10
        position_history.update_observed_stack({1:{'barcode':'SERIAL-B'}},present_positions={1},
            communication_healthy=True,expected_module_count=1,observed_at=clock[0])
    assert config_ui.record_stable_observed_positions(position_service=service,
        maintenance_service=app.collector.maintenance_service,
        clock=lambda:datetime.fromtimestamp(clock[0],timezone.utc))
    finish(app.metadata_tail)
    assert service.current().positions['1']=='SERIAL-B'
    assert len(app.collector.maintenance_service.list())==2
    assert not config_ui.record_stable_observed_positions(position_service=service,
        maintenance_service=app.collector.maintenance_service,
        clock=lambda:datetime.fromtimestamp(clock[0],timezone.utc))
    app.close()


@pytest.mark.parametrize('stage', ['discovery', 'mqtt', 'tail'])
def test_start_failure_releases_http_workers_and_transports(tmp_path, monkeypatch, stage):
    import threading
    env,app,clock,calls=setup(tmp_path,collector_factory=factory)
    prepare(env,app)
    closed=[]
    app.console.close=lambda:closed.append('console')
    def fail(*args,**kwargs):
        raise RuntimeError('synthetic startup failure')
    if stage=='discovery':
        monkeypatch.setattr(app.collector.publisher,'discovery',fail)
    elif stage=='mqtt':
        monkeypatch.setattr(app.collector.mqtt,'start',fail)
    else:
        original=threading.Thread.start
        def start(thread):
            if thread.name=='guardian-prepared-tail':fail()
            return original(thread)
        monkeypatch.setattr(threading.Thread,'start',start)
    with pytest.raises(RuntimeError,match='synthetic startup failure'):
        app.start(http_port=0)
    assert app.stop.is_set() and app.scheduler.status()['closed']
    assert app.server.socket.fileno()==-1
    assert app.collector.publisher.closed and closed==['console']
    for worker in (app.collector.analysis,app.collector.mqtt):
        assert worker._thread is None or not worker._thread.is_alive()
    app.close()
    assert closed==['console']


def test_shutdown_continues_after_transport_error(tmp_path):
    env,app,clock,calls=setup(tmp_path,collector_factory=factory)
    def fail():raise OSError('synthetic close failure')
    app.console.close=fail
    with pytest.raises(ExceptionGroup,match='Application shutdown failed'):
        app.close()
    assert app.collector.publisher.closed
    assert app.scheduler.status()['closed']


def test_manual_http_finishes_during_full_six_module_collector_round(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from test_cell_acquisition import module
    from test_application_tail import drain
    import config_ui
    monkeypatch.setattr(config_ui.Handler,'_ingress_allowed',lambda self:True)
    def six_factory(*args):
        collector=factory(*args);collector.options['module_count']=6;return collector
    env,app,clock,calls=setup(tmp_path,collector_factory=six_factory)
    prepare(env,app)
    app.collect_once([module(1)]);drain(app.preparation,app.tail)
    app.preparation.pause()
    stamp=clock[0];clock[0]+=10
    entered,release=Event(),Event();durations={}
    base,footer=pwr().split('\n')
    payload='\n'.join(str(i)+base[1:] for i in range(1,7))+'\n'+footer
    commands=[]
    def command(value):
        commands.append(value)
        if value=='bat 2':
            entered.set()
            assert release.wait(10),'synthetic slow transport was not released'
        return payload if value=='pwr' else value
    app.console.command=command
    app.start(http_port=0)
    address='http://127.0.0.1:'+str(app.server.server_port)+target(stamp)
    def cycle():
        start=time.monotonic()
        try:return app.collector.cycle()
        finally:durations['collector_seconds']=time.monotonic()-start
    def request():
        start=time.monotonic()
        with urlopen(address,timeout=5) as response:body=json.load(response)
        durations['http_seconds']=time.monotonic()-start
        return body
    try:
        with ThreadPoolExecutor(2) as pool:
            live=pool.submit(cycle)
            try:
                assert entered.wait(5)
                assert app.scheduler.status()['raw_active']>0
                body=pool.submit(request).result(5)
                assert body['series'][0]['points'][0]['value']==50
                assert not live.done(),'HTTP must finish before blocked transport resumes'
            finally:release.set()
            assert live.result(5)['modules']==6
        assert [c for c in commands if c.startswith('bat ')]==[f'bat {i}' for i in range(1,7)]
        assert [c for c in commands if c.startswith('info ')]==[f'info {i}' for i in range(1,7)]
        assert commands.count('pwr')==commands.count('stat')==1
        assert app.failure is None,app.failure
        print('SYNTHETIC_FULL_COLLECTOR_HTTP '+json.dumps(durations,sort_keys=True))
    finally:app.close()


def test_config_epoch_fanout_is_consumed_by_actual_application_steps(tmp_path):
    env,app,clock,calls=setup(tmp_path,collector_factory=factory)
    prepare(env,app)
    consumer=app.epoch_consumers[0]
    for _ in range(250):
        app.step()
        if consumer.cursor()[0] > consumer.registry._head()['seq']:
            break
    else:pytest.fail('application did not consume initial metadata epochs')
    assert app.preparation.epochs.at('config',clock[0])['config_id']==app.collector.config_history._last_record()['config_id']
    assert app.preparation.epochs.position_at(1,clock[0])[0]=='SERIAL-A'
    clock[0]+=60
    options=dict(app.collector.options,poll_interval_seconds=20)
    record=app.collector.config_history.record_if_changed(options)
    for _ in range(250):
        app.step()
        if consumer.cursor()[0] > consumer.registry._head()['seq']:
            break
    else:pytest.fail('application did not consume changed configuration epoch')
    assert app.preparation.epochs.at('config',clock[0]-1)['parameters']['poll_interval_seconds']==10
    assert app.preparation.epochs.at('config',clock[0])['config_id']==record['config_id']
    assert consumer.step(expected_cursor=consumer.cursor())['raw_bytes']==0
    app.close()
