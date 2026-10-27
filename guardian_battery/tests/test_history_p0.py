"""Synthetic P0 regressions. These are not HA runtime measurements."""
import json
from datetime import datetime, timedelta, timezone
import pytest
from history_block_index import build_index
from history_series import CellHistorySeries, _ExtremaCollector
from history_series_contract import normalize_series
from history_append_receipt import before_append, after_append, signature, matches_prefix
from test_history_block_index import cell_record, write_jsonl


def test_append_receipt_distinguishes_append_rewrite_rotation_and_truncate(tmp_path):
    source = tmp_path / "day.jsonl"
    source.write_text("first\n")
    generation = before_append(source)
    after_append(source, generation)
    original = signature(source)
    generation = before_append(source)
    with source.open("a") as out: out.write("next\n")
    after_append(source, generation)
    assert matches_prefix(source, original)
    source.write_text("other\nnext\n")
    assert not matches_prefix(source, original)
    generation = before_append(source)
    with source.open("a") as out: out.write("tail\n")
    after_append(source, generation)
    assert not matches_prefix(source, original)
    current = signature(source)
    source.rename(tmp_path / "rotated")
    source.write_text("other\nnext\ntail\n")
    assert not matches_prefix(source, current)
    source.write_text("")
    assert not matches_prefix(source, current)


@pytest.mark.parametrize("modules", [(1,), (1,2,3,4,5,6)])
@pytest.mark.parametrize("metric", ["soc", "cell_voltage", "cell_temperature", "current"])
def test_same_24h_window_cost_independent_of_system_age(tmp_path, modules, metric):
    day = datetime(2026,9,1,tzinfo=timezone.utc)
    path = tmp_path / "2026-09-01.jsonl"
    records = [cell_record(day.timestamp()+minute*60, module, minute%100)
               for minute in range(360) for module in range(1,7)]
    write_jsonl(path, records)
    build_index(path,timestamp_field="timestamp",iso_timestamp=False)
    baseline = None
    for age in (1,10,30,365):
        for past in range(1,age):
            # Poison old files prove they are not opened/decoded, not merely fast.
            old = tmp_path / f"{(day-timedelta(days=past)).date()}.jsonl"
            if not old.exists(): old.write_text("INVALID OLD HISTORY\n")
        reader = CellHistorySeries(tmp_path, position_history_path=tmp_path/"positions")
        kwargs = dict(requests=[{"metric":metric,"cell_numbers":(2,5)}],
                      timestamp_from=day.isoformat(),
                      timestamp_to=(day+timedelta(days=1, microseconds=-1)).isoformat(),
                      module_numbers=modules)
        cold = reader.query_bundles(**kwargs)
        warm = reader.query_bundles(**kwargs)
        result = (cold["series"],cold["bytes_read"],cold["parsed_records"],cold["file_count"])
        if baseline is None: baseline = result
        assert result == baseline
        assert warm["series"] == cold["series"]
        assert warm["cache_hit"] is True


def test_sort_conflict_identity_and_explicit_gap_preserved():
    start="2026-09-01T00:00:00+00:00"; end="2026-09-01T01:00:00+00:00"
    a={"timestamp":start,"value":60,"module_serial":"A"}
    b={**a,"value":40}
    c={"timestamp":end,"value":30,"module_serial":"A"}
    result=normalize_series({"points":[c,a,b,a],"coverage":{"missing_intervals":[{"from":start,"to":end}]}},start,end)
    assert len(result["points"])==3
    assert all(p["conflict"] for p in result["points"][:2])
    assert result["points"][-1]["_segment_start"]


def test_downsampling_keeps_jump_edges_extrema_and_predecimation_gap():
    collector=_ExtremaCollector(4,0,1000,preserve_changes=True)
    for epoch,value in [(0,70),(1,70),(2,70),(3,20),(4,20),(100,20),(101,20),(102,90)]:
        collector.add({"timestamp":datetime.fromtimestamp(epoch,timezone.utc).isoformat(),
                       "_epoch":epoch,"value":value,"module_serial":"A"})
    points=collector.points()
    epochs={datetime.fromisoformat(p["timestamp"]).timestamp() for p in points}
    assert {2,3,4,100,101,102}<=epochs
    assert next(p for p in points if datetime.fromisoformat(p["timestamp"]).timestamp()==100)["_segment_start"]


def test_live_cycle_and_raw_backlog_preempt_history(tmp_path):
    import queue,time
    import history_live_priority as priority
    from history_api import HistoryApi
    from maintenance_api import ApiResponse
    from research_budget import check_budget
    api=HistoryApi(None,None)
    api._query=lambda _: ApiResponse(200,{"ok":True})
    class Writer:
        def __init__(self): self._queue=queue.Queue()
    writer=Writer();priority.register_raw_writer(writer)
    try:
        priority.cycle_started()
        assert api.handle("GET","/api/history/series").body["error"]["code"]=="live_acquisition_priority"
        priority.idle_until(time.monotonic()+5)
        assert api.handle("GET","/api/history/series").status==200
        writer._queue.put("pending raw record")
        assert api.handle("GET","/api/history/series").status==503
        writer._queue.get()
        def query(_):
            priority.cycle_started()
            check_budget()
            raise AssertionError("historical work continued during live cycle")
        api._query=query
        assert api.handle("GET","/api/history/series").body["error"]["code"]=="live_acquisition_priority"
    finally: priority.collector_stopped()


def test_socket_disconnect_probe_is_non_consuming():
    import socket
    from config_ui import Handler
    left,right=socket.socketpair()
    handler=object.__new__(Handler);handler.connection=left
    try:
        assert not handler._history_disconnected()
        right.sendall(b"x")
        assert not handler._history_disconnected()
        assert left.recv(1)==b"x"
        right.close()
        assert handler._history_disconnected()
    finally:
        left.close();right.close()


def test_nested_budget_cannot_extend_live_deadline(monkeypatch):
    from research_budget import query_budget, check_budget, ResearchQueryError
    import research_budget
    now=[10.]
    monkeypatch.setattr(research_budget.time,"monotonic",lambda:now[0])
    with pytest.raises(ResearchQueryError,match="timed out"):
        with query_budget(11.):
            with query_budget(None):
                with query_budget(100.):
                    now[0]=12.
                    check_budget()


def test_oversized_legacy_context_is_explicit_and_bounded():
    import io
    from history_live_priority import historical_slot, bounded_metadata_lines
    from research_budget import ResearchQueryError
    handle=io.StringIO("x"*1000000)
    with historical_slot():
        with pytest.raises(ResearchQueryError,match="context bound"):
            list(bounded_metadata_lines(handle))
    assert handle.tell()==65537


def test_invalid_timestamp_cannot_bridge_following_observation():
    start="2026-09-01T00:00:00+00:00"; end="2026-09-01T01:00:00+00:00"
    result=normalize_series({"points":[{"timestamp":stamp,"value":50,"module_serial":"A"}
        for stamp in (start,"invalid",end)]},start,end)
    assert len(result["invalid_points"])==1
    assert result["points"][-1]["_segment_start"]


def test_local_day_cell_file_at_utc_boundary_is_not_lost(tmp_path,monkeypatch):
    import time
    stamp=datetime(2026,9,1,23,tzinfo=timezone.utc)
    path=tmp_path/"2026-09-02.jsonl"
    write_jsonl(path,[cell_record(stamp.timestamp(),1,50)])
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ","Europe/Berlin");time.tzset()
            result=CellHistorySeries(tmp_path,position_history_path=tmp_path/"positions").query_bundles(
                requests=[{"metric":"soc"}],timestamp_from=stamp.isoformat(),timestamp_to=stamp.isoformat(),
                module_number=1,include_samples=False)
            assert [point["value"] for point in result["series"][0]["points"]]==[50]
    finally:time.tzset()


@pytest.mark.parametrize("modules", [(1,), (1,2,3,4,5,6)])
def test_combined_cell_and_bus_api_is_age_independent(tmp_path,modules,monkeypatch):
    from history_api import HistoryApi
    from rs485_evidence import Rs485HistorySeries
    day=datetime(2026,9,1,tzinfo=timezone.utc)
    cells=tmp_path/"cells";bus=tmp_path/"bus";cells.mkdir();bus.mkdir()
    rows=[cell_record(day.timestamp()+minute*60,module,minute%100) for minute in range(60) for module in range(1,7)]
    write_jsonl(cells/"2026-09-01.jsonl",rows)
    frames=[{"record_type":"frame","timestamp":datetime.fromtimestamp(row["timestamp"],timezone.utc).isoformat(),
        "adr":row["module"],"physical_serial":f"S-{row['module']}","position":row["module"],
        "position_history_id":"observed-test-epoch","decoded":{"charge_current_limit_a":12,"charge_enable":True}} for row in rows]
    write_jsonl(bus/"2026-09-01.jsonl",frames)
    build_index(cells/"2026-09-01.jsonl",timestamp_field="timestamp",iso_timestamp=False)
    build_index(bus/"2026-09-01.jsonl",timestamp_field="timestamp",iso_timestamp=True)
    class Overlay:
        def markers(self,*args,**kwargs):return []
    actual=json.loads;counts=[0]
    def measured(value,*args,**kwargs):
        result=actual(value,*args,**kwargs)
        if isinstance(result,dict) and ("soc_percent" in result or result.get("record_type")=="frame"):counts[0]+=1
        return result
    monkeypatch.setattr(json,"loads",measured)
    target="/api/history/series?metrics=soc,cell_voltage,cell_temperature,rs485_ccl,rs485_charge_enable&voltage_cell_numbers=2,5&temperature_cell_numbers=2,5&selected_modules="+",".join(map(str,modules))+"&from=2026-09-01T00:00:00Z&to=2026-09-01T23:59:59Z"
    expected=None;expected_decodes=None
    for age in (1,10,30,365):
        for ago in range(1,age):
            for directory in (cells,bus):
                path=directory/f"{(day-timedelta(days=ago)).date()}.jsonl"
                if not path.exists():path.write_text("UNREADABLE OLD HISTORY\n")
        api=HistoryApi(CellHistorySeries(cells,position_history_path=tmp_path/"positions"),Overlay(),
            rs485_series=Rs485HistorySeries(bus,position_history_path=tmp_path/"positions"))
        counts[0]=0;cold=api.handle("GET",target)
        assert cold.status==200,cold.body
        values=cold.body["series"]
        if expected is None:expected=values;expected_decodes=counts[0]
        assert values==expected and counts[0]==expected_decodes
        counts[0]=0;warm=api.handle("GET",target)
        assert warm.status==200 and warm.body["series"]==expected
        assert counts[0]<=expected_decodes
