"""Original daily science/fingerprint parity on actual bounded window inputs."""
from dataclasses import replace
from datetime import timedelta
import json
import uuid
import pytest
import daily_diagnostics as daily
from daily_indexed_inputs import DailyIndexedInputs
from raw_registry import RawRegistry, encoded, hashed
from raw_registry_catalog import RegistryCatalogConsumer
from source_catalog import SourceCatalog
from test_daily_diagnostics import source_set, cell, management, position


def catalog(tmp_path, name, paths, kind):
    registry = RawRegistry.create(tmp_path/(name+'-registry'))
    store = SourceCatalog(tmp_path/(name+'-catalog'))
    consumer = RegistryCatalogConsumer(registry, store, record_format=kind)
    consumer.initialize()
    for index, path in enumerate(paths):
        before = path.read_bytes()
        receipt = registry.register(str(index), 'initial', path, writer_epoch='synthetic-migration',
            namespace='legacy-local', operation='register:'+str(index), allow_existing=True)
        while receipt['end'] < len(before):
            receipt = registry.adopt_tail(str(index), expected_receipt=hashed(encoded(receipt)),
                                          operation=uuid.uuid4().hex)
        assert path.read_bytes() == before
    for _ in range(1000):
        if consumer.step(expected_cursor=consumer.cursor(), max_records=1)['caught_up']:
            break
    else:
        pytest.fail('receipt preparation did not finish')
    for source in store.registered():
        for _ in range(1000):
            progress = store.progress(source['source'], source['revision'])
            if progress['cursor'] == progress['frontier']:
                break
            store.index_step(source['source'], source['revision'], expected_cursor=progress['cursor'], max_records=8)
        else:
            pytest.fail('index preparation did not finish')
    return store


def inputs(tmp_path, sources):
    mapping = {'cell': (sources.cell_history_root, 'cell'),
               'rs485': (sources.rs485_history_root, 'daily_rs485'),
               'position_history': (sources.position_history_path, 'daily_position'),
               'config_history': (sources.config_history_path, 'daily_config'),
               'maintenance_history': (sources.maintenance_history_path, 'daily_maintenance')}
    stores = {}
    for name, (path, kind) in mapping.items():
        if path is None or not path.exists():
            stores[name] = None
        else:
            paths = sorted(path.glob('*.jsonl')) if path.is_dir() else [path]
            stores[name] = catalog(tmp_path, name, paths, kind)
    return DailyIndexedInputs(stores)


@pytest.mark.parametrize('date', ['2026-03-29', '2026-10-25', '2026-08-31'])
def test_exact_original_fingerprint_risk_context_and_day_edges(tmp_path, monkeypatch, date):
    day = daily.guardian_day(date)
    first, end = day.start.timestamp(), day.end.timestamp()
    risk_start = daily.guardian_day(day.start.date()-timedelta(days=13)).start.timestamp()
    stamps = [risk_start-1, risk_start, first-1, first, end-1, end]
    records = [dict(cell(stamp), schema_version=1) for stamp in stamps]
    configs = [{'schema_version':1, 'timestamp':day.start.isoformat(), 'config_id':'same', 'parameters':{}}]
    sources = source_set(tmp_path, [management(first, -25), management(end, -15)], records,
        [position((day.start-timedelta(days=400)).isoformat()),
         position((day.start-timedelta(days=5)).isoformat(), module=4)], configs)
    expected = daily.probe_daily_inputs(date, sources)
    backend = inputs(tmp_path, sources)
    candidate = replace(sources, input_backend=backend)
    monkeypatch.setattr(daily, '_source_files', lambda *a, **k: pytest.fail('global discovery'))
    monkeypatch.setattr(daily, '_slice_jsonl', lambda *a, **k: pytest.fail('legacy raw daily read'))
    assert daily.build_source_catalog(candidate) == {}
    actual = daily.probe_daily_inputs(date, candidate)
    assert actual.input_fingerprint == expected.input_fingerprint
    assert actual.risk_cell_records == expected.risk_cell_records
    assert actual.position_projection == expected.position_projection
    assert actual.config_projection == expected.config_projection
    assert actual.sources['rs485'].records == expected.sources['rs485'].records
    assert backend.metrics['cell']['decodes'] == 4
    assert backend.metrics['position_history']['decodes'] == 2
    assert backend.metrics['context']['to_exclusive'] == end
    assert day.duration_seconds == (23*3600 if date=='2026-03-29' else 25*3600 if date=='2026-10-25' else 24*3600)


def test_actual_daily_methods_and_persisted_scientific_results_match(tmp_path):
    first = daily.guardian_day('2026-08-31').start.timestamp()+3600
    sources = source_set(tmp_path, [management(first, -25)],
        [dict(cell(first+i), schema_version=1) for i in range(300)],
        [position('2026-08-30T00:00:00+00:00')])
    backend = inputs(tmp_path, sources)
    old = daily.run_daily_diagnostic('2026-08-31', sources, tmp_path/'old')
    new = daily.run_daily_diagnostic('2026-08-31', replace(sources, input_backend=backend), tmp_path/'new')
    assert new['input_fingerprint'] == old['input_fingerprint']
    assert new['daily_result_id'] == old['daily_result_id']
    assert new['components'] == old['components']
    for relative in ('aggregates/cell_risk/2026-08-31.json',):
        assert json.loads((tmp_path/'old'/relative).read_text()) == json.loads((tmp_path/'new'/relative).read_text())


def test_real_worker_reuses_persisted_guard_without_raw_and_invalidates_parameters(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from daily_diagnostic_worker import DailyDiagnosticWorker
    from cell_source_resolver import CellSourceResolver
    from bms_management_evidence import EvidenceParameters
    stamp=daily.guardian_day('2026-08-31').start.timestamp()+3600
    sources=source_set(tmp_path,[management(stamp,-25)],[dict(cell(stamp),schema_version=1)],
        [position('2026-08-30T00:00:00+00:00')])
    backend=inputs(tmp_path,sources);prepared=replace(sources,input_backend=backend)
    def create(**kw):
        return DailyDiagnosticWorker(prepared,tmp_path/'output',automatic_history_days=1,
            initial_catchup_days=1,clock=lambda:datetime(2026,9,1,12,tzinfo=timezone.utc),**kw)
    first=create();assert first.check_once()==[]
    result=first.check_once();assert len(result)==1
    assert first.state['prepared_inputs']['2026-08-31']['input_fingerprint']==result[0]['input_fingerprint']
    # A new worker reads the persisted guard and actual validated result/index.
    raw=CellSourceResolver.read_refs;calls=[]
    monkeypatch.setattr(CellSourceResolver,'read_refs',lambda self,refs,**kw:(calls.append(len(refs)),raw(self,refs,**kw))[1])
    second=create();assert second.check_once()==[] and calls==[]
    assert second.check_once()==[] and calls==[]
    # Scientific configuration is part of the guard, never inferred from IDs.
    from dataclasses import replace as changed
    third=create(bms_parameters=changed(second.bms_parameters,near_zero_current_a=0.6))
    third.check_once()
    assert sum(calls)>0


def test_persisted_guard_rejects_same_size_same_mtime_rewrite(tmp_path):
    import os
    from research_budget import ResearchQueryError
    stamp=daily.guardian_day('2026-08-31').start.timestamp()+3600
    sources=source_set(tmp_path,[],[dict(cell(stamp),schema_version=1)],[position('2026-08-30T00:00:00+00:00')])
    backend=inputs(tmp_path,sources);day=daily.guardian_day('2026-08-31')
    backend.confirmed_guard(day)
    path=next(sources.cell_history_root.glob('*.jsonl'));before=path.stat();raw=path.read_bytes()
    assert b'3300' in raw
    path.write_bytes(raw.replace(b'3300',b'3301',1));os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert path.stat().st_size==before.st_size and path.stat().st_mtime_ns==before.st_mtime_ns
    with pytest.raises(ResearchQueryError,match='source changed'):backend.confirmed_guard(day)


@pytest.mark.parametrize('date',['2026-03-29','2026-10-25'])
def test_daily_long_context_is_partitioned_without_loss_or_double_boundary(tmp_path,monkeypatch,date):
    day=daily.guardian_day(date);start=day.start.timestamp();end=day.end.timestamp()
    risk=daily.guardian_day(day.start.date()-timedelta(days=13)).start.timestamp()
    stamps=[risk,risk+3599.5,risk+3600,start,start+3600,end-0.001,end]
    sources=source_set(tmp_path,[],[dict(cell(stamp),schema_version=1) for stamp in stamps])
    backend=inputs(tmp_path,sources)
    # A tiny per-window page budget demonstrates that the whole fourteen-day
    # window is not forced through one interactive budget. No production cap
    # is raised, and each selected row still uses the actual resolver/Raw read.
    from cell_source_resolver import CellSourceResolver
    original=CellSourceResolver.plan_snapshot;windows=[]
    def single(self,snapshot,**kw):
        kw['limit']=1
        return original(self,snapshot,**kw)
    monkeypatch.setattr(CellSourceResolver,'plan_snapshot',single)
    backend.windows['cell'].max_pages=2
    opened=CellSourceResolver.open_window
    def capture(self,first,last,**kw):
        windows.append((first,last));return opened(self,first,last,**kw)
    monkeypatch.setattr(CellSourceResolver,'open_window',capture)
    slices,records=backend.read(day)
    assert [row['timestamp'] for row in records]==stamps[:-1]
    assert backend.metrics['cell']['decodes']==len(stamps)-1
    assert all(last-first<=3600 for first,last in windows)
    proof=backend.confirmed_guard(day)
    assert proof['sources']['cell']['selected_records']==len(stamps)-1



def test_closed_daily_guard_ignores_today_append_during_capture_without_raw(tmp_path,monkeypatch):
    from test_source_catalog import registration,write,index_all
    from cell_source_resolver import CellSourceResolver
    day=daily.guardian_day('2026-08-31');stamp=day.start.timestamp()+3600
    sources=source_set(tmp_path,[],[dict(cell(stamp),schema_version=1)])
    backend=inputs(tmp_path,sources);expected=backend.confirmed_guard(day)
    window=backend.windows['cell'];catalog=window.resolver.catalog
    original=window.resolver.plan_window;changes=[]
    def plan(*args,**kw):
        if not changes:
            path=write(tmp_path/'today.jsonl',[dict(cell(day.end.timestamp()+60),schema_version=1)])
            catalog.register(**registration(path,source='today'))
            index_all(catalog,'today')
            changes.append(True)
        return original(*args,**kw)
    monkeypatch.setattr(window.resolver,'plan_window',plan)
    monkeypatch.setattr(CellSourceResolver,'read_refs',lambda *a,**kw:pytest.fail('warm guard decoded Raw'))
    assert backend.confirmed_guard(day)==expected
    assert changes and catalog.status()['pending']==0



def test_daily_unknown_unindexed_source_is_not_hidden_by_an_indexed_source(tmp_path):
    from test_source_catalog import registration,write
    from research_budget import ResearchQueryError
    day=daily.guardian_day('2026-08-31');stamp=day.start.timestamp()+3600
    sources=source_set(tmp_path,[],[dict(cell(stamp),schema_version=1)])
    backend=inputs(tmp_path,sources);catalog=backend.windows['cell'].resolver.catalog
    unknown=write(tmp_path/'unknown.jsonl',[dict(cell(stamp+1,module=2),schema_version=1)])
    catalog.register(**registration(unknown,source='unknown'))
    with pytest.raises(ResearchQueryError,match='stable indexed prefix'):backend.confirmed_guard(day)
    with pytest.raises(ResearchQueryError,match='unindexed confirmed'):backend.read(day)
