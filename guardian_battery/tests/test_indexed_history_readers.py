import json
from datetime import datetime,timezone
import pytest
from source_catalog import SourceCatalog
from indexed_history_window import IndexedHistoryWindow
from test_source_catalog import registration,index_all,write


def hycube(stamp,capacity=42,kind='hycube_system_observation'):
    value={'schema_version':1,'record_type':kind,'received_at':datetime.fromtimestamp(stamp,timezone.utc).isoformat()}
    if kind=='hycube_system_observation':value['BatteryCapacity']=capacity
    else:value.update(battery_capacity=capacity,source_raw_end_offset=100)
    return value


@pytest.mark.parametrize('kind',['hycube_system_observation','hycube_history_projection'])
def test_hycube_original_bytes_and_inclusive_window(tmp_path,kind):
    c=SourceCatalog(tmp_path/'index.db');p=write(tmp_path/'arbitrary-name.jsonl',[hycube(i,kind=kind) for i in (99,100,101,102)])
    original=p.read_bytes();c.register(**registration(p,context={'record_format':kind}));index_all(c)
    window=IndexedHistoryWindow(c)
    with window.read(100,101) as rows:values=list(rows)
    assert [r.timestamp for _,r in values]==[100,101]
    assert window.metrics['decodes']==2 and window.metrics['files_opened']==1
    assert p.read_bytes()==original
    with c.connection() as db:assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0


@pytest.mark.parametrize('older_days',[0,29,364,729])
def test_window_cost_independent_of_older_registered_hycube_days(tmp_path,older_days):
    c=SourceCatalog(tmp_path/'index.db');stamp=1800000000
    for day in range(older_days+1):
        p=write(tmp_path/f'{day}.jsonl',[hycube(stamp-day*86400)])
        c.register(**registration(p,source=str(day),context={'record_format':'hycube_system_observation'}));index_all(c,str(day))
    window=IndexedHistoryWindow(c)
    with window.read(stamp,stamp) as rows:assert len(list(rows))==1
    m=window.metrics
    print('HYCUBE_WINDOW_SCALE '+json.dumps({'days':older_days+1,**m},sort_keys=True))
    assert m['decodes']==1 and m['files_opened']==1 and m['pages']==1
    assert m['raw_bytes']<256 and m['sql_vm_steps']<500


def test_no_unindexed_fallback_and_cancel_releases_snapshot(tmp_path):
    from work_scheduler import WorkScheduler,WorkCancelled
    import threading
    c=SourceCatalog(tmp_path/'index.db');p=write(tmp_path/'raw.jsonl',[hycube(100)])
    c.register(**registration(p,context={'record_format':'hycube_system_observation'}))
    w=IndexedHistoryWindow(c)
    from research_budget import ResearchQueryError
    with pytest.raises(ResearchQueryError,match='pending'):
        with w.read(99,101) as rows:list(rows)
    assert w.metrics['pending_sources']==1 and w.metrics['raw_bytes']==0
    index_all(c);stop=threading.Event();s=WorkScheduler()
    def query():
        with w.read(99,101) as rows:
            stop.set();list(rows)
    with pytest.raises(WorkCancelled):s.execute('manual',query,cancelled=stop.is_set,lane='read')
    with c.connection() as db:assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0


@pytest.mark.parametrize('change',['rotate','truncate','rewrite'])
def test_reader_rejects_changed_source_without_fallback(tmp_path,change):
    from research_budget import ResearchQueryError
    c=SourceCatalog(tmp_path/'index.db');p=write(tmp_path/'raw.jsonl',[hycube(100),hycube(101)])
    c.register(**registration(p,context={'record_format':'hycube_system_observation'}));index_all(c)
    window=IndexedHistoryWindow(c)
    with pytest.raises(ResearchQueryError):
        with window.read(100,101) as rows:
            if change=='rotate':p.rename(tmp_path/'rotated.jsonl');write(p,[hycube(100,3)])
            elif change=='truncate':p.write_bytes(b'')
            else:p.write_text(json.dumps(hycube(100,3))+'\n')
            list(rows)
    assert window.metrics['decodes']==0
    with c.connection() as db:assert db.execute('SELECT count(*) FROM window_snapshot').fetchone()[0]==0
