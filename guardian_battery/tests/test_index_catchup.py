import json
from datetime import datetime, timezone
from pathlib import Path
import time
import pytest
import history_live_priority as priority
from cell_history import CellHistoryWriter
from history_block_index import index_path
from history_preparation import SelectedHistoryPreparation
from test_history_block_index import cell_record, write_jsonl

def test_backlog_catches_up_across_restart_one_block_per_append(tmp_path):
    base=datetime(2026,1,2,tzinfo=timezone.utc).timestamp()
    path=tmp_path/'2026-01-02.jsonl'
    write_jsonl(path,[cell_record(base+i) for i in range(1024)])
    original=path.read_bytes()
    writer=CellHistoryWriter(tmp_path)
    counts=[]
    for i in range(5):
        if i==2: writer=CellHistoryWriter(tmp_path)
        writer.append(cell_record(base+1024+i))
        idx=json.loads(index_path(path).read_text())
        counts.append(sum(b['record_count'] for b in idx['blocks']))
    assert counts==[256,512,768,1024,1024]
    assert path.read_bytes().startswith(original)

def test_closed_selection_leaves_open_neighbor_with_writer(tmp_path):
    base=datetime(2026,1,2,tzinfo=timezone.utc).timestamp()
    for day in (1,2,3):
        write_jsonl(tmp_path/'cell_history'/f'2026-01-0{day}.jsonl',
                    [cell_record(base+(day-2)*86400+i) for i in range(300)])
    current=tmp_path/'cell_history/2026-01-03.jsonl'
    raw=current.read_bytes()
    priority.cycle_started();priority.idle_until(time.monotonic()+120)
    try:
        worker=SelectedHistoryPreparation(tmp_path,clock=lambda:base+86400+100)
        worker.submit(dict(timestamp_from='2026-01-02T12:00:00Z',timestamp_to='2026-01-02T12:05:00Z',modules=[1],components=['cell_index'],shared_day_artifacts=True))
        for _ in range(12):
            result=worker.step()
            if result['state'] in ('complete','blocked'): break
        assert result['state']=='complete'
        assert result['tasks'][-1]['state']=='writer_owned'
        assert current.read_bytes()==raw and not index_path(current).exists()
        assert all(index_path(tmp_path/'cell_history'/f'2026-01-0{d}.jsonl').exists() for d in (1,2))
    finally: priority.collector_stopped()
