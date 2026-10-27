"""Versioned gap events coexist with unchanged historical eigen-drop events."""
import json
from dataclasses import replace
from datetime import datetime, timezone
import pytest
from soc_crash_detector import SocObservation
from soc_crash_events import (SocGapEvent, SocCrashEventStore, make_gap_event,
    read_soc_event, DETECTOR_VERSION, LEGACY_DETECTOR_VERSION, EVENT_COLLECTION_VERSION)
from test_soc_crash_events import historical_equal_values
from test_soc_crash_api import api_at, get


def gap_event():
    return make_gap_event(SocObservation(2,20,10,'C'),None,SocObservation(2,20,10,'C'),
        [SocObservation(3,25,10,None),SocObservation(1,25,10,'R')],5,300)


@pytest.mark.parametrize('gap,valid',[(4.9,False),(5,True),(5.1,True)])
def test_new_gap_threshold_allows_equality_even_without_eigen_drop(gap,valid):
    args=(SocObservation(2,20,10),None,SocObservation(2,20,10),[SocObservation(1,20+gap,10)],5,300)
    if valid:
        event=make_gap_event(*args)
        assert event.observed_gap_pp==gap and event.schema_version==2
        assert event.candidate_soc_at_start==event.candidate_soc_at_confirmation
    else:
        with pytest.raises(ValueError,match='threshold'):make_gap_event(*args)


def test_second_minimum_ties_are_sorted_and_serials_remain_metadata():
    event=gap_event();values=event.to_dict()
    assert values['reference_modules_at_confirmation']==[1,3]
    assert values['reference_serials_at_confirmation']==['R',None]
    assert values['second_lowest_soc_at_confirmation']==25
    assert 'observed_drop_pp' not in values and 'reference_module' not in values
    assert read_soc_event(json.loads(json.dumps(values)))==event
    assert replace(event,candidate_physical_serial=None).event_id==event.event_id


@pytest.mark.parametrize('changes',[
    {'observed_gap_pp':4.9},{'observed_gap_pp':float('nan')},
    {'candidate_module':None},{'candidate_module':True},{'candidate_module':1},
    {'reference_modules_at_confirmation':(3,1)}, {'reference_modules_at_confirmation':()},
    {'reference_serials_at_confirmation':('R',)}, {'second_lowest_soc_at_confirmation':24},
    {'confirmed_at':'1970-01-01T00:05:11+00:00'}, {'event_id':'SCS-'+'0'*64},
    {'detector_version':LEGACY_DETECTOR_VERSION}, {'schema_version':1},
    {'previous_weakest_physical_serial':'invented'}, {'causality':'determined'},
])
def test_gap_event_validation_remains_strict(changes):
    with pytest.raises(ValueError):replace(gap_event(),**changes)


def test_mixed_store_and_api_preserve_old_bytes_ids_and_semantics(tmp_path):
    old=historical_equal_values();raw=(json.dumps(old)+'\n').encode()
    api=api_at(tmp_path);path=api.paths.soc_crash_events;path.write_bytes(raw)
    store=SocCrashEventStore(path);new=gap_event()
    assert store.append(new) and not store.append(new)
    assert path.read_bytes().startswith(raw)
    reader=SocCrashEventStore(path)
    assert reader.get(old['event_id']).to_dict()==old
    assert reader.get(new.event_id)==new
    listed=get(api,'soc-crash-events?from=1970-01-01T00:00:00Z&to=1970-01-02T00:00:00Z')
    assert listed.status==200 and listed.body['semantics_version']==EVENT_COLLECTION_VERSION
    assert {e['schema_version'] for e in listed.body['data']['events']}=={1,2}
    for event_id,version in [(old['event_id'],LEGACY_DETECTOR_VERSION),(new.event_id,DETECTOR_VERSION)]:
        response=get(api,'soc-crash-events/'+event_id)
        assert response.body['semantics_version']==version
        assert response.body['data']['event']==reader.get(event_id).to_dict()
    with pytest.raises(ValueError,match='threshold'):store.append(reader.get(old['event_id']))
    assert path.read_bytes().startswith(raw)


def test_new_namespace_and_immutable_payload_dedup(tmp_path):
    old=historical_equal_values();event=gap_event()
    assert event.event_id!=old['event_id']
    store=SocCrashEventStore(tmp_path/'events.jsonl');assert store.append(event)
    before=store.path.read_bytes()
    with pytest.raises(ValueError,match='immutable'):
        store.append(replace(event,reference_serials_at_confirmation=('CHANGED',None)))
    assert store.path.read_bytes()==before
