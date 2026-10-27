"""Real offline adoption -> prepared layout -> candidate; synthetic files only."""
import json
from pathlib import Path
import pytest
from prepared_migration import PreparedMigration
from test_prepared_candidate import candidate_fixture,CandidatePublisher
from test_prepared_collector import pwr
from test_cell_acquisition import rows
from test_diagnostic_repository import raw
from cell_history import CellHistoryWriter
from types import SimpleNamespace
from projection_repository import Conflict


def legacy_plan(tmp_path):
    # Fixture only supplies original metadata records/options. None of its
    # prepared databases/registries is copied into the migration target.
    source=tmp_path/'fixture-source';source.mkdir()
    create,layout,clock,inspected=candidate_fixture(source);inspected.close()
    root=tmp_path/'offline';root.mkdir()
    files=[]
    for key in ('options','maintenance','positions','config'):
        path=root/layout['paths'][key];path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes((source/layout['paths'][key]).read_bytes())
        if key!='options':files.append({'writer':'metadata','path':layout['paths'][key]})
    writer=CellHistoryWriter(root/layout['writers']['cell']['root'])
    writer.append(raw(clock[0]+10,serial='SERIAL-A'));writer.append(raw(clock[0]+20,serial='SERIAL-A'))
    files.extend({'writer':'cell','path':str(path.relative_to(root))} for path in writer.directory.glob('*.jsonl'))
    (root/'cursor-key').write_bytes(b'synthetic-only-secret-no-production')
    layout['runtime']=dict(console_port=str(root/'console'),bus_port=str(root/'passive-wire'),cursor_secret_file='cursor-key',http_port=0,bind_host='127.0.0.1')
    plan={'contract':'guardian-prepared-migration-1','layout':layout,'files':files,
        'streams':[dict(stream='serial-a',origin_day='2026-01-01',identity_epoch_id='SERIAL-A@observed',physical_serial='SERIAL-A')],
        'frontier_day':'2026-01-01','as_of_timestamp':clock[0]+30,
        'provenance':{'kind':'explicit synthetic original writer files'},'rollback':{'raw_preserved':True,'production':False}}
    return root,plan


@pytest.mark.parametrize('all_sources',[False,True])
def test_actual_offline_migration_reopens_every_step_and_starts_candidate(tmp_path,monkeypatch,all_sources):
    from prepared_candidate import open_candidate
    root,plan=legacy_plan(tmp_path)
    if all_sources:add_auxiliary_legacy_sources(root,plan)
    originals={item['path']:(root/item['path']).read_bytes() for item in plan['files']}
    migration=PreparedMigration(root,plan);migration.begin();phases=set()
    for _ in range(1500):
        migration=PreparedMigration(root,plan);state=migration.step();phases.add(state['phase'])
        if state['phase']=='complete':break
    else:pytest.fail(str(state))
    assert {'adopt','metadata','epochs','core','handover','manifest','complete'}<=phases
    assert not state['runtime_activated']
    assert all((root/path).read_bytes()==raw for path,raw in originals.items())
    layout=json.loads(migration.output.read_text())
    app=open_candidate(root=root,layout=layout,console=SimpleNamespace(port=str(root/'console'),command=lambda command:pwr() if command=='pwr' else command,close=lambda:None),
        publisher=CandidatePublisher(),supervisor_api=lambda *a:pytest.fail('Supervisor access'),cursor_secret=b'synthetic-cursor',
        wall_clock=lambda:plan['as_of_timestamp']+60,monotonic=lambda:plan['as_of_timestamp']+60,parse_bat=rows,
        bus_port_resolver=lambda:layout['runtime']['bus_port'],bus_serial_opener=lambda *a,**k:SilentWire(),hycube_opener=SyntheticHycube())
    try:
        assert app.readiness()['state']=='ready_local_candidate'
        assert app.tail.diagnostics.state(layout['diagnostics'])['ingestion']=='live'
        if all_sources:
            from daily_diagnostics import guardian_day
            slices,risk=app.daily_worker.sources.input_backend.read(guardian_day('2026-01-01'))
            assert len(risk)==2 and len(slices['rs485'].records)==1
            assert len(slices['position_history'].records)==1
            assert app.auxiliary.bus_reader.identities()[2]['identity_known']
        app.start();assert app.collector.cycle()['modules']==1
    finally:app.close()


def test_plan_change_and_existing_work_are_never_overwritten(tmp_path):
    root,plan=legacy_plan(tmp_path)
    occupied=root/plan['layout']['paths']['repository'];occupied.write_bytes(b'existing work')
    with pytest.raises(Conflict,match='existing derived work'):PreparedMigration(root,plan).begin()
    assert occupied.read_bytes()==b'existing work' and not (root/'prepared-migration.json').exists()



class SilentWire:
    def __init__(self):
        import threading
        self.stop=threading.Event()
    def read(self,size):self.stop.wait(.01);return b''
    def close(self):self.stop.set()
    def write(self,*a):pytest.fail('passive wire write')


class SyntheticHycube:
    def open(self,request,timeout):
        from test_hycube_evidence import Response,PAYLOAD
        return Response(PAYLOAD if request.full_url.endswith('/data_row/') else
            dict(normalMode=82,bufferMode=3,emergency=10,batProtection=5))


def add_auxiliary_legacy_sources(root,plan):
    from hycube_evidence import HycubeEvidenceWriter,HycubePolicyHistory,observation,policy_observation
    from hycube_projection import HycubeProjectionStore
    from test_hycube_evidence import PAYLOAD
    from test_prepared_rs485_identity import record
    layout=plan['layout'];options_path=root/layout['paths']['options']
    options=json.loads(options_path.read_text());options.update(rs485_sniffer_enabled=True,hycube_evidence_enabled=True,hycube_base_url='http://localhost')
    options_path.write_text(json.dumps(options))
    for name,kind in [('rs485','rs485'),('hycube','hycube_system_observation'),('policy','hycube_policy_observation'),('hycube_projection','hycube_history_projection')]:
        layout['writers'][name]=dict(registry=name+'-registry',root=name,stream=name,epoch='synthetic')
        layout['catalogs'][name]=dict(path=name+'-catalog',writer=name,format=kind,tail=name)
        (root/name).mkdir()
    (root/'rs485'/'day.jsonl').write_text(json.dumps(record())+'\n')
    stamp=plan['as_of_timestamp']
    raw=observation(json.dumps(PAYLOAD).encode(),stamp,200)
    receipt=HycubeEvidenceWriter(root/'hycube').append_with_receipt(raw)
    projection=HycubeProjectionStore(root/'hycube_projection',flush_records=1)
    projection.append_live(raw,receipt);projection.close()
    policy=policy_observation(json.dumps(dict(normalMode=82,bufferMode=3,emergency=10,batProtection=5)).encode(),stamp)
    HycubePolicyHistory(root/'policy').append(policy)
    counts=projection.status();keys=('projection_days','projection_bytes','complete_days','open_days','invalid_days')
    (root/'hycube-inventory.json').write_text(json.dumps({'directory':str(root/'hycube_projection'),'origin':{'synthetic':True},'counts':{key:counts[key] for key in keys}}))
    layout['paths']['hycube_inventory']='hycube-inventory.json'
    for name in ('rs485','hycube','policy','hycube_projection'):
        plan['files'].extend({'writer':name,'path':str(path.relative_to(root))} for path in (root/name).glob('*.jsonl'))
    for kind in ('position','config','maintenance'):
        name='daily_'+kind
        formats={source:name if mapped==kind else None for source,mapped in layout['metadata']['mapping'].items()}
        layout['catalogs'][name]=dict(path=name+'-catalog',writer='metadata',format=name,source_formats=formats,tail=name)
    layout['daily']={'catalogs':{'cell':'cell','rs485':'rs485','position_history':'daily_position','config_history':'daily_config','maintenance_history':'daily_maintenance'},
        'options':{'automatic_history_days':1,'initial_catchup_days':1,'late_data_days':1,'check_interval_seconds':1}}
    layout['research_paths'].update(rs485_history='rs485',hycube_history='hycube',hycube_policy='policy',hycube_projection='hycube_projection')


def complete_migration(root,plan):
    PreparedMigration(root,plan).begin()
    for _ in range(1500):
        state=PreparedMigration(root,plan).step()
        if state['phase']=='complete':return root/'prepared-layout.json'
    pytest.fail(str(state))


def test_migrated_layout_actual_cli_check_run_and_shutdown(tmp_path,monkeypatch,capsys):
    import main,prepared_candidate,version,config_ui,signal,startup,hashlib
    from prepared_collector import PreparedCollector
    from test_build_provenance import SEAL,REVISION
    from urllib.request import urlopen
    root,plan=legacy_plan(tmp_path)
    layout_path=complete_migration(root,plan)
    marker=tmp_path/'source-marker';marker.write_text(REVISION)
    build=tmp_path/'build-info.json'
    SEAL.seal_build_info(version.GUARDIAN_VERSION,marker,build)
    validate_source=version.require_source_commit
    monkeypatch.setattr(main,'require_source_commit',lambda:validate_source(build))
    monkeypatch.setattr(version,'require_source_commit',lambda:validate_source(build))
    closed=[];publishers=[];applications=[]
    def console(port,*args):
        return SimpleNamespace(port=port,command=lambda command:pwr() if command=='pwr' else command,close=lambda:closed.append(port))
    def mqtt(options):
        publisher=CandidatePublisher();publishers.append(publisher);return publisher
    monkeypatch.setattr(main,'PylontechConsole',console)
    monkeypatch.setattr(main,'Mqtt',mqtt)
    monkeypatch.setattr(main,'parse_bat',rows)
    monkeypatch.setattr(config_ui.Handler,'_ingress_allowed',lambda self:True)
    monkeypatch.setattr(signal,'signal',lambda *a:None)
    def one_cycle(self,stop):
        app=self.application;applications.append(app)
        assert self.cycle()['modules']==1
        with urlopen('http://127.0.0.1:'+str(app.server.server_port)+'/api/config',timeout=5) as response:
            assert json.load(response)['current']['module_count']==1
        stop.set()
    monkeypatch.setattr(PreparedCollector,'run',one_cycle)
    args=['--root',str(root),'--layout',str(layout_path)]
    prepared_candidate.main(args+['--check'])
    assert json.loads(capsys.readouterr().out)['state']=='ready_local_candidate'
    assert not publishers and not applications
    activation=root/'operator-activation.json'
    assert not activation.exists()  # Migration must never activate the runtime.
    activation.write_text(json.dumps(dict(contract='guardian-prepared-activation-1',
        guardian_version=version.GUARDIAN_VERSION,source_commit=REVISION,root=str(root),
        layout=layout_path.name,layout_sha256=hashlib.sha256(layout_path.read_bytes()).hexdigest())))
    startup.main(activation_path=activation)
    assert len(applications)==len(publishers)==1
    assert applications[0].closed and not applications[0].thread.is_alive()
    assert publishers[0].closed and closed


def test_migration_plan_and_original_change_rejected(tmp_path):
    import copy
    root,plan=legacy_plan(tmp_path)
    migration=PreparedMigration(root,plan);migration.begin()
    changed=copy.deepcopy(plan);changed['as_of_timestamp']+=1
    with pytest.raises(Conflict,match='plan changed'):PreparedMigration(root,changed).step()
    # Persist the original snapshot, then change a frozen source. No activation.
    while migration.state()['phase']=='freeze':migration.step()
    source=root/plan['files'][0]['path'];before=source.read_bytes();source.write_bytes(before+b' ')
    with pytest.raises(Conflict):
        for _ in range(1500):migration.step()
    assert source.read_bytes()==before+b' '
    assert not migration.output.exists()


def test_resume_after_committed_manifest_before_control_ack(tmp_path,monkeypatch):
    root,plan=legacy_plan(tmp_path);migration=PreparedMigration(root,plan);migration.begin()
    while migration.state()['phase']!='manifest':migration.step()
    class ProcessLost(Exception):pass
    def lost(state):raise ProcessLost()
    with monkeypatch.context() as context:
        context.setattr(migration,'save',lost)
        with pytest.raises(ProcessLost):migration.step()
    # Reconstruct from disk; the manifest cursor, not stale ordinal, owns resume.
    complete_migration(root,plan)
    assert PreparedMigration(root,plan).state()['phase']=='complete'


def test_one_original_file_cannot_be_adopted_by_two_writers(tmp_path):
    root,plan=legacy_plan(tmp_path)
    first=plan['files'][0]
    plan['layout']['writers']['second']=dict(plan['layout']['writers'][first['writer']],registry='second-registry')
    plan['files'].append(dict(first,writer='second'))
    with pytest.raises(ValueError,match='duplicate legacy source binding'):PreparedMigration(root,plan)
    assert not (root/'prepared-migration.json').exists()
