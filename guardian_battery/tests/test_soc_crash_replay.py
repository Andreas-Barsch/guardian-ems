import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from research_api import GuardianResearchApi, ResearchPaths
from soc_crash_detector import SocCrashDetector, SocObservation
from soc_crash_replay import ReplayInputError, replay_saved_pwr

BASE = datetime(2026, 9, 11, tzinfo=timezone.utc).timestamp()


def source(tmp_path, frames):
    path = tmp_path / "source" / "2026-09-11.jsonl"; path.parent.mkdir()
    rows = [{"schema_version": 1, "timestamp": BASE+t+n/10, "pwr_sample_at": BASE+t,
             "module": n, "module_serial": "S"+str(n), "soc_percent": soc,
             "current_a": -2, "voltages_mv": [3300]*15}
            for t, values in frames for n, soc in enumerate(values, 1)]
    path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    return path


def run(path, **kwargs):
    return replay_saved_pwr([path], start="2026-09-11T00:00:00Z", end="2026-09-11T00:10:00Z",
                            expected_modules=[1,2], **kwargs)


@pytest.mark.parametrize("drop,when,count", [(4.9,100,0),(5,100,1),(5.1,100,1),(5.1,299,1),(5.1,300,1),(5.1,301,0)])
def test_gap_detector_boundaries_and_original_pwr_time(tmp_path, drop, when, count):
    path = source(tmp_path, [(0,[50,60]),(10,[50,49]),(10+when,[50,50-drop])])
    before=path.read_bytes(); result=run(path)
    assert result["mode"] == "dry_run" and result["appended"] == 0
    assert result["crashes"] == count and path.read_bytes()==before
    if count:
        assert result["events"][0]["candidate_soc_at_start"] == 49
        assert result["events"][0]["confirmed_at"] == datetime.fromtimestamp(BASE+10+when,timezone.utc).isoformat(timespec="microseconds")
    assert not (path.parent / "soc_crash_events.jsonl").exists()


def test_tie_and_role_change_are_the_same_live_detector(tmp_path):
    frames=[(0,[50,50]),(10,[50,49]),(100,[42,43]),(120,[42,37]),(200,[42,31])]
    path=source(tmp_path,frames); result=run(path)
    detector=SocCrashDetector(); expected=[]
    for t,values in frames:
        expected.extend(e.to_dict() for e in detector.observe([SocObservation(n,soc,BASE+t,"S"+str(n)) for n,soc in enumerate(values,1)]))
    assert result["events"]==expected and result["crashes"]==1
    assert result["events"][0]["observation_started_at"]==datetime.fromtimestamp(BASE+120,timezone.utc).isoformat(timespec="microseconds")


def test_restart_dedup_and_end_to_end_event_timeseries_coverage(tmp_path):
    path=source(tmp_path,[(0,[50,60]),(10,[50,49]),(110,[50,43])])
    output=tmp_path/'test-store'/'soc_crash_events.jsonl'
    first=run(path,event_output=output); before=output.read_bytes()
    second=run(path,event_output=output)
    assert first["appended"]==1 and second["appended"]==0 and before==output.read_bytes()
    assert first["events"]==second["events"]
    api=GuardianResearchApi(ResearchPaths(path.parent,tmp_path/'positions',tmp_path/'maintenance',tmp_path/'canonical',tmp_path/'daily',soc_crash_events=output))
    event=first["events"][0]
    response=api.handle('GET','/api/research/soc-crash-events/'+event['event_id'])
    assert response.status==200 and response.body['data']['event']==event
    result=api.handle('GET','/api/research/module-history?physical_serial=S2&metric=soc&from=2026-09-11T00:00:00Z&to=2026-09-11T00:03:00Z')
    assert result.status==200 and [p['value'] for p in result.body['data']['points']]==[60,49,43]
    assert result.body['data']['unit']=='%' and result.body['data']['coverage']['quality']=='partial'


def test_incomplete_or_missing_pwr_blocks_before_writes(tmp_path):
    path=source(tmp_path,[(0,[50,60]),(10,[50,49]),(110,[50,43])])
    rows=[json.loads(line) for line in path.read_text().splitlines()]; rows[-1]["module"]=3
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    output=tmp_path/'test-store'/'soc_crash_events.jsonl'
    with pytest.raises(ReplayInputError,match='incomplete'):
        run(path,event_output=output)
    assert not output.exists()
    rows[0].pop('pwr_sample_at');path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    with pytest.raises(ReplayInputError,match='no original PWR'):
        run(path)


def test_null_identity_is_metadata_not_extra_crash_condition(tmp_path):
    path=source(tmp_path,[(0,[50,50]),(10,[50,49]),(110,[50,43])])
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    for row in rows: row['module_serial']=None
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=run(path)
    assert result['crashes']==1 and result['events'][0]['previous_weakest_module'] is None
    assert result['events'][0]['candidate_physical_serial'] is None


def test_historical_identity_fallback_and_conflicts_are_reported(tmp_path):
    from test_research_api import snapshot
    path=source(tmp_path,[(0,[50,50]),(10,[50,49]),(110,[50,43])])
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:row['module_serial']=None
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    positions=tmp_path/'positions.jsonl'
    positions.write_text(json.dumps(snapshot('2026-09-10T00:00:00Z',{1:'S1',2:'HISTORICAL-S2'}).to_dict())+'\n')
    result=run(path,position_history=positions)
    assert result['events'][0]['candidate_physical_serial']=='HISTORICAL-S2'
    assert result['identity_conflicts']==0 and result['position_history_source']['sha256']
    for row in rows:row['module_serial']='RECORDED'+str(row['module'])
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=run(path,position_history=positions)
    assert result['identity_conflicts']==6
    assert result['events'][0]['candidate_physical_serial']=='RECORDED2'


def test_production_and_source_output_paths_rejected_before_writes(tmp_path):
    path=source(tmp_path,[(0,[50,60])])
    for output in ['/Volumes/share/guardian_battery/soc_crash_events.jsonl',path.parent/'soc_crash_events.jsonl']:
        with pytest.raises(ReplayInputError,match='separate local'):
            run(path,event_output=output)


def test_inventory_continues_across_source_files_and_is_deterministic(tmp_path):
    from soc_crash_replay import audit_saved_history
    path=source(tmp_path,[(0,[50,60]),(10,[50,49]),(110,[50,43])])
    lines=path.read_text().splitlines(keepends=True)
    path.write_text(''.join(lines[:4]))
    (path.parent/'2026-09-12.jsonl').write_text(''.join(lines[4:]))
    first=audit_saved_history(path.parent,expected_modules=[1,2])
    second=audit_saved_history(path.parent,expected_modules=[1,2])
    assert first['events']==second['events'] and first['crashes']==1
    assert first['complete_frames_replayed']==3 and len(first['sections'])==1
    assert first['appended']==0


def test_inventory_single_module_suspends_without_fake_frame_or_reset(tmp_path):
    from soc_crash_replay import audit_saved_history
    path=source(tmp_path,[(0,[50,60]),(10,[50,49]),(50,[50]),(110,[50,43])])
    result=audit_saved_history(path.parent,expected_modules=[1,2])
    assert result['incomplete_or_unusable_groups']==1
    assert len(result['sections'])==1 and result['crashes']==1
    assert result['candidates_started']==1


def test_inventory_missing_pwr_prefix_is_reported_without_invention(tmp_path):
    from soc_crash_replay import audit_saved_history
    path=source(tmp_path,[(0,[50,60]),(10,[50,49]),(110,[50,43])])
    rows=[json.loads(v) for v in path.read_text().splitlines()]
    rows[0].pop('pwr_sample_at')
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=audit_saved_history(path.parent,expected_modules=[1,2])
    assert result['missing_pwr_rows']==1
    assert result['complete_frames_replayed']==2 and result['crashes']==0


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_inventory_default_accepts_observed_groups_without_topology_requirement(tmp_path, count):
    from soc_crash_replay import audit_saved_history
    path = source(tmp_path, [(0, [50] + [60] * (count-1)),
        (10, [50, 49] + [60] * (count-2)), (110, [50, 43] + [60] * (count-2))])
    before = path.read_bytes()
    result = audit_saved_history(path.parent)
    assert result["selection_policy"] == "observed_min_two" and result["expected_modules"] is None
    assert result["frames_replayed"] == 3 and result["crashes"] == 1
    assert result["complete_frames_replayed"] is None  # no physical completeness assertion
    assert result["historical_completeness"] == "unverified" and result["provisional"] is True
    assert result["event_observation_context"][0]["confirmation_modules"] == list(range(1,count+1))
    assert result["appended"] == 0 and path.read_bytes() == before
    assert not list(path.parent.glob("*.lock")) and not (path.parent / "soc_crash_events.jsonl").exists()


def membership_source(tmp_path, frames):
    path = tmp_path / "2026-09-11.jsonl"
    path.write_text("".join(json.dumps({"timestamp":BASE+t+.1, "pwr_sample_at":BASE+t,
        "module":module, "soc_percent":soc})+"\n" for t, members in frames for module,soc in members.items()))
    return path


def test_membership_change_preserves_candidate_when_existing_detector_does(tmp_path):
    from soc_crash_replay import audit_saved_history
    path = membership_source(tmp_path, [(0,{1:50,2:60,3:80}),
        (10,{1:50,2:49}), (110,{1:50,2:43,3:80})])
    result = audit_saved_history(path.parent)
    assert result["crashes"] == 1 and len(result["sections"]) == 1
    assert result["events"][0]["candidate_soc_at_start"] == 49
    assert len(result["membership_changes"]) == 2
    assert result["membership_changes"][0]["observed_modules"] == [1,2]
    assert result["events"][0]["candidate_physical_serial"] is None
    assert audit_saved_history(path.parent)["events"] == result["events"]


def test_missing_incumbent_uses_existing_detector_reset_not_physical_removal_assumption(tmp_path):
    from soc_crash_replay import audit_saved_history
    path = membership_source(tmp_path, [(0,{1:50,2:60,3:70}), (10,{1:50,2:49,3:70}),
        (50,{1:48,3:70}), (110,{1:48,2:43,3:70}), (120,{1:48,2:37,3:70})])
    result = audit_saved_history(path.parent)
    assert len(result["sections"]) == 1 and result["crashes"] == 1
    event = result["events"][0]
    assert event["candidate_soc_at_start"] == 43
    assert event["observation_started_at"] == datetime.fromtimestamp(BASE+110,timezone.utc).isoformat(timespec="microseconds")
    assert event["confirmed_at"] == datetime.fromtimestamp(BASE+110,timezone.utc).isoformat(timespec="microseconds")


def test_new_observed_weakest_cancels_old_before_threshold_check(tmp_path):
    from soc_crash_replay import audit_saved_history
    path = membership_source(tmp_path, [(0,{1:50,2:60}), (10,{1:50,2:49}),
        (110,{1:50,2:43,3:40}), (150,{1:50,2:42,3:34})])
    result = audit_saved_history(path.parent)
    assert result["crashes"] == 1
    event = result["events"][0]
    assert event["candidate_module"] == 3 and event["candidate_soc_at_start"] == 40
    assert event["confirmed_at"] == datetime.fromtimestamp(BASE+150,timezone.utc).isoformat(timespec="microseconds")


def test_single_module_remains_comparison_pause_with_original_start(tmp_path):
    from soc_crash_replay import audit_saved_history
    path = source(tmp_path, [(0,[50,60]), (10,[50,49]), (50,[50]), (110,[50,43])])
    result = audit_saved_history(path.parent)
    assert result["suspended_groups"] == 1 and len(result["sections"]) == 1
    assert result["crashes"] == 1 and result["frames_replayed"] == 3


def test_inventory_daily_counts_separate_utc_and_berlin_dates(tmp_path):
    from soc_crash_replay import audit_saved_history
    path = source(tmp_path, [(23*3600,[50,60]), (23*3600+10,[50,49]), (23*3600+110,[50,43])])
    result = audit_saved_history(path.parent)
    assert result["daily"]["UTC"]["2026-09-11"]["crashes"] == 1
    assert result["daily"]["Europe/Berlin"]["2026-09-12"]["crashes"] == 1
    assert "2026-09-11" not in result["daily"]["Europe/Berlin"]


def test_cli_defaults_to_minimum_two_without_modules_argument(tmp_path, capsys):
    import runpy
    path = source(tmp_path, [(0,[50,60]), (10,[50,49]), (110,[50,43])])
    cli = runpy.run_path(str(Path(__file__).parents[1] / "tools" / "replay_soc_crashes.py"))
    assert cli["main"](["--saved-directory", str(path.parent)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["selection_policy"] == "observed_min_two" and result["crashes"] == 1
    assert result["appended"] == 0


def test_live_replay_same_ids_with_suspension_and_immediate_confirmation(tmp_path):
    from soc_crash_replay import audit_saved_history
    frames=[(0,{1:30,2:40}),(10,{1:30,2:27}),(100,{2:26}),(200,{1:32,2:26}),
            (250,{1:20,2:26}),(300,{1:20,2:26})]
    path=membership_source(tmp_path,frames)
    result=audit_saved_history(path.parent)
    live=SocCrashDetector();events=[]
    for t,values in frames:
        events.extend(e.to_dict() for e in live.observe([SocObservation(m,s,BASE+t) for m,s in values.items()]))
    assert result["events"]==events and len(events)==2
    assert result["candidates_started"]==live.candidates_started==2
    assert events[1]["observation_started_at"]==events[1]["confirmed_at"]
    assert result["appended"]==0 and not list(tmp_path.glob("*.lock"))


def test_replay_suspension_keeps_original_expiry(tmp_path):
    from soc_crash_replay import audit_saved_history
    path=membership_source(tmp_path,[(0,{1:30,2:40}),(10,{1:30,2:27}),
        (100,{2:26}),(311,{1:32,2:26})])
    result=audit_saved_history(path.parent)
    assert result["crashes"]==0 and result["candidates_started"]==1
