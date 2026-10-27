import copy
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import time

import pytest
import history_live_priority as priority
import history_preparation as prep
from history_block_index import selected_ranges
from research_budget import ResearchQueryError
from test_display_history_projection import cell_record, write_rows

DAY='2026-01-02'
BASE=datetime(2026,1,2,tzinfo=timezone.utc).timestamp()
NOW=datetime(2026,1,10,tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def coordinated():
    priority.collector_stopped()
    priority.cycle_started(); priority.idle_until(time.monotonic()+120)
    yield
    priority.collector_stopped()


def request(*components):
    return dict(timestamp_from='2026-01-02T00:00:00Z',timestamp_to='2026-01-02T01:00:00Z',
                modules=[1],components=list(components),shared_day_artifacts=True)


def worker(root): return prep.SelectedHistoryPreparation(root,clock=lambda:NOW)


def run(w, limit=30):
    for _ in range(limit):
        status=w.step()
        if status['state'] in {'complete','blocked','cancelled'}: return status
    raise AssertionError(w.status())


def source(root, count=600):
    path=root/'cell_history'/f'{DAY}.jsonl'
    write_rows(path,[cell_record(BASE+i,module=1) for i in range(count)])
    return path


def test_submission_and_status_do_not_inspect_history(tmp_path,monkeypatch):
    w=worker(tmp_path)
    def forbidden(*a,**k): raise AssertionError('history read in HTTP path')
    monkeypatch.setattr(prep,'signature',forbidden)
    value=w.submit(request('cell_index','display'))
    assert value['state']=='pending' and w.status()==value
    assert not (tmp_path/'cell_history').exists()
    with pytest.raises(ValueError): w.submit(request('cell_index'))


@pytest.mark.parametrize('patch',[
    {'components':[]},{'components':['everything']},{'shared_day_artifacts':False},
    {'timestamp_to':'2026-01-07T00:00:00Z'},{'timestamp_to':'2026-01-02T01:00:00'},
    {'modules':[True]},{'modules':[]},{'timestamp_from':None},
])
def test_invalid_scope_rejected_before_job_created(tmp_path,patch):
    w=worker(tmp_path); value=request('cell_index');value.update(patch)
    with pytest.raises(ValueError): w.submit(value)
    assert not w.path.exists()


def test_live_priority_and_no_uncoordinated_cli(tmp_path):
    source(tmp_path);w=worker(tmp_path);w.submit(request('cell_index'))
    priority.collector_stopped()
    assert w.step()['reason']=='live_coordination_unavailable'
    priority.cycle_started()
    assert w.step()['reason']=='live_acquisition_priority'
    assert not list(tmp_path.rglob('*.idx'))
    priority.idle_until(time.monotonic()+120)
    assert run(w)['state']=='complete'


def test_index_checkpoint_restart_bounded_and_raw_unchanged(tmp_path):
    path=source(tmp_path);raw=path.read_bytes();w=worker(tmp_path);w.submit(request('cell_index'))
    w.step() # absent adjacent day
    w.step()
    task=next(t for t in w.status()['tasks'] if t['day']==DAY)
    assert 0<task['bytes_indexed']<len(raw)
    assert task['bytes_indexed']<=prep.CHUNK_BYTES+65536
    restart=worker(tmp_path)
    assert restart.status()['id']==w.status()['id']
    assert run(restart)['state']=='complete'
    assert path.read_bytes()==raw
    ranges, info=selected_ranges(path,BASE+400,BASE+450,timestamp_field='timestamp',iso_timestamp=False)
    assert ranges and info['skipped_bytes']>0
    assert not (tmp_path/'cell_history/2025-12-31.jsonl.idx').exists()


def test_source_change_blocks_without_replacing_old_index(tmp_path):
    path=source(tmp_path);old=Path(str(path)+'.idx');old.write_text('existing')
    w=worker(tmp_path);w.submit(request('cell_index'));w.step();w.step()
    path.write_bytes(path.read_bytes()+b'{}\n')
    result=run(w)
    assert result['state']=='blocked'
    assert any(t.get('reason')=='source_revision_changed' for t in result['tasks'])
    assert old.read_text()=='existing'


def test_preemption_mid_block_replays_uncommitted_step(tmp_path,monkeypatch):
    path=source(tmp_path);w=worker(tmp_path);w.submit(request('cell_index'));w.step()
    original=prep._epoch;calls=0
    def interrupted(*a,**k):
        nonlocal calls
        calls+=1
        if calls==5: priority.cycle_started()
        return original(*a,**k)
    monkeypatch.setattr(prep,'_epoch',interrupted)
    assert w.step()['reason']=='live_acquisition_priority'
    assert not Path(str(path)+'.idx').exists()
    monkeypatch.setattr(prep,'_epoch',original)
    priority.idle_until(time.monotonic()+120)
    assert run(worker(tmp_path))['state']=='complete'
    assert sum(b['record_count'] for b in json.loads(Path(str(path)+'.idx').read_text())['blocks'])==600


def test_open_day_is_blocked_without_touching_writer_files(tmp_path):
    path=source(tmp_path,2);w=prep.SelectedHistoryPreparation(tmp_path,clock=lambda:BASE+100)
    w.submit(request('cell_index'))
    result=run(w)
    assert result['state']=='blocked'
    assert any(t.get('reason')=='open_source_owned_by_live_writer' for t in result['tasks'])
    assert not Path(str(path)+'.idx').exists()


def test_cancel_preserves_checkpoint_and_sources(tmp_path):
    path=source(tmp_path);raw=path.read_bytes();w=worker(tmp_path);w.submit(request('cell_index'))
    w.step();w.step();w.cancel()
    assert w.step()['state']=='cancelled'
    assert path.read_bytes()==raw and not Path(str(path)+'.idx').exists()
    assert list(w.directory.rglob('index.json'))


def test_display_builds_selected_day_in_staging_and_preserves_unselected(tmp_path):
    from display_history_projection import read_projection
    source(tmp_path,300)
    other=tmp_path/'display_history/1m/2025-12-01.jsonl.gz'
    other.parent.mkdir(parents=True);other.write_bytes(b'untouched old artifact')
    w=worker(tmp_path);w.submit(request('display'))
    w.step()
    assert not (tmp_path/f'display_history/1m/{DAY}.meta.json').exists()
    assert run(worker(tmp_path))['state']=='complete'
    assert other.read_bytes()==b'untouched old artifact'
    records=read_projection(tmp_path/'display_history','1m',DAY)
    assert any(r['metric']=='soc' for r in records)
    assert (tmp_path/f'display_history/1m/{DAY}.jsonl.gz.window.sqlite').is_file()


def canonical_fixture(root):
    from canonical_phase import CanonicalPhaseProjection
    from config_history import ConfigHistory
    from test_canonical_phase import PARAMS
    source(root,10)
    previous=root/'cell_history/2026-01-01.jsonl'
    write_rows(previous,[cell_record(BASE-43200),cell_record(BASE-43170)])
    config=root/'config_history.jsonl'
    config.write_text(json.dumps({'schema_version':1,'timestamp':'2025-12-31T00:00:00+00:00',
        'config_id':'cfg','parameters':PARAMS})+'\n')
    projection=CanonicalPhaseProjection(root/'cell_history',root/'canonical_phase_v2',
        ConfigHistory(config),clock=lambda:NOW)
    projection.build()
    target=root/f'canonical_phase_v2/{DAY}.json.gz'
    Path(str(target)+'.window.sqlite').unlink()
    return target


def test_canonical_reindex_preserves_facts_bytes_and_predecessor(tmp_path):
    from canonical_window_store import read
    target=canonical_fixture(tmp_path);original=target.read_bytes();mtime=target.stat().st_mtime_ns
    w=worker(tmp_path);w.submit(request('canonical'))
    assert run(w)['state']=='complete'
    assert target.read_bytes()==original and target.stat().st_mtime_ns==mtime
    result,_=read(target,BASE,BASE+600,module=1)
    assert result['identities']
    assert w.status()['tasks'][0]['predecessor']=='2026-01-01'


@pytest.mark.parametrize('damage',['predecessor_missing','source_changed','config_changed'])
def test_canonical_missing_or_stale_context_honestly_blocked(tmp_path,damage):
    target=canonical_fixture(tmp_path);original=target.read_bytes()
    if damage=='predecessor_missing': (target.parent/'2026-01-01.json.gz').unlink()
    elif damage=='source_changed': (tmp_path/'cell_history/2026-01-01.jsonl').write_text('{}\n')
    else: (tmp_path/'config_history.jsonl').write_text('{}\n')
    w=worker(tmp_path);w.submit(request('canonical'));result=run(w)
    assert result['state']=='blocked'
    assert result['tasks'][0]['reason'].startswith('canonical_')
    assert target.read_bytes()==original and not Path(str(target)+'.window.sqlite').exists()


def test_ingress_only_and_http_never_processes_job(tmp_path,monkeypatch):
    import config_ui
    w=worker(tmp_path);monkeypatch.setattr(config_ui,'_HISTORY_PREPARATION',w)
    handler=config_ui.Handler.__new__(config_ui.Handler)
    handler.path='/api/history-preparation'
    handler._is_research_api=lambda:False
    handler._ingress_allowed=lambda:True
    response=[];handler._send=lambda status,body,*a,**k:response.append((status,body))
    payload=json.dumps(request('cell_index')).encode()
    handler.headers={'Content-Length':str(len(payload))};handler.rfile=io.BytesIO(payload)
    monkeypatch.setattr(w,'step',lambda:pytest.fail('HTTP executed job'))
    handler.do_POST()
    assert response[-1][0]==202 and response[-1][1]['state']=='pending'
    handler.do_GET();assert response[-1][0]==200
    handler._ingress_allowed=lambda:False
    handler.do_POST();assert response[-1][0]==403
    handler.do_GET();assert response[-1][0]==403


def test_raw_queue_and_deadline_pause_without_advancing(tmp_path):
    import queue
    path=source(tmp_path);w=worker(tmp_path);w.submit(request('cell_index'))
    class Writer:
        def __init__(self): self._queue=queue.Queue()
    writer=Writer();writer._queue.put('pending');priority.register_raw_writer(writer)
    assert w.step()['reason']=='live_acquisition_priority'
    writer._queue.get()
    priority.idle_until(time.monotonic()-1)
    assert w.step()['reason']=='timeout'
    assert not Path(str(path)+'.idx').exists()
    priority.idle_until(time.monotonic()+120)
    assert run(w)['state']=='complete'


def test_canonical_hash_checks_same_metadata_rewrite(tmp_path):
    import os
    target=canonical_fixture(tmp_path)
    path=tmp_path/'cell_history/2026-01-01.jsonl';before=path.stat()
    raw=path.read_bytes();path.write_bytes(raw.replace(b'3300',b'3301'))
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    w=worker(tmp_path);w.submit(request('canonical'));result=run(w)
    assert result['state']=='blocked'
    assert result['tasks'][0]['reason']=='canonical_source_hash_mismatch_or_missing'
    assert not Path(str(target)+'.window.sqlite').exists()


def test_display_publication_preemption_resumes_without_mixing_old_algorithm(tmp_path,monkeypatch):
    path=source(tmp_path,10);raw=path.read_bytes()
    target=tmp_path/f'display_history/1m/{DAY}.meta.json'
    target.parent.mkdir(parents=True);target.write_text('{"aggregation_algorithm_version":"old"}')
    w=worker(tmp_path);w.submit(request('display'))
    original=prep._copy_atomic
    def interrupt(src,dst): raise ResearchQueryError('timeout','synthetic deadline',503)
    monkeypatch.setattr(prep,'_copy_atomic',interrupt)
    assert w.step()['state']=='paused'
    assert json.loads(target.read_text())['aggregation_algorithm_version']=='old'
    monkeypatch.setattr(prep,'_copy_atomic',original)
    assert run(worker(tmp_path))['state']=='complete'
    assert json.loads(target.read_text())['aggregation_algorithm_version']=='guardian_display_bucket_v2_segments'
    assert path.read_bytes()==raw


def test_persisted_request_cannot_escape_selected_plan(tmp_path):
    w=worker(tmp_path);w.submit(request('cell_index'))
    value=json.loads(w.path.read_text());value['tasks'][0]['day']='../../../outside'
    w.path.write_text(json.dumps(value))
    assert worker(tmp_path).status()=={'state':'blocked','reason':'invalid_persisted_request'}


def test_no_work_scales_with_unselected_old_files(tmp_path,monkeypatch):
    source(tmp_path,3)
    for i in range(100):
        old=tmp_path/f'cell_history/old-{i}.jsonl';old.write_text('unreadable irrelevant history')
    w=worker(tmp_path);w.submit(request('cell_index'))
    original=Path.open
    def audited(path,*a,**kw):
        assert not path.name.startswith('old-')
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'open',audited)
    assert run(w)['state']=='complete'


def test_cancel_request_survives_restart(tmp_path):
    source(tmp_path);w=worker(tmp_path);w.submit(request('cell_index'));w.cancel()
    assert worker(tmp_path).step()['state']=='cancelled'


def test_corrupt_index_checkpoint_does_not_replace_existing(tmp_path):
    path=source(tmp_path);old=Path(str(path)+'.idx');old.write_text('existing')
    w=worker(tmp_path);w.submit(request('cell_index'));w.step();w.step()
    checkpoint=next(w.directory.rglob('index.json'))
    value=json.loads(checkpoint.read_text());value['offset']+=1
    checkpoint.write_text(json.dumps(value))
    result=run(worker(tmp_path))
    assert result['state']=='blocked'
    assert any(t.get('reason')=='invalid_index_checkpoint' for t in result['tasks'])
    assert old.read_text()=='existing'


def test_hycube_index_uses_received_at(tmp_path):
    from test_history_block_index import projection_record
    path=tmp_path/f'hycube_history_projection/{DAY}.jsonl'
    write_rows(path,[projection_record(BASE+i,i%100,str(i)) for i in range(300)])
    w=worker(tmp_path);w.submit(request('hycube_index'));assert run(w)['state']=='complete'
    ranges,_=selected_ranges(path,BASE+270,BASE+290,timestamp_field='received_at',iso_timestamp=True)
    assert ranges


def test_missing_canonical_day_rebuild_resumes_with_unchanged_phase_semantics(tmp_path):
    target=canonical_fixture(tmp_path)
    expected=json.loads(gzip.decompress(target.read_bytes()))
    target.unlink() # synthetic missing derived artifact, never production
    w=worker(tmp_path);w.submit(request('canonical'))
    w.step();w.step()
    assert not target.exists()
    assert run(worker(tmp_path))['state']=='complete'
    actual=json.loads(gzip.decompress(target.read_bytes()))
    assert actual==expected
    assert w.status()['id']==worker(tmp_path).status()['id']


def test_canonical_rebuild_sorts_source_without_neutral_boundary_restart(tmp_path):
    from canonical_phase import CanonicalPhaseProjection
    from config_history import ConfigHistory
    target=canonical_fixture(tmp_path)
    source_path=tmp_path/f'cell_history/{DAY}.jsonl'
    rows=source_path.read_text().splitlines();source_path.write_text('\n'.join(reversed(rows))+'\n')
    # Reference is the existing semantic builder, isolated to this fixture.
    reference=tmp_path/'reference'
    CanonicalPhaseProjection(tmp_path/'cell_history',reference,
        ConfigHistory(tmp_path/'config_history.jsonl'),clock=lambda:NOW).build()
    expected=json.loads(gzip.decompress((reference/target.name).read_bytes()))
    target.unlink()
    w=worker(tmp_path);w.submit(request('canonical'));assert run(w)['state']=='complete'
    assert json.loads(gzip.decompress(target.read_bytes()))==expected


def test_canonical_missing_sort_checkpoint_blocks_without_false_empty_result(tmp_path):
    target=canonical_fixture(tmp_path);target.unlink()
    w=worker(tmp_path);w.submit(request('canonical'));w.step();w.step()
    spool=next(w.directory.rglob('ordered.sqlite'));spool.unlink()
    assert run(worker(tmp_path))['state']=='blocked'
    assert not target.exists()


def test_display_staging_append_interruption_replays_without_duplicates(tmp_path,monkeypatch):
    import display_history_projection as display
    source(tmp_path,300)
    w=worker(tmp_path);w.submit(request('display'))
    import display_window_store
    original=display_window_store.publish
    def interrupt(*a,**k): raise ResearchQueryError('timeout','synthetic publication interruption',503)
    monkeypatch.setattr(display_window_store,'publish',interrupt)
    assert w.step()['state']=='paused'
    assert list(w.directory.rglob('display-journal.json'))
    monkeypatch.setattr(display_window_store,'publish',original)
    assert run(worker(tmp_path))['state']=='complete'
    records=display.read_projection(tmp_path/'display_history','1m',DAY)
    soc=[r for r in records if r['metric']=='soc']
    assert sum(r['sample_count'] for r in soc)==300
    assert len({r['bucket_start'] for r in soc})==len(soc)


def test_display_closed_prefix_keeps_checkpoint_bounded(tmp_path):
    path=tmp_path/f'cell_history/{DAY}.jsonl'
    write_rows(path,[cell_record(BASE+i*10) for i in range(3000)])
    w=worker(tmp_path);w.submit(request('display'))
    sizes=[]
    for _ in range(30):
        status=w.step()
        sizes.extend(p.stat().st_size for p in w.directory.glob('*/display-*/display/.state/*.json'))
        if status['state'] in {'complete','blocked'}: break
    assert status['state']=='complete'
    assert sizes and max(sizes)<1024*1024


def test_rs485_identity_checkpoint_never_uses_future_identity(tmp_path):
    path=tmp_path/f'rs485_history/{DAY}.jsonl'
    rows=[{'record_type':'frame','timestamp':datetime.fromtimestamp(BASE+i,timezone.utc).isoformat(),
           'adr':1,'physical_serial':'A' if i<290 else 'B'} for i in range(600)]
    write_rows(path,rows)
    w=worker(tmp_path);w.submit(request('rs485_index'));assert run(w)['state']=='complete'
    index=json.loads(Path(str(path)+'.idx').read_text())
    assert index['blocks'][0]['identities_before']=={}
    assert index['blocks'][1]['identities_before']['1']['serial_string']=='A'
    assert index['blocks'][2]['identities_before']['1']['serial_string']=='B'


def test_oversized_record_stops_index_without_replacing_previous(tmp_path):
    path=source(tmp_path,1);path.write_text(' '*65537+'\n')
    old=Path(str(path)+'.idx');old.write_text('previous')
    w=worker(tmp_path);w.submit(request('cell_index'));result=run(w)
    assert result['state']=='blocked'
    assert any(t.get('reason')=='source_record_size_bound' for t in result['tasks'])
    assert old.read_text()=='previous'


def test_unchanged_pause_does_not_repeat_checkpoint_io(tmp_path,monkeypatch):
    w=worker(tmp_path);w.submit(request('cell_index'));priority.cycle_started()
    writes=[];original=prep._save
    def observed(path,value):
        writes.append(path)
        return original(path,value)
    monkeypatch.setattr(prep,'_save',observed)
    assert w.step()['reason']=='live_acquisition_priority'
    assert w.step()['reason']=='live_acquisition_priority'
    assert writes==[w.path]


def test_canonical_restart_between_processing_chunks_matches_continuous_builder(tmp_path):
    from canonical_phase import CanonicalPhaseProjection
    from config_history import ConfigHistory
    target=canonical_fixture(tmp_path)
    source_path=tmp_path/f'cell_history/{DAY}.jsonl'
    rows=[cell_record(BASE+i*10,current=(2 if (i//70)%2 else -2),soc=20+i%70)
          for i in range(600)]
    write_rows(source_path,rows)
    reference=tmp_path/'reference'
    CanonicalPhaseProjection(tmp_path/'cell_history',reference,
        ConfigHistory(tmp_path/'config_history.jsonl'),clock=lambda:NOW).build()
    expected=json.loads(gzip.decompress((reference/target.name).read_bytes()))
    target.unlink()
    w=worker(tmp_path);w.submit(request('canonical'))
    for _ in range(12):
        w.step()
        task=w.status()['tasks'][0]
        if task.get('last_chunk_records'): break
    checkpoint=next(w.directory.rglob('rebuild.json'))
    saved=json.loads(checkpoint.read_text())
    assert saved['cursor'] is not None
    assert any(v['diagnostic_intervals'] for v in saved['emitted'].values())
    assert not target.exists()
    assert run(worker(tmp_path))['state']=='complete'
    assert json.loads(gzip.decompress(target.read_bytes()))==expected


def bootstrap_request():
    return {**request('canonical'),'canonical_bootstrap_from':'2026-01-01'}


def test_bootstrap_restarts_from_retained_origin_matches_existing_full_builder(tmp_path):
    target=canonical_fixture(tmp_path)
    expected={p.name:json.loads(gzip.decompress(p.read_bytes()))
              for p in target.parent.glob('*.json.gz')}
    raws={p:p.read_bytes() for p in (tmp_path/'cell_history').glob('*.jsonl')}
    # Simulate installed live-only artifacts without promoting their checkpoints.
    for p in target.parent.glob('*.json.gz'):
        value=json.loads(gzip.decompress(p.read_bytes()));value['complete']=False
        p.write_bytes(gzip.compress(json.dumps(value).encode()))
    w=worker(tmp_path);w.submit(bootstrap_request())
    w.step();w.step()
    assert next(w.directory.rglob('ordered.sqlite')).exists()
    restart=worker(tmp_path)
    assert run(restart)['state']=='complete'
    for name,value in expected.items():
        assert json.loads(gzip.decompress((target.parent/name).read_bytes()))==value
    assert all(p.read_bytes()==value for p,value in raws.items())
    assert restart.status()['tasks'][0]['mode']=='rebuilt_from_retained_origin'
    assert restart.status()['tasks'][0]['predecessor'] is None


def test_bootstrap_cannot_ignore_an_earlier_retained_day(tmp_path):
    target=canonical_fixture(tmp_path);before=target.read_bytes()
    write_rows(tmp_path/'cell_history/2025-12-31.jsonl',[cell_record(BASE-172800+3600)])
    w=worker(tmp_path);w.submit(bootstrap_request());status=run(w)
    assert status['state']=='blocked'
    assert status['tasks'][0]['reason']=='bootstrap_retained_origin_or_contiguous_prefix_unproved'
    assert target.read_bytes()==before


def test_bootstrap_revision_change_after_restart_blocks_chain(tmp_path):
    target=canonical_fixture(tmp_path);before=target.read_bytes()
    w=worker(tmp_path);w.submit(bootstrap_request());w.step()
    source=tmp_path/'cell_history/2026-01-01.jsonl'
    source.write_bytes(source.read_bytes()+b'\n')
    result=run(worker(tmp_path))
    assert result['state']=='blocked'
    assert result['tasks'][0]['reason']=='bootstrap_context_changed'
    assert target.read_bytes()==before


def test_bootstrap_missing_config_is_unknown_not_invented_default(tmp_path):
    from canonical_phase import CanonicalPhaseProjection
    from config_history import ConfigHistory
    target=canonical_fixture(tmp_path)
    config=tmp_path/'config_history.jsonl'
    value=json.loads(config.read_text());value['timestamp']='2026-01-01T18:00:00+00:00'
    config.write_text(json.dumps(value)+'\n')
    reference=tmp_path/'reference'
    CanonicalPhaseProjection(tmp_path/'cell_history',reference,ConfigHistory(config),clock=lambda:NOW).build()
    expected={p.name:json.loads(gzip.decompress(p.read_bytes())) for p in reference.glob('*.json.gz')}
    w=worker(tmp_path);w.submit(bootstrap_request());assert run(w)['state']=='complete'
    assert json.loads(gzip.decompress(target.read_bytes()))==expected[target.name]
    first=json.loads(gzip.decompress((target.parent/'2026-01-01.json.gz').read_bytes()))
    assert first==expected['2026-01-01.json.gz']
    assert any(i['phase']=='unknown' for data in first['identities'].values() for i in data['diagnostic_intervals'])


def test_bootstrap_live_priority_and_persisted_cancel(tmp_path):
    target=canonical_fixture(tmp_path);before=target.read_bytes()
    w=worker(tmp_path);w.submit(bootstrap_request());priority.cycle_started()
    assert w.step()['reason']=='live_acquisition_priority'
    assert not list(w.directory.rglob('bootstrap-context.json'))
    priority.idle_until(time.monotonic()+120);w.step();w.cancel()
    assert worker(tmp_path).step()['state']=='cancelled'
    assert target.read_bytes()==before


@pytest.mark.parametrize('value',[None,'2026-01-02','2025-01-01','2026-1-1'])
def test_bootstrap_requires_explicit_bounded_origin(tmp_path,value):
    w=worker(tmp_path)
    with pytest.raises(ValueError): w.submit({**request('canonical'),'canonical_bootstrap_from':value})
    assert not w.path.exists()


def test_bootstrap_metadata_inventory_has_hard_entry_limit(tmp_path):
    target=canonical_fixture(tmp_path);before=target.read_bytes()
    for i in range(513): (tmp_path/'cell_history'/f'ignored-{i}.idx').touch()
    w=worker(tmp_path);w.submit(bootstrap_request());status=run(w)
    assert status['state']=='blocked'
    assert status['tasks'][0]['reason']=='bootstrap_inventory_entry_bound'
    assert target.read_bytes()==before
