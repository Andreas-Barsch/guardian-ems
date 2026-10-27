import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from history_block_index import build_index
from research_timeseries import observation_coverage, ResearchTimeseriesService, ResearchQueryError
from test_research_api import environment, get


def test_edges_are_explicit_and_pagination_does_not_change_source_coverage(tmp_path):
    api, _, _ = environment(tmp_path)
    query = "module-history?physical_serial=SERIAL-M4&metric=soc&resolution=full&max_points=2&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z"
    first = get(api, query).body["data"]
    second = get(api, query + "&cursor=" + first["next_cursor"]).body["data"]
    assert first["coverage"] == second["coverage"]
    assert [g["kind"] for g in first["coverage"]["missing_intervals"]] == ["leading", "trailing"]
    assert first["unit"] == "%" and first["coverage"]["quality"] == "partial"
    complete = get(api, query.replace("09:00:00", "10:00:00").replace("12:00:00", "10:08:00"))
    assert complete.body["data"]["coverage"]["quality"] == "complete"
    assert complete.body["truncated"] is True  # source coverage != page completeness


def test_coverage_one_scan_for_many_metrics_no_point_or_epoch_expansion(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    api._refresh_identity()
    scans = []
    original = api.series._records
    def records(*args, **kwargs):
        scans.append(1)
        yield from original(*args, **kwargs)
    monkeypatch.setattr(api.series, "_records", records)
    monkeypatch.setattr(api.series, "query", lambda **kwargs: pytest.fail("full point query"))
    monkeypatch.setattr(api.identity, "position_at", lambda *args: pytest.fail("identity epoch expansion"))
    response = get(api, "coverage?physical_serial=SERIAL-M4&datasets=soc,module_current,cell_voltage&from=2026-09-11T09:00:00Z&to=2026-09-11T12:00:00Z")
    assert response.status == 200 and len(scans) == 1
    assert [r["unit"] for r in response.body["data"]["datasets"]] == ["%", "A", "mV"]
    assert all(r["sample_count"] == 5 for r in response.body["data"]["datasets"])


def test_missing_metric_is_absent_not_zero_and_zero_is_valid(tmp_path):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row.pop("soc_percent", None); row["current_a"] = 0
    path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    result = get(api, "coverage?physical_serial=SERIAL-M4&datasets=soc,module_current&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z").body["data"]["datasets"]
    assert result[0]["sample_count"] == 0 and result[0]["quality"] == "absent"
    assert result[1]["sample_count"] == 5 and result[1]["quality"] == "complete"


def test_existing_index_and_stale_fallback_match_without_writes(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    path = api.paths.cell_history / "2026-09-11.jsonl"
    query = "module-history?physical_serial=SERIAL-M4&metric=soc&from=2026-09-11T10:04:00Z&to=2026-09-11T10:08:00Z"
    plain = get(api, query).body
    build_index(path, timestamp_field="timestamp", iso_timestamp=False, block_records=2)
    index = Path(str(path)+".idx"); before = index.read_bytes()
    assert get(api, query).body == plain
    assert index.read_bytes() == before
    index.write_text("corrupt")
    assert get(api, query).body == plain
    assert index.read_text() == "corrupt"


def test_deadline_survives_fast_path_and_errors_are_timeout(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    api._refresh_identity()
    with pytest.raises(ResearchQueryError) as error:
        api.series.coverage(["soc"], "SERIAL-M4", "2026-09-11T00:00:00Z", "2026-09-12T00:00:00Z", deadline=-1)
    assert error.value.code == "timeout"
    def expired(*args, **kwargs): raise ResearchQueryError("timeout", "research query timed out", 503)
    monkeypatch.setattr(api.series, "coverage", expired)
    response = get(api, "coverage?physical_serial=SERIAL-M4&datasets=soc&from=2026-09-11T10:00:00Z&to=2026-09-11T10:08:00Z")
    assert response.status == 503 and response.body["error"]["code"] == "timeout"


def test_live_reported_boundary_example():
    begin = datetime.fromisoformat("2026-10-01T10:43:49+00:00")
    times = [begin.timestamp()+192.181513+i*59.5764 for i in range(8)]
    c = observation_coverage(begin.isoformat(), "2026-10-01T10:53:49+00:00", times)
    assert c["quality"] == "partial" and c["missing_intervals"][0]["kind"] == "leading"


def test_index_reduces_parsed_records_and_open_tail_remains_visible(tmp_path, monkeypatch):
    api, _, _ = environment(tmp_path)
    path=api.paths.cell_history/'2026-09-11.jsonl'
    base=datetime(2026,9,11,tzinfo=timezone.utc).timestamp()
    rows=[{'timestamp':base+i,'module':4,'module_serial':'SERIAL-M4','soc_percent':50} for i in range(10000)]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    from history_append_receipt import before_append, after_append
    after_append(path, before_append(path))
    build_index(path,timestamp_field='timestamp',iso_timestamp=False,block_records=256,complete=False)
    generation = before_append(path)
    with path.open('a') as f:f.write(json.dumps({'timestamp':base+10000,'module':4,'module_serial':'SERIAL-M4','soc_percent':49})+'\n')
    after_append(path, generation)
    seen=[]; original=json.loads
    def loads(value,*args,**kwargs):
        result=original(value,*args,**kwargs)
        if isinstance(result,dict) and 'soc_percent' in result:seen.append(1)
        return result
    monkeypatch.setattr('research_timeseries.json.loads',loads)
    response=get(api,'coverage?physical_serial=SERIAL-M4&datasets=soc&from=2026-09-11T02:46:30Z&to=2026-09-11T02:46:40Z')
    assert response.status==200
    assert response.body['data']['datasets'][0]['sample_count']==11
    assert len(seen)<512  # actual bounded block reads, not a timeout increase


def test_internal_gaps_and_empty_window_are_explicit():
    start='2026-09-11T00:00:00+00:00'; end='2026-09-11T00:10:00+00:00'
    base=datetime.fromisoformat(start).timestamp()
    c=observation_coverage(start,end,[base+i for i in [0,60,120,480,540,600]])
    assert c['quality']=='partial' and c['missing_intervals'][0]['kind']=='internal'
    assert c['largest_gap_seconds']==360
    assert observation_coverage(start,end,[])['quality']=='absent'


def test_local_daily_filename_matches_writer_at_utc_day_boundary(tmp_path, monkeypatch):
    import time
    api,_,_=environment(tmp_path)
    at=datetime.fromisoformat('2026-09-10T22:05:00+00:00').timestamp()
    (api.paths.cell_history/'2026-09-11.jsonl').write_text(json.dumps({'timestamp':at,'module':4,'module_serial':'SERIAL-M4','soc_percent':50})+'\n')
    try:
        with monkeypatch.context() as patch:
            patch.setenv('TZ','Europe/Berlin');time.tzset()
            response=get(api,'module-history?physical_serial=SERIAL-M4&metric=soc&from=2026-09-10T22:00:00Z&to=2026-09-10T22:10:00Z')
            assert response.status==200 and response.body['data']['point_count']==1
    finally:
        time.tzset()


@pytest.mark.parametrize("observed,quality,leading", [
    ("2026-09-11T08:00:00+00:00", "complete", False),
    ("2026-09-11T09:00:00+00:00", "complete", False),
    ("2026-09-11T09:30:00+00:00", "partial", True),
    (None, "absent", False),
])
def test_policy_coverage_uses_validity_intervals_not_observation_cadence(tmp_path, observed, quality, leading):
    from dataclasses import replace
    from hycube_evidence import HycubePolicyHistory, policy_observation
    from research_api import GuardianResearchApi
    api, _, _ = environment(tmp_path)
    policy = tmp_path / "policy"
    hycube = tmp_path / "hycube"
    hycube.mkdir()
    if observed:
        HycubePolicyHistory(policy).append(policy_observation(
            b'{"normalMode":82,"bufferMode":3,"emergency":10,"batProtection":5}',
            datetime.fromisoformat(observed).timestamp()))
    api = GuardianResearchApi(replace(api.paths, hycube_history=hycube, hycube_policy=policy))
    response = get(api, "coverage?physical_serial=SERIAL-M4&datasets=policy"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T10:00:00Z")
    assert response.status == 200
    row = response.body["data"]["datasets"][0]
    assert row["quality"] == quality and row["coverage_basis"] == "policy_intervals"
    assert row["expected_cadence_seconds"] is None and row["largest_gap_seconds"] is None
    assert row["sample_count"] == int(observed is not None)
    if observed:
        assert row["covered_intervals"] == [{"from": observed if leading else "2026-09-11T09:00:00+00:00",
                                             "to": "2026-09-11T10:00:00+00:00"}]
        assert row["first_observation"] == observed
        assert row["missing_intervals"] == ([{"from": "2026-09-11T09:00:00+00:00",
            "to": observed, "kind": "leading"}] if leading else [])
    else:
        assert row["covered_intervals"] == []
        assert row["missing_intervals"] == [{"from": "2026-09-11T09:00:00+00:00",
            "to": "2026-09-11T10:00:00+00:00", "kind": "entire_range"}]


@pytest.mark.parametrize("times", [[], ["09:30"], ["09:00", "09:01", "10:00"]])
def test_maintenance_events_do_not_imply_continuous_coverage_or_missing_measurements(tmp_path, times):
    from maintenance import MaintenanceEventLog, new_maintenance_event
    api, _, _ = environment(tmp_path)
    stamps = ["2026-09-11T" + t + ":00+00:00" for t in times]
    for stamp in stamps:
        MaintenanceEventLog(api.paths.maintenance).append(new_maintenance_event(
            occurred_at=stamp, category="maintenance", title="Inspection", affected_system="Battery",
            module_serial="SERIAL-M4", source={"kind": "manual"}))
    response = get(api, "coverage?physical_serial=SERIAL-M4&datasets=maintenance"
        "&from=2026-09-11T09:00:00Z&to=2026-09-11T10:00:00Z")
    assert response.status == 200
    row = response.body["data"]["datasets"][0]
    assert row["sample_count"] == len(times)
    assert row["first_observation"] == (stamps[0] if stamps else None)
    assert row["last_observation"] == (stamps[-1] if stamps else None)
    assert row["covered_intervals"] == row["missing_intervals"] == []
    assert row["expected_cadence_seconds"] is None and row["largest_gap_seconds"] is None
    assert row["quality"] == "unknown" and row["coverage_basis"] == "event_occurrences"
