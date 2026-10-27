"""Targeted preparation/reader regressions for the reported missing SOC indexes."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import history_preparation as prep
from history_block_index import build_index, _load_valid
from history_live_priority import historical_slot
from hycube_evidence import HycubeBatteryCapacitySeries, HycubePolicyHistory, policy_observation
from policy_index_reader import indexed_policy_records
from research_budget import ResearchQueryError
from test_history_preparation import coordinated, worker, run, source, request, BASE, DAY, NOW
from test_hycube_projection import raw_record
from test_display_history_projection import write_rows


def selected(*parts):
    return {**request(*parts),'selected_sources_only':True}


def iso(epoch): return datetime.fromtimestamp(epoch,timezone.utc).isoformat()


def test_reuse_keeps_valid_index_bytes_and_does_not_read_other_days(tmp_path,monkeypatch):
    raw=source(tmp_path,300)
    build_index(raw,timestamp_field='timestamp',iso_timestamp=False)
    index=Path(str(raw)+'.idx'); before=index.read_bytes()
    unrelated=tmp_path/'cell_history/2026-01-01.jsonl';unrelated.write_text('do not read')
    w=worker(tmp_path);w.submit(selected('cell_index'))
    assert len(w.status()['tasks']) == 1
    monkeypatch.setattr(prep,'_epoch',lambda *a:pytest.fail('reused source decoded'))
    result=run(w)
    assert result['tasks'][0]['mode']=='existing_index_reused'
    assert index.read_bytes()==before
    assert not Path(str(unrelated)+'.idx').exists()


def test_extend_only_missing_prefix_preserves_raw_and_previous_index(tmp_path,monkeypatch):
    raw=source(tmp_path,500); original=raw.read_bytes()
    build_index(raw,timestamp_field='timestamp',iso_timestamp=False,complete=False)
    index=Path(str(raw)+'.idx'); previous=index.read_bytes(); calls=[]
    original_epoch=prep._epoch
    def count(*args):
        calls.append(args[0]['timestamp']);return original_epoch(*args)
    monkeypatch.setattr(prep,'_epoch',count)
    w=worker(tmp_path);w.submit(selected('cell_index'));result=run(w)
    assert len(calls)==244 and min(calls)==BASE+256
    assert result['tasks'][0]['mode']=='existing_prefix_extended'
    assert raw.read_bytes()==original
    assert (w.directory/result['id']/('cell_index-'+DAY)/'previous.idx').read_bytes()==previous
    assert _load_valid(raw,timestamp_field='timestamp',iso_timestamp=False)['complete']


def test_hycube_prepares_actual_raw_fallback_and_returns_measurements(tmp_path):
    raw=tmp_path/'hycube_history'/f'{DAY}.jsonl'
    write_rows(raw,[raw_record(iso(BASE+i),42) for i in range(600)])
    projection=tmp_path/'hycube_history_projection'/f'{DAY}.jsonl'
    projection.parent.mkdir();projection.write_text('unusable projection without metadata')
    original=raw.read_bytes();w=worker(tmp_path);w.submit(selected('hycube_source_index'))
    result=run(w)
    assert result['tasks'][0]['source_directory']=='hycube_history'
    assert not Path(str(projection)+'.idx').exists()
    with historical_slot():
        data=HycubeBatteryCapacitySeries(raw.parent).query(timestamp_from=iso(BASE+300),timestamp_to=iso(BASE+359))
    assert len(data['points'])==60 and all(p['value']==42 for p in data['points'])
    assert raw.read_bytes()==original


def test_hycube_reuses_complete_projection_when_reader_uses_it(tmp_path):
    from hycube_projection import HycubeProjectionBackfill, projection_plan
    raw=tmp_path/'hycube_history'/f'{DAY}.jsonl'
    write_rows(raw,[raw_record(iso(BASE+i),42) for i in range(3)])
    directory=tmp_path/'hycube_history_projection'
    HycubeProjectionBackfill(raw.parent,directory,max_records=10,pause_seconds=0,clock=lambda:NOW).run_once()
    assert projection_plan(raw,directory)['mode']=='projection'
    w=worker(tmp_path);w.submit(selected('hycube_source_index'));result=run(w)
    assert result['tasks'][0]['source_directory']=='hycube_history_projection'
    assert not Path(str(raw)+'.idx').exists()


def policy(epoch,changed=True):
    value=policy_observation(b'{"normalMode":82,"bufferMode":3,"emergency":10,"batProtection":5}',epoch)
    value['policy_changed']=changed
    return value


def test_policy_index_reads_only_selected_records_with_predecessor(tmp_path,monkeypatch):
    directory=tmp_path/'hycube_policy_history';path=directory/f'{DAY}.jsonl'
    rows=[policy(BASE+i*300, i==0 or i==180) for i in range(288)]
    write_rows(path,rows);assert path.stat().st_size>65536
    old=directory/'2025-12-01.jsonl';old.write_text('must not be read'*100000)
    original=path.read_bytes();w=worker(tmp_path);w.submit(selected('policy_index'));result=run(w)
    assert result['state']=='complete' and path.read_bytes()==original
    monkeypatch.setattr('history_live_priority.bounded_metadata_paths',lambda *a:pytest.fail('legacy inventory called'))
    with historical_slot():
        values=HycubePolicyHistory(directory).query(timestamp_from=iso(BASE+179*300+10),timestamp_to=iso(BASE+181*300))
    assert len(values)==2
    assert values[0]['observed_at']==iso(BASE+179*300)
    assert values[0]['from']==iso(BASE+179*300+10)
    assert values[1]['from']==iso(BASE+180*300)
    assert values[0]['to']==values[1]['from']


def test_open_sources_stay_writer_owned_and_policy_appends_index_themselves(tmp_path):
    directory=tmp_path/'hycube_policy_history';write_rows(directory/f'{DAY}.jsonl',[policy(BASE)])
    w=prep.SelectedHistoryPreparation(tmp_path,clock=lambda:BASE+100)
    w.submit(selected('policy_index'));result=run(w)
    assert result['tasks'][0]['state']=='writer_owned'
    assert not list(directory.glob('*.idx'))
    writer=HycubePolicyHistory(directory);path=writer.append(policy(BASE+300))
    saved=path.read_bytes()
    indexed=_load_valid(path,timestamp_field='observed_at',iso_timestamp=True)
    assert indexed['indexed_size']==len(saved) and len(indexed['policy_refs'])==2
    assert indexed_policy_records(directory,iso(BASE+301),iso(BASE+350))[0]['observed_at']==iso(BASE+300)


def test_policy_writer_catches_backlog_in_bounded_units(tmp_path):
    directory=tmp_path/'hycube_policy_history';path=directory/f'{DAY}.jsonl'
    write_rows(path,[policy(BASE+i,False) for i in range(500)])
    writer=HycubePolicyHistory(directory);writer.append(policy(BASE+501))
    value=json.loads(Path(str(path)+'.idx').read_text())
    assert len(value['blocks'])==1
    assert value['blocks'][0]['record_count']<=256
    assert value['indexed_size']<=256*1024+65536
    assert value['indexed_size']<path.stat().st_size
    # No complete historical context is asserted while the live writer catches up.
    assert indexed_policy_records(directory,iso(BASE+300),iso(BASE+400)) is None


def test_policy_tamper_and_missing_predecessor_do_not_invent_context(tmp_path):
    directory=tmp_path/'hycube_policy_history';path=directory/f'{DAY}.jsonl'
    write_rows(path,[policy(BASE+300)])
    build_index(path,timestamp_field='observed_at',iso_timestamp=True)
    assert indexed_policy_records(directory,iso(BASE),iso(BASE+600)) is None
    path.write_text(path.read_text().replace('82','72'))
    assert indexed_policy_records(directory,iso(BASE+301),iso(BASE+600)) is None


def test_policy_midnight_uses_indexed_prior_day_without_backdating_change(tmp_path):
    directory=tmp_path/'hycube_policy_history'
    previous=directory/'2026-01-01.jsonl'
    current=directory/f'{DAY}.jsonl'
    write_rows(previous,[policy(BASE-300)])
    write_rows(current,[policy(BASE+300)])
    for path in (previous,current):
        build_index(path,timestamp_field='observed_at',iso_timestamp=True)
    values=HycubePolicyHistory(directory).query(timestamp_from=iso(BASE),timestamp_to=iso(BASE+600))
    assert values[0]['from']==iso(BASE)
    assert values[0]['observed_at']==iso(BASE-300)
    assert values[0]['to']==iso(BASE+300)
    assert values[1]['from']==iso(BASE+300)
