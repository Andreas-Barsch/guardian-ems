"""Confirmed gap rule; legacy eigen-drop persistence is tested separately."""
import inspect
from dataclasses import fields
import pytest
from soc_crash_detector import SocCrashDetector, SocObservation


def frame(detector, timestamp, first, second, serials=(None,None)):
    return detector.observe([SocObservation(1,first,timestamp,serials[0]),
                             SocObservation(2,second,timestamp,serials[1])])


def started(**policy):
    d=SocCrashDetector(**policy)
    assert frame(d,0,50,60)==[]
    assert frame(d,10,50,49,("R","C"))==[]
    return d


@pytest.mark.parametrize('gap', [4.9,5.0,5.1])
@pytest.mark.parametrize('elapsed', [0,100,299,300,301])
def test_gap_and_elapsed_boundaries(gap,elapsed):
    if elapsed==0:
        d=SocCrashDetector();frame(d,0,50,60)
        result=frame(d,10,50,50-gap)
    else:
        d=started();result=frame(d,10+elapsed,50,50-gap)
    assert bool(result)==(gap>=5 and elapsed<=300)
    if result:
        assert result[0].observed_gap_pp==gap
        assert result[0].causality=='not_determined'
        assert d.candidates_started==1


def test_initial_unique_minimum_is_baseline_not_candidate():
    d=SocCrashDetector()
    assert frame(d,0,20,40)==[]
    assert frame(d,10,10,40)==[]
    assert d.candidates_started==0


def test_immediate_confirmation_needs_no_further_eigen_drop():
    d=SocCrashDetector();frame(d,0,50,60)
    event,=frame(d,10,50,40)
    assert event.candidate_soc_at_start==event.candidate_soc_at_confirmation==40
    assert event.observation_started_at==event.confirmed_at
    assert event.observed_gap_pp==10
    assert frame(d,20,50,30)==[]


@pytest.mark.parametrize('winner',[1,2,3])
def test_initial_tie_waits_without_arbitrary_selection(winner):
    d=SocCrashDetector()
    assert d.observe([SocObservation(m,50,0) for m in (3,1,2)])==[]
    assert d.weakest_module is None
    result=d.observe([SocObservation(m,45 if m==winner else 50,10) for m in (3,1,2)])
    event,=result
    assert event.candidate_module==winner and event.previous_weakest_module is None
    assert event.reference_modules_at_confirmation==tuple(m for m in (1,2,3) if m!=winner)


def test_later_new_tied_minima_wait_until_unique_and_then_start_fresh_clock():
    d=SocCrashDetector()
    def observe(t,values):return d.observe([SocObservation(m,s,t) for m,s in enumerate(values,1)])
    assert observe(0,[30,40,40])==[]
    assert observe(10,[35,20,20])==[]
    assert d.weakest_module is None and d.candidates_started==0
    event,=observe(320,[35,14,19])
    assert event.observation_started_at==event.confirmed_at
    assert event.candidate_soc_at_start==14 and d.candidates_started==1


def test_later_incumbent_tie_preserves_candidate_start():
    d=started();assert frame(d,20,48,48)==[]
    assert d.weakest_module==2
    event,=frame(d,30,48,43)
    assert event.candidate_soc_at_start==49
    assert event.observation_started_at=='1970-01-01T00:00:10.000000+00:00'


def test_role_change_cancels_old_before_new_immediate_confirmation():
    d=started()
    event,=frame(d,20,38,43)
    assert event.candidate_module==1 and event.candidate_soc_at_start==38
    assert event.previous_weakest_module==2
    assert d.candidates_started==2


def test_current_second_minimum_changes_without_restarting_clock():
    d=SocCrashDetector()
    def obs(t,a,b,c):return d.observe([SocObservation(1,a,t,'A'),SocObservation(2,b,t,'B'),SocObservation(3,c,t,'C')])
    obs(0,30,40,50);assert obs(10,30,28,40)==[]
    assert obs(20,35,28,32)==[]
    event,=obs(30,35,28,33)
    assert event.candidate_soc_at_start==event.candidate_soc_at_confirmation==28
    assert event.reference_modules_at_confirmation==(3,)
    assert event.reference_serials_at_confirmation==('C',)
    assert event.previous_weakest_module==1
    assert event.observation_started_at=='1970-01-01T00:00:10.000000+00:00'


def test_single_module_suspends_comparison_without_restarting_deadline():
    d=SocCrashDetector();frame(d,0,30,40);frame(d,10,30,27)
    assert d.observe([SocObservation(2,26,100)])==[]
    event,=frame(d,200,32,26)
    assert event.candidate_soc_at_start==27 and event.observed_gap_pp==6
    assert event.observation_started_at=='1970-01-01T00:00:10.000000+00:00'


def test_single_module_does_not_extend_expired_window_or_change_role():
    d=started()
    assert d.observe([SocObservation(1,10,100)])==[]
    assert d.weakest_module==2  # absent observation is not removal
    assert d.observe([SocObservation(2,26,311)])==[]
    assert frame(d,400,50,26)==[]
    assert d.candidates_started==1


def test_missing_incumbent_with_two_other_values_seeds_existing_baseline_path():
    d=started()
    assert d.observe([SocObservation(1,30,20),SocObservation(3,40,20)])==[]
    assert d.weakest_module==1 and d._pending is None


def test_no_rearm_after_confirmation_or_expiry_without_role_change():
    d=started();assert len(frame(d,20,50,45))==1
    assert frame(d,30,50,49)==[] and frame(d,40,50,40)==[]
    expired=started();assert frame(expired,311,50,40)==[]
    assert frame(expired,320,50,30)==[]
    assert len(frame(expired,330,20,30))==1


def test_identity_is_metadata_candidate_frozen_reference_current():
    d=started();e,=frame(d,20,50,45,('R-NOW','C-NOW'))
    assert e.candidate_physical_serial=='C'
    assert e.previous_weakest_physical_serial=='R'
    assert e.reference_serials_at_confirmation==('R-NOW',)


def test_duplicate_older_and_empty_input_cannot_reconfirm():
    d=started()
    assert frame(d,10,50,40)==[] and frame(d,9,50,40)==[]
    assert d.observe([])==[]
    assert len(frame(d,11,50,40))==1
    assert frame(d,11,50,40)==[]


def test_configuration_never_reinterprets_legacy_drop_value(caplog):
    d=SocCrashDetector.from_options({'soc_crash_drop_pp':12,'soc_crash_window_seconds':17})
    assert d.x_pp==5 and d.y_seconds==17
    assert 'not used for gap detection' in caplog.text
    assert SocCrashDetector.from_options({'soc_crash_gap_pp':2.5}).x_pp==2.5


def test_observation_interface_has_no_diagnostic_inputs():
    assert {field.name for field in fields(SocObservation)}=={'module','soc','timestamp','physical_serial'}
    assert set(inspect.signature(SocCrashDetector).parameters)=={'x_pp','y_seconds'}


@pytest.mark.parametrize('policy',[dict(x_pp=0),dict(x_pp=101),dict(x_pp=float('nan')),dict(x_pp=True),dict(y_seconds=0),dict(y_seconds=86401),dict(y_seconds=1.5)])
def test_invalid_policy_rejected(policy):
    with pytest.raises(ValueError):SocCrashDetector(**policy)


@pytest.mark.parametrize('soc',[float('nan'),float('inf'),-1,101,True])
def test_invalid_observation_rejected(soc):
    with pytest.raises(ValueError):SocObservation(1,soc,1)
