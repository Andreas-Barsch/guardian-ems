"""Local actual application factory, transport and HTTP; no HA or hardware."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.request import urlopen
import pytest
from main import create_prepared_application
from application_writer import ApplicationWriter
from cell_history import CellHistoryWriter
from local_history_composition import compose_local_history
from metadata_revision import MetadataRevision, RevisionMetadataReader
from research_api import ResearchPaths
from store_migration_manifest import MigrationManifest
from work_scheduler import WorkScheduler
from projection_repository import Unavailable
from test_application_tail import setup as tail_setup, drain
from test_metadata_revision import publish
from test_cell_acquisition import module, rows
from canonical_day_repository import bounds


def setup(tmp_path, *, with_daily=False, with_legacy=False, collector_factory=None):
    env, preparation, tail = tail_setup(tmp_path)
    scheduler = WorkScheduler()
    backend = ApplicationWriter(env['rawreg'], tmp_path/'cells', stream='cell',
                                writer_epoch='synthetic-application', scheduler=scheduler)
    writer = CellHistoryWriter(backend.root, append_backend=backend)
    revision = MetadataRevision(env['repo'], 'initial')
    revision.initialize()
    revision.begin(provenance={'synthetic': 'explicit empty position history'})
    publish(revision)
    metadata = RevisionMetadataReader(env['repo'], 'initial')
    paths = ResearchPaths(backend.root, tmp_path/'positions', tmp_path/'maintenance',
                          tmp_path/'canonical', tmp_path/'daily', display_history=tmp_path/'display')
    composition = compose_local_history(paths=paths, cell_catalog=env['catalog'], metadata=metadata,
        publication_index=tail.index, canonical_repository=preparation.canonical,
        display_repository=preparation.display, streams=['serial-a'], scheduler=scheduler,
        cursor_secret=b'synthetic-local-test-secret', cell_registry=backend.registry)
    manifest = MigrationManifest(env['repo'], 'application-migration')
    manifest.initialize()
    start, _ = bounds(env['origin'])
    clock = [start + 10]
    calls = []
    console = SimpleNamespace(command=lambda command: (calls.append(command), command)[1], close=lambda:None)
    daily_worker = None
    if with_daily:
        from daily_indexed_inputs import DailyIndexedInputs
        from daily_diagnostics import DailyDiagnosticSources
        from daily_diagnostic_worker import DailyDiagnosticWorker
        daily_inputs = DailyIndexedInputs({'cell':env['catalog'], 'rs485':None,
            'position_history':None, 'config_history':None, 'maintenance_history':None})
        daily_worker = DailyDiagnosticWorker(DailyDiagnosticSources(input_backend=daily_inputs), tmp_path/'daily',
            scheduler=scheduler, clock=lambda:datetime(2026,1,2,1,tzinfo=timezone.utc),
            automatic_history_days=1, initial_catchup_days=1, late_data_days=1, check_interval_seconds=1)
    collector = None if collector_factory is None else collector_factory(tmp_path, scheduler, clock)
    metadata_tail = None
    epoch_consumers = ()
    identity = SimpleNamespace(identity_at=lambda position, when: ('SERIAL-A', 'synthetic-position'))
    if collector is not None:
        from application_metadata import MetadataTail, HeadMetadataReader, ConfirmedIdentity, ConfirmedPositionReader, ConfirmedMaintenanceReader
        from metadata_log_consumer import MetadataLogConsumer
        from test_metadata_log_consumer import drain as drain_metadata
        tail.diagnostics.configure(tail.name, collector.options)
        service = collector.maintenance_service
        service.repository.log.append_backend = collector.technical_writer
        event = service.create(occurred_at=datetime.fromtimestamp(clock[0],timezone.utc),
            category='module_replacement',title='Synthetic initial installation',affected_system='Stack')
        collector.position_service.record(effective_at=datetime.fromtimestamp(clock[0],timezone.utc).isoformat(),
            maintenance_event_id=event.maintenance_event_id,
            positions={str(i):'SERIAL-A' if i==1 else None for i in range(1,7)},expected_latest_snapshot_id=None)
        collector.config_history.record_if_changed(collector.options)
        registry = collector.technical_writer.registry
        source = collector.technical_writer.source
        mapping = {source(collector.position_service.log.path):'position',
            source(service.repository.log.path):'maintenance',source(collector.config_history.path):'config',
            source(collector.technical_path):'technical',
            source(service.repository.log.path.with_suffix(service.repository.log.path.suffix+'.retired-requests')):'retired'}
        origin = {'source':'explicit synthetic initial collector metadata'}
        revision = MetadataRevision(env['repo'],'collector-initial')
        revision.begin(provenance=origin)
        consumer = MetadataLogConsumer(registry,revision);consumer.initialize(mapping,origin_proof=origin)
        drain_metadata(consumer);publish(revision,head='initial')
        metadata_tail = MetadataTail(env['repo'],registry,head='current',mapping=mapping,origin_proof=origin)
        metadata_tail.initialize()
        from history_epoch_adapter import EpochLogConsumer
        epoch_consumer = EpochLogConsumer(registry, preparation.epochs)
        epoch_consumer.initialize({source:kind if kind in ('config','position') else None
                                  for source,kind in mapping.items()})
        epoch_consumers = (epoch_consumer,)
        metadata = HeadMetadataReader(env['repo'],'current')
        identity = ConfirmedIdentity(metadata_tail,metadata)
        collector.position_service.read_backend = ConfirmedPositionReader(metadata_tail,metadata,
            collector.technical_writer,collector.position_service.log.path)
        service.repository.read_backend = ConfirmedMaintenanceReader(metadata_tail,metadata,
            collector.technical_writer,service.repository.log.path)
        composition = compose_local_history(paths=paths,cell_catalog=env['catalog'],metadata=metadata,
            publication_index=tail.index,canonical_repository=preparation.canonical,
            display_repository=preparation.display,streams=['serial-a'],scheduler=scheduler,
            cursor_secret=b'synthetic-local-test-secret',cell_registry=backend.registry)
    app = create_prepared_application(root=tmp_path, manifest=manifest, preparation=preparation,
        tail=tail, composition=composition, scheduler=scheduler, cell_writer=writer,
        identity=identity, metadata_tail=metadata_tail, epoch_consumers=epoch_consumers,
        console=console, parse_bat=rows, wall_clock=lambda: clock[0], poll_seconds=.001, daily_worker=daily_worker,
        collector=collector)
    if with_legacy:
        from application_migration import LegacyWriterAdoption
        from test_canonical_repository import sample
        from raw_registry import file_signature
        legacy = CellHistoryWriter(backend.root)
        record = sample(start+5, soc=75)
        legacy.append(record)
        path = next(backend.root.glob('*.jsonl'))
        before, signature = path.read_bytes(), file_signature(path)
        adoption = LegacyWriterAdoption(env['repo'], 'legacy-cell', [(backend,path)])
        adoption.initialize()
        adoption.begin(provenance={'synthetic':'frozen legacy CellHistoryWriter output'},
                       rollback={'original_bytes':'preserved'})
        for _ in range(100):
            if adoption.step()['status'] == 'complete':
                break
        else:
            pytest.fail('explicit legacy adoption did not finish')
        assert path.read_bytes() == before and file_signature(path) == signature
        env['legacy_bytes'] = before
        # Explicit offline preparation before manifest/startup, never a GET.
        drain(preparation, tail)
    # Frozen migration scope is explicit. No startup/GET invokes this preparation.
    env['consumer'].step(expected_cursor=env['consumer'].cursor(), max_records=1)
    state = env['catalog'].progress('raw', 'r1')
    units = [(env['catalog'], dict(origin='synthetic empty source', source='raw', revision='r1',
                                  frontier=state['frontier'], receipt=state['receipt']))]
    if with_legacy:
        receipt = backend.registry.state(backend.source(path))
        state = env['catalog'].progress(receipt['source'], receipt['revision'])
        units.append((env['catalog'], dict(origin='adopted frozen legacy cell bytes',
            source=receipt['source'], revision=receipt['revision'],
            frontier=state['frontier'], receipt=state['receipt'])))
    manifest.begin(units, provenance={'application_binding': app.binding, 'synthetic': True},
                   rollback={'preserved_originals': True, 'production': False})
    return env, app, clock, calls


def prepare(env, app):
    for _ in range(100):
        result = app.manifest.step(env['catalog'], expected_version=app.manifest.state()['version'])
        if result['complete']:
            return
    pytest.fail('explicit application manifest incomplete')


def target(stamp):
    def iso(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    return '/api/history/series?metrics=soc&selected_modules=1&from='+iso(stamp-1)+'&to='+iso(stamp+1)


def test_unprepared_application_never_starts_or_reads_raw(tmp_path, monkeypatch):
    env, app, clock, calls = setup(tmp_path)
    monkeypatch.setattr(app.preparation, 'step', lambda: pytest.fail('implicit preparation'))
    assert app.readiness()['state'] == 'preparation_required'
    with pytest.raises(Unavailable, match='migration_incomplete'):
        app.start(http_port=0)
    assert app.thread is None and app.server is None and calls == []
    assert list(app.cell_writer.directory.iterdir()) == []


def test_actual_factory_collect_tail_and_manual_http(tmp_path, monkeypatch):
    import config_ui
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    app.collect_once([module(1)])
    assert calls == ['bat 1']
    drain(app.preparation, app.tail)
    monkeypatch.setattr(config_ui.Handler, '_ingress_allowed', lambda self: True)
    monkeypatch.setattr(config_ui, '_get_history_api', lambda: pytest.fail('legacy API factory'))
    monkeypatch.setattr(config_ui, '_get_research_api', lambda: pytest.fail('legacy research factory'))
    try:
        app.start(http_port=0)
        with urlopen('http://127.0.0.1:'+str(app.server.server_port)+target(clock[0]), timeout=5) as response:
            body = json.load(response)
        assert body['series'][0]['points'][0]['value'] == 50
        assert calls == ['bat 1']
        assert app.failure is None
    finally:
        app.close()
    assert not app.thread.is_alive()


def test_wrong_completed_migration_cannot_select_application(tmp_path):
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    app.binding = 'different-composition'
    with pytest.raises(Unavailable, match='application_binding_mismatch'):
        app.collect_once([module(1)])
    assert calls == []


def test_actual_daily_worker_owned_by_application_uses_prepared_windows(tmp_path, monkeypatch):
    import time
    import daily_diagnostics
    env, app, clock, calls = setup(tmp_path, with_daily=True)
    prepare(env, app)
    app.collect_once([module(1)])
    drain(app.preparation, app.tail)
    monkeypatch.setattr(daily_diagnostics, '_source_files', lambda *a, **k: pytest.fail('legacy daily discovery'))
    result = tmp_path/'daily/daily/2026-01-01/index.json'
    try:
        app.start()
        deadline = time.monotonic()+10
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert result.exists(), app.daily_worker.state
        payload = json.loads(result.read_text())
        assert payload['diagnostic_date'] == '2026-01-01'
        assert app.daily_worker.sources.input_backend.metrics['cell']['decodes'] == 1
        assert calls == ['bat 1']
        assert app.failure is None
    finally:
        app.close()
    assert not app.daily_worker.thread.is_alive()


def reopen(root):
    """Fresh interpreter reconstruction, no initialization or legacy rebuild."""
    from pathlib import Path
    from history_repository_preparation import local_composition
    from diagnostic_repository import DiagnosticRepository
    from publication_window_index import PublicationWindowIndex
    from application_tail import ApplicationTail
    from raw_registry import RawRegistry
    root = Path(root)
    preparation = local_composition(repository_path=root/'projection.db',
        catalog_path=root/'catalog.db', epoch_path=root/'epochs/epochs.db')
    diagnostics = DiagnosticRepository(preparation.repository, preparation.catalog)
    index = PublicationWindowIndex(preparation.repository)
    tail = ApplicationTail(preparation, diagnostics, index, name='application')
    scheduler = WorkScheduler()
    backend = ApplicationWriter(RawRegistry(root/'raw-registry'), root/'cells', stream='cell',
                                writer_epoch='synthetic-application', scheduler=scheduler)
    paths = ResearchPaths(root/'cells', root/'positions', root/'maintenance', root/'canonical',
                          root/'daily', display_history=root/'display')
    metadata = RevisionMetadataReader(preparation.repository, 'initial')
    composition = compose_local_history(paths=paths, cell_catalog=preparation.catalog, metadata=metadata,
        publication_index=index, canonical_repository=preparation.canonical,
        display_repository=preparation.display, streams=['serial-a'], scheduler=scheduler,
        cursor_secret=b'synthetic-local-test-secret', cell_registry=backend.registry)
    clock = [bounds('2026-01-01')[0]+20]
    calls = []
    app = create_prepared_application(root=root,
        manifest=MigrationManifest(preparation.repository, 'application-migration'),
        preparation=preparation, tail=tail, composition=composition, scheduler=scheduler,
        cell_writer=CellHistoryWriter(root/'cells', append_backend=backend),
        identity=SimpleNamespace(identity_at=lambda position, when: ('SERIAL-A','synthetic-position')),
        console=SimpleNamespace(command=lambda command: (calls.append(command),command)[1]),
        parse_bat=rows, wall_clock=lambda:clock[0], poll_seconds=.001)
    return app, clock, calls


@pytest.mark.parametrize('with_legacy', [False, True])
def test_full_interpreter_restart_reads_confirmed_state_and_only_new_diagnostic_delta(tmp_path, with_legacy):
    import os
    import subprocess
    import sys
    from pathlib import Path
    env, app, clock, calls = setup(tmp_path, with_legacy=with_legacy)
    prepare(env, app)
    app.collect_once([module(1)])
    drain(app.preparation, app.tail)
    raw_path = next(app.cell_writer.directory.glob('*.jsonl'))
    if with_legacy:
        assert raw_path.read_bytes().startswith(env['legacy_bytes'])
    old_end = raw_path.stat().st_size
    old_bundle = app.tail.diagnostics.state('application')['bundle']
    app.close()
    script = r'''
import json,sys,time
from urllib.request import urlopen
from test_prepared_application import reopen,target,module
import config_ui
app,clock,calls=reopen(sys.argv[1])
assert app.tail.diagnostics.state('application')['bundle']==int(sys.argv[3])
assert app.tail.step()['raw_bytes']==0
original=app.tail.diagnostics.reader.read_refs
seen=[]
def counted(refs,*args,**kwargs):
    assert all(ref.start>=int(sys.argv[2]) for ref in refs), 'old diagnostic Raw replay'
    seen.extend(refs)
    return original(refs,*args,**kwargs)
app.tail.diagnostics.reader.read_refs=counted
config_ui.Handler._ingress_allowed=lambda self:True
try:
    app.start(http_port=0)
    address='http://127.0.0.1:'+str(app.server.server_port)
    with urlopen(address+target(clock[0]-10),timeout=5) as response:
        assert json.load(response)['series'][0]['points'][0]['value']==50
    if sys.argv[4]=='True':
        with urlopen(address+target(clock[0]-15),timeout=5) as response:
            assert json.load(response)['series'][0]['points'][0]['value']==75
    app.collect_once([module(1)])
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        with app.preparation.repository._connection() as db:
            last=db.execute('SELECT max(id) FROM preparation_job').fetchone()[0]
        if app.tail.state()['after_job']==last and app.preparation.status()['state']=='caught_up' and len(seen)==1:
            break
        time.sleep(.005)
    assert len(seen)==1,app.failure
    with urlopen(address+target(clock[0]),timeout=5) as response:
        assert json.load(response)['series'][0]['points'][0]['value']==50
    assert calls==['bat 1']
    assert app.failure is None
    print('RESTART_RESULT '+json.dumps({'new_diagnostic_decodes':len(seen),'old_bundle':int(sys.argv[3]),
        'new_bundle':app.tail.diagnostics.state('application')['bundle'],'calls':calls}))
finally:
    app.close()
'''
    environment = dict(os.environ)
    environment['PYTHONPATH'] += os.pathsep+str(Path(__file__).parent)
    result = subprocess.run([sys.executable,'-c',script,str(tmp_path),str(old_end),str(old_bundle),str(with_legacy)],
                            env=environment, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout+result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith('RESTART_RESULT '))
    evidence = json.loads(line.removeprefix('RESTART_RESULT '))
    assert evidence['new_bundle'] != evidence['old_bundle']
    assert evidence['new_diagnostic_decodes'] == 1


@pytest.mark.parametrize('method', ['GET','POST','PUT','PATCH','DELETE'])
def test_unbound_candidate_routes_cannot_escape_to_productive_defaults(tmp_path, monkeypatch, method):
    from urllib.request import Request
    from urllib.error import HTTPError
    import config_ui
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    monkeypatch.setattr(config_ui.Handler, '_ingress_allowed', lambda self:True)
    monkeypatch.setattr(config_ui, '_read_options', lambda:pytest.fail('productive default options'))
    monkeypatch.setattr(config_ui, '_api', lambda *a, **k:pytest.fail('Supervisor call'))
    try:
        app.start(http_port=0)
        address='http://127.0.0.1:'+str(app.server.server_port)
        with pytest.raises(HTTPError) as error:
            urlopen(Request(address+'/api/config', method=method), timeout=5)
        assert error.value.code == 503
        assert json.load(error.value)['error']['code'] == 'application_binding_required'
        with urlopen(address+'/api/history-preparation', timeout=5) as response:
            body=json.load(response)
        assert body['application']['production_approved'] is False
        assert calls == []
    finally:
        app.close()


def test_fresh_application_rejects_same_content_database_replaced_at_same_path(tmp_path):
    import os
    import sqlite3
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    app.close()
    replacement = tmp_path/'replacement-catalog'
    with env['catalog'].connection() as source, sqlite3.connect(replacement) as destination:
        source.backup(destination)
    # Synthetic replacement of a derived store; never a production operation.
    os.replace(replacement, env['catalog'].path)
    reopened, _, commands = reopen(tmp_path)
    with pytest.raises(Unavailable, match='application_binding_mismatch'):
        reopened.start()
    assert commands == []


def test_manual_http_reads_during_live_io_and_keeps_confirmed_read_while_catalog_pending(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import config_ui
    import time
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    app.collect_once([module(1)])
    drain(app.preparation, app.tail)
    # Hold background preparation at its existing explicit pause gate so the
    # receipt-to-catalog handover is deterministic, not a timing race.
    app.preparation.pause()
    raw_started, release_raw, read_started, query_started = (Event() for _ in range(4))
    original_query = app.composition.history._query
    def query(*args, **kwargs):
        query_started.set()
        return original_query(*args, **kwargs)
    monkeypatch.setattr(app.composition.history, '_query', query)
    original_execute = app.scheduler.execute
    def execute(kind, callback, **kwargs):
        if kind == 'manual_http_history':
            read_started.set()
        return original_execute(kind, callback, **kwargs)
    monkeypatch.setattr(app.scheduler, 'execute', execute)
    def bat(command):
        calls.append(command)
        raw_started.set()
        assert release_raw.wait(5), 'synthetic BAT release missing'
        return command
    app.console.command = bat
    monkeypatch.setattr(config_ui.Handler, '_ingress_allowed', lambda self: True)
    clock[0] += 10
    try:
        app.start(http_port=0)
        address = 'http://127.0.0.1:'+str(app.server.server_port)+target(clock[0]-10)
        def request():
            from urllib.error import HTTPError
            try:
                with urlopen(address, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, json.load(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            live = pool.submit(app.collect_once, [module(1)])
            try:
                assert raw_started.wait(5)
                manual = pool.submit(request)
                assert read_started.wait(5)
                status, body = manual.result(timeout=5)
                assert status == 200, body
                assert query_started.is_set()
                assert not live.done()
            finally:
                release_raw.set()
            assert len(live.result(timeout=5)) == 1
            status, body = manual.result(timeout=5)
            assert status == 200, body
            assert body['series'][0]['points'][0]['value'] == 50
            # This first response finished before the pending append existed.
            # A second explicit read proves stable access after confirmed append.
            status, body = request()
            assert status == 200, body
            assert body['data_state']['measurements']['confirmed_prefixes'][0]['pending_confirmed_bytes'] > 0
            assert query_started.is_set()
        backend = app.cell_writer.append_backend
        raw_path = next(app.cell_writer.directory.glob('*.jsonl'))
        receipt = backend.registry.state(backend.source(raw_path))
        app.preparation.resume()
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            with app.preparation.repository._connection() as db:
                last = db.execute('SELECT max(id) FROM preparation_job').fetchone()[0]
            indexed = env['catalog'].progress(receipt['source'], receipt['revision'])
            if (indexed['cursor'] == receipt['end'] and app.tail.state()['after_job'] == last
                    and app.preparation.status()['state'] == 'caught_up'):
                break
            time.sleep(.005)
        else:
            pytest.fail('confirmed receipt was not prepared: '+str(app.failure))
        # A distinct explicit request, never automatic retry in the application.
        status, body = request()
        assert status == 200, body
        assert body['series'][0]['points'][0]['value'] == 50
        assert calls == ['bat 1', 'bat 1']
        assert app.failure is None
    finally:
        release_raw.set()
        app.close()


class StableAsOfReadUnavailable(AssertionError):
    """Only the known A1/A2 handover error, never another test failure."""


def test_forty_delayed_consumer_cycles_keep_the_confirmed_as_of_http_window(tmp_path, monkeypatch):
    """Confirmed as-of contract during a delayed application consumer."""
    from urllib.error import HTTPError
    import config_ui
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    app.collect_once([module(1)])
    drain(app.preparation, app.tail)
    original_stamp = clock[0]
    old_catalog = env['catalog'].progress(
        app.cell_writer.append_backend.source(next(app.cell_writer.directory.glob('*.jsonl'))), 'initial')
    app.preparation.pause()
    monkeypatch.setattr(config_ui.Handler, '_ingress_allowed', lambda self: True)
    failures = []
    try:
        app.start(http_port=0)
        route = target(original_stamp)
        route = route[:route.index('&to=')]+'&to='+datetime.fromtimestamp(original_stamp+500,timezone.utc).isoformat().replace('+00:00','Z')
        address = 'http://127.0.0.1:'+str(app.server.server_port)+route
        for cycle in range(40):
            clock[0] += 10
            app.collect_once([module(1)])
            try:
                with urlopen(address, timeout=5) as response:
                    body = json.load(response)
                    assert body['series'][0]['points'][0]['value'] == 50
                    assert len(body['series'][0]['points']) == 1
                    assert body['data_state']['measurements']['confirmed_prefixes'][0]['pending_confirmed_bytes'] > 0
            except HTTPError as exc:
                body = json.load(exc)
                assert exc.code == 503
                assert body['error']['code'] == 'history_preparation_required'
                assert body['error']['message'] == 'source metadata changed; explicit append receipt or new revision required'
                failures.append((cycle+1, exc.code, body['error']['code'], body['error']['message']))
        assert calls == ['bat 1']*41
        assert app.failure is None
        current = env['catalog'].progress(app.cell_writer.append_backend.source(
            next(app.cell_writer.directory.glob('*.jsonl'))), 'initial')
        assert current['cursor'] == old_catalog['cursor']
        if failures:
            raise StableAsOfReadUnavailable(f'{len(failures)}/40 stable as-of requests rejected; first={failures[:1]}')
    finally:
        app.close()


def test_http_keeps_completed_publications_through_catalog_canonical_display_handover(tmp_path, monkeypatch):
    import config_ui
    env, app, clock, calls = setup(tmp_path)
    prepare(env, app)
    app.collect_once([module(1)])
    drain(app.preparation, app.tail)
    day = env['origin']
    start, _ = bounds(day)
    iso = lambda stamp:datetime.fromtimestamp(stamp,timezone.utc).isoformat().replace('+00:00','Z')
    route = '/api/history/series?metrics=soc&selected_modules=1&from='+iso(start)+'&to='+iso(start+3600)
    # Inject delayed background turns; use the same real durable preparation
    # and subscriber explicitly to stop at each cross-store boundary.
    monkeypatch.setattr(app,'step',lambda:None)
    monkeypatch.setattr(config_ui.Handler,'_ingress_allowed',lambda self:True)
    try:
        app.start(http_port=0)
        def read():
            with urlopen('http://127.0.0.1:'+str(app.server.server_port)+route,timeout=5) as response:
                return json.load(response)
        before = read()
        old = before['data_state']['measurements']
        assert old['basis'] == 'published_confirmed_inputs'
        old_canonical = env['repo'].head(app.preparation.canonical._head('serial-a',day))
        clock[0] += 60
        app.collect_once([module(1)])
        assert read()['data_state']['measurements'] == old
        for _ in range(1500):
            app.preparation.step()
            if env['repo'].head(app.preparation.canonical._head('serial-a',day)) != old_canonical:
                break
        else:
            pytest.fail('canonical append did not publish')
        # New canonical head, old display and no new read indexes yet.
        assert read()['data_state']['measurements'] == old
        for _ in range(1500):
            if app.preparation.status()['state'] == 'caught_up':break
            app.preparation.step()
        else:
            pytest.fail('display append did not publish')
        # Both new derived heads exist; previous complete read roots stay selected.
        assert read()['data_state']['measurements'] == old
        drain(app.preparation, app.tail)
        after = read()['data_state']['measurements']
        assert after['publications'] != old['publications']
        assert after['latest_raw_coverage'] == 'not_asserted'
        assert calls == ['bat 1','bat 1']
    finally:
        app.close()
