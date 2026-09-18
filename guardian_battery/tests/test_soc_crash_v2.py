import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from history_block_index import build_index
from position_history import PositionSnapshot
from research_api import GuardianResearchApi, ResearchPaths
from soc_crash_v2 import (DETECTOR_VERSION, INTEGRATION_METHOD,
                          POLICY_VERSION, SocCrashV2Policy,
                          discover_soc_crash_events, evaluate_soc_window)


SERIAL = "SERIAL-V2"
START = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def observations(socs=(70.0, 67.5, 65.0), currents=(-1.0, -1.0, -1.0),
                 gaps=(0, 60, 120), epochs=("E1", "E1", "E1")):
    return [{"timestamp": datetime.fromtimestamp(START.timestamp() + offset,
                timezone.utc).isoformat(), "physical_serial": SERIAL,
             "soc": soc, "current": current, "identity_epoch_id": epoch}
            for offset, soc, current, epoch in zip(gaps, socs, currents, epochs)]


def evaluate(rows=None, policy=None, duration=120):
    return evaluate_soc_window(physical_serial=SERIAL,
        observations=observations() if rows is None else rows,
        interval_start=START.isoformat(),
        interval_end=datetime.fromtimestamp(START.timestamp() + duration,
                                            timezone.utc).isoformat(),
        policy=policy or SocCrashV2Policy(reference_capacity_ah=100.0))


def derived(result):
    return result["evidence"]["DERIVED"]


def test_large_unexplained_drop_is_soc_crash_and_reproducible():
    first = evaluate()
    second = evaluate()
    assert first == second
    assert first["classification"] == "SOC_CRASH"
    assert first["detector_version"] == DETECTOR_VERSION
    assert first["policy_version"] == POLICY_VERSION
    assert first["integration_method"] == INTEGRATION_METHOD
    assert first["event_id"].startswith("SCE2-")
    assert derived(first)["observed_soc_drop_pp"] == 5
    assert derived(first)["discharged_ah"] == pytest.approx(1 / 30)
    assert set(first["evidence"]) == {"OBSERVED", "DERIVED"}


def test_normal_discharge_and_electrically_explained_large_drop_are_not_crash():
    normal = evaluate(observations((70, 68, 66), (-10, -10, -10)),
                      SocCrashV2Policy(reference_capacity_ah=10))
    explained = evaluate(observations((70, 67.5, 65), (-15, -15, -15)),
                         SocCrashV2Policy(reference_capacity_ah=10))
    assert normal["classification"] == "NORMAL"
    assert explained["classification"] == "SOC_DISCONTINUITY"
    assert derived(explained)["expected_soc_drop_pp"] == pytest.approx(5)
    assert derived(explained)["unexplained_soc_drop_for_classification_pp"] == 0


@pytest.mark.parametrize("drop,expected", [(5.0, "SOC_CRASH"),
                                             (4.999, "NORMAL")])
def test_observed_drop_threshold_boundaries(drop, expected):
    rows = observations((70, 70 - drop / 2, 70 - drop))
    assert evaluate(rows)["classification"] == expected


def test_unexplained_drop_threshold_exact_and_just_below():
    rows = observations((70, 67.5, 65), (-60, -60, -60))
    exact = evaluate(rows, SocCrashV2Policy(reference_capacity_ah=100,
        min_unexplained_fraction=0))
    below = evaluate(rows, SocCrashV2Policy(reference_capacity_ah=100,
        min_unexplained_fraction=0, min_unexplained_soc_drop_pp=3.001))
    assert derived(exact)["unexplained_soc_drop_pp"] == pytest.approx(3)
    assert exact["classification"] == "SOC_CRASH"
    assert below["classification"] == "SOC_DISCONTINUITY"


def test_unexplained_fraction_threshold_exact_and_just_below():
    rows = observations((70, 67, 64), (-90, -90, -90))
    exact = evaluate(rows, SocCrashV2Policy(reference_capacity_ah=100,
        min_unexplained_soc_drop_pp=0, min_unexplained_fraction=.5))
    below = evaluate(rows, SocCrashV2Policy(reference_capacity_ah=100,
        min_unexplained_soc_drop_pp=0, min_unexplained_fraction=.5001))
    assert derived(exact)["unexplained_fraction"] == pytest.approx(.5)
    assert exact["classification"] == "SOC_CRASH"
    assert below["classification"] == "SOC_DISCONTINUITY"


def test_sample_gap_and_minimum_sample_boundaries():
    exact_gap = evaluate(observations(gaps=(0, 120, 240)), duration=240)
    over_gap = evaluate(observations(gaps=(0, 121, 240)), duration=240)
    exact_samples = evaluate(observations())
    too_few = evaluate(observations()[:2])
    assert exact_gap["classification"] == "SOC_CRASH"
    assert exact_gap["maximum_sample_gap_s"] == 120
    assert over_gap["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "sample_gap_exceeded" in over_gap["reason_codes"]
    assert exact_samples["sample_count"] == 3
    assert too_few["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "insufficient_samples" in too_few["reason_codes"]


@pytest.mark.parametrize("field,reason", [
    ("soc", "soc_missing_or_invalid"),
    ("current", "current_missing_or_invalid"),
])
def test_missing_required_values_are_insufficient_not_zero(field, reason):
    rows = observations(); rows[1][field] = None
    result = evaluate(rows)
    assert result["classification"] == "INSUFFICIENT_EVIDENCE"
    assert reason in result["reason_codes"]
    assert derived(result)["discharged_ah"] is None


@pytest.mark.parametrize("capacity", [None, 0, -1, float("nan")])
def test_missing_or_invalid_reference_capacity_is_insufficient(capacity):
    result = evaluate(policy=SocCrashV2Policy(reference_capacity_ah=capacity))
    assert result["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "reference_capacity_unavailable_or_invalid" in result["reason_codes"]
    assert result["evidence"]["OBSERVED"]["soc_start"] == 70
    assert result["evidence"]["OBSERVED"]["soc_end"] == 65
    assert derived(result)["discharged_ah"] == pytest.approx(1 / 30)
    assert derived(result)["expected_soc_drop_pp"] is None


def test_charging_and_mixed_current_follow_proven_negative_discharge_convention():
    charging = evaluate(observations(currents=(1, 1, 1)))
    mixed = evaluate(observations(currents=(-1, 1, -1)))
    assert charging["classification"] == "SOC_DISCONTINUITY"
    assert "qualifying_discharge_absent" in charging["reason_codes"]
    assert derived(charging)["discharged_ah"] == 0
    assert derived(mixed)["discharged_ah"] == pytest.approx(1 / 60)


def test_minimum_discharge_current_boundary_is_inclusive():
    exact = evaluate(observations(currents=(-.2, -.2, -.2)))
    below = evaluate(observations(currents=(-.199, -.199, -.199)))
    assert exact["classification"] == "SOC_CRASH"
    assert derived(exact)["contains_qualifying_discharge"] is True
    assert below["classification"] == "SOC_DISCONTINUITY"
    assert "qualifying_discharge_absent" in below["reason_codes"]


def test_wrong_serial_nonchronological_and_missing_epoch_are_insufficient():
    wrong = observations(); wrong[1]["physical_serial"] = "OTHER"
    unordered = observations(gaps=(0, 120, 60))
    no_epoch = observations(epochs=("E1", None, "E1"))
    expected = ((wrong, "physical_serial_mismatch"),
                (unordered, "observations_not_chronological"),
                (no_epoch, "identity_epoch_unavailable"))
    for rows, reason in expected:
        result = evaluate(rows)
        assert result["classification"] == "INSUFFICIENT_EVIDENCE"
        assert reason in result["reason_codes"]


def test_identity_epoch_boundary_and_event_window_are_insufficient():
    boundary = evaluate(observations(epochs=("E1", "E1", "E2")))
    exact = evaluate(observations(gaps=(0, 120, 240)), duration=300)
    oversized = evaluate(duration=301)
    assert boundary["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "identity_epoch_boundary" in boundary["reason_codes"]
    assert exact["classification"] == "SOC_CRASH"
    assert oversized["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "event_window_exceeded" in oversized["reason_codes"]


def test_candidate_discovery_algorithm_finds_local_event_in_twenty_minutes():
    points = [(offset, 70, -1) for offset in range(0, 601, 60)]
    points += [(660, 67.5, -1), (720, 65, -1)]
    points += [(offset, 65, -1) for offset in range(780, 1201, 60)]
    result = discover_soc_crash_events(physical_serial=SERIAL,
        observations=detector_rows(points),
        policy=SocCrashV2Policy(reference_capacity_ah=100))
    crashes = [item for item in result["events"]
               if item["classification"] == "SOC_CRASH"]
    assert len(crashes) == 1
    assert crashes[0]["interval"]["duration_seconds"] <= 300


def test_candidate_discovery_finds_event_in_one_hour_and_is_deterministic():
    points = [(offset, 80, -1) for offset in range(0, 1801, 60)]
    points += [(1860, 77.5, -1), (1920, 75, -1)]
    points += [(offset, 75, -1) for offset in range(1980, 3601, 60)]
    rows = detector_rows(points)
    policy = SocCrashV2Policy(reference_capacity_ah=100)
    assert discover_soc_crash_events(physical_serial=SERIAL, observations=rows,
        policy=policy) == discover_soc_crash_events(physical_serial=SERIAL,
        observations=rows, policy=policy)
    assert sum(item["classification"] == "SOC_CRASH" for item in
               discover_soc_crash_events(physical_serial=SERIAL,
                   observations=rows, policy=policy)["events"]) == 1


def test_candidate_discovery_returns_two_non_overlapping_events():
    points = [(0, 70, -1), (60, 67.5, -1), (120, 65, -1),
              (600, 65, -1), (660, 62.5, -1), (720, 60, -1)]
    result = discover_soc_crash_events(physical_serial=SERIAL,
        observations=detector_rows(points),
        policy=SocCrashV2Policy(reference_capacity_ah=100))
    crashes = [item for item in result["events"]
               if item["classification"] == "SOC_CRASH"]
    assert len(crashes) == 2
    assert crashes[0]["interval"]["end"] < crashes[1]["interval"]["start"]


def test_long_normal_search_returns_no_events():
    rows = detector_rows([(offset, 70, -1) for offset in range(0, 3601, 60)])
    result = discover_soc_crash_events(physical_serial=SERIAL, observations=rows,
        policy=SocCrashV2Policy(reference_capacity_ah=100))
    assert result["classification"] == "NORMAL"
    assert result["events"] == []


def test_gap_and_identity_boundary_separate_candidate_discovery():
    points = [(0, 70, -1), (60, 68, -1), (240, 65, -1)]
    gap = discover_soc_crash_events(physical_serial=SERIAL,
        observations=detector_rows(points),
        policy=SocCrashV2Policy(reference_capacity_ah=100))
    epochs = discover_soc_crash_events(physical_serial=SERIAL,
        observations=detector_rows([(0, 70, -1), (60, 68, -1), (120, 65, -1)],
                                  ("E1", "E1", "E2")),
        policy=SocCrashV2Policy(reference_capacity_ah=100))
    assert gap["events"] == [] and gap["segments"] == 2
    assert epochs["events"] == [] and epochs["segments"] == 2


def test_candidate_duration_boundary_is_inclusive_and_overlong_is_not_emitted():
    exact = detector_rows([(0, 70, -1), (100, 69, -1), (200, 68, -1),
                           (300, 65, -1)])
    over = detector_rows([(0, 70, -1), (120, 69, -1), (240, 68, -1),
                          (360, 65, -1)])
    policy = SocCrashV2Policy(reference_capacity_ah=100)
    exact_result = discover_soc_crash_events(physical_serial=SERIAL,
        observations=exact, policy=policy)
    over_result = discover_soc_crash_events(physical_serial=SERIAL,
        observations=over, policy=policy)
    assert len(exact_result["events"]) == 1
    assert exact_result["events"][0]["interval"]["duration_seconds"] == 300
    assert over_result["events"] == []


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def api_environment(tmp_path, *, with_index=True, rows=None, capacity=None,
                    block_records=1):
    positions = tmp_path / "positions.jsonl"
    snapshot = PositionSnapshot(schema_version=1,
        position_history_id="PHS-" + str(uuid.uuid4()),
        effective_at="2026-09-17T00:00:00+00:00",
        created_at="2026-09-17T00:00:00+00:00",
        maintenance_event_id="MEV-" + str(uuid.uuid4()),
        positions={str(number): SERIAL if number == 4 else None for number in range(1, 7)})
    write_jsonl(positions, [snapshot.to_dict()])
    history = tmp_path / "cells"
    rows = rows or [{"timestamp": START.timestamp() + offset, "module": 4,
                     "module_serial": SERIAL, "soc_percent": soc, "current_a": -1}
                    for offset, soc in ((0, 70), (60, 67.5), (120, 65))]
    source = history / "2026-09-17.jsonl"; write_jsonl(source, rows)
    if with_index:
        build_index(source, timestamp_field="timestamp", iso_timestamp=False,
                    block_records=block_records)
    paths = ResearchPaths(history, positions, tmp_path / "maintenance.jsonl",
                          tmp_path / "canonical", tmp_path / "daily")
    policy = SocCrashV2Policy(reference_capacity_ah=capacity)
    return GuardianResearchApi(paths, cursor_secret=b"test",
                               soc_crash_v2_policy=policy), source


def endpoint(api, extra=""):
    return api.handle("GET", "/api/research/events/soc-crashes-v2?"
        f"physical_serial={SERIAL}&from=2026-09-17T12:00:00Z"
        "&to=2026-09-17T12:02:00Z&reference_capacity_ah=100" + extra)


def search_endpoint(api, seconds, extra=""):
    start = START.isoformat().replace("+00:00", "Z")
    end = datetime.fromtimestamp(START.timestamp() + seconds,
                                 timezone.utc).isoformat().replace("+00:00", "Z")
    return api.handle("GET", "/api/research/events/soc-crashes-v2?"
        f"physical_serial={SERIAL}&from={start}&to={end}" + extra)


def history_rows(points, epoch="E1"):
    del epoch
    return [{"timestamp": START.timestamp() + offset, "module": 4,
             "module_serial": SERIAL, "soc_percent": soc, "current_a": current}
            for offset, soc, current in points]


def detector_rows(points, epochs=None):
    epochs = epochs or ["E1"] * len(points)
    return [{"timestamp": datetime.fromtimestamp(START.timestamp() + offset,
                timezone.utc).isoformat(), "physical_serial": SERIAL, "soc": soc,
             "current": current, "identity_epoch_id": identity_epoch}
            for (offset, soc, current), identity_epoch in zip(points, epochs)]


def test_bounded_endpoint_uses_only_indexed_requested_day_and_preserves_v1(tmp_path,
                                                                           monkeypatch):
    api, source = api_environment(tmp_path)
    unrelated = api.paths.cell_history / "2020-01-01.jsonl"
    write_jsonl(unrelated, [{"timestamp": 1, "module_serial": SERIAL}] * 10_000)
    opened = []
    original = type(source).open
    def tracked(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(source), "open", tracked)
    response = endpoint(api)
    assert response.status == 200
    assert response.body["data"]["classification"] == "SOC_CRASH"
    assert unrelated not in opened
    v1 = api.handle("GET", "/api/research/events/soc-crashes?"
        f"physical_serial={SERIAL}&from=2026-09-17T12:00:00Z"
        "&to=2026-09-17T12:02:00Z")
    assert v1.body["data"]["thresholds"] == {"soc_loss_pp": 2,
        "sample_gap_seconds": 300, "discharge_current_below_a": -0.2,
        "merge_gap_seconds": 600}


def test_missing_index_fails_closed_as_insufficient_without_full_scan(tmp_path,
                                                                      monkeypatch):
    api, source = api_environment(tmp_path, with_index=False)
    original = type(source).open
    def reject_source_open(path, *args, **kwargs):
        if path == source:
            raise AssertionError("unindexed source must not be opened")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(source), "open", reject_source_open)
    response = endpoint(api)
    assert response.status == 200
    assert response.body["data"]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "bounded_index_unavailable" in response.body["data"]["reason_codes"]
    assert source.stat().st_size > 0


def test_stale_identity_snapshot_fails_closed_without_history_read(tmp_path,
                                                                   monkeypatch):
    api, source = api_environment(tmp_path)
    api.paths.position_history.write_text(
        api.paths.position_history.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    original = type(source).open
    def reject_source_open(path, *args, **kwargs):
        if path == source:
            raise AssertionError("history must not be read with stale identity")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(source), "open", reject_source_open)
    response = endpoint(api)
    assert response.status == 200
    assert response.body["data"]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert "bounded_identity_snapshot_unavailable" in response.body["data"]["reason_codes"]


def test_request_local_policy_override_does_not_mutate_production_policy(tmp_path):
    api, _ = api_environment(tmp_path)
    original = api.soc_crash_v2_policy
    first = endpoint(api, "&min_observed_soc_drop_pp=6").body["data"]
    second = endpoint(api).body["data"]
    assert first["classification"] == "NORMAL"
    assert second["classification"] == "SOC_CRASH"
    assert api.soc_crash_v2_policy == original
    assert first["effective_policy"]["min_observed_soc_drop_pp"] == 6


def test_production_policy_without_explicit_capacity_is_insufficient(tmp_path):
    api, _ = api_environment(tmp_path)
    response = api.handle("GET", "/api/research/events/soc-crashes-v2?"
        f"physical_serial={SERIAL}&from=2026-09-17T12:00:00Z"
        "&to=2026-09-17T12:02:00Z")
    assert response.body["data"]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert response.body["data"]["effective_policy"]["reference_capacity_ah"] is None


def test_non_finite_capacity_is_fail_visible_and_json_safe(tmp_path):
    api, _ = api_environment(tmp_path)
    response = api.handle("GET", "/api/research/events/soc-crashes-v2?"
        f"physical_serial={SERIAL}&from=2026-09-17T12:00:00Z"
        "&to=2026-09-17T12:02:00Z&reference_capacity_ah=nan")
    assert response.body["data"]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert response.body["data"]["effective_policy"]["reference_capacity_ah"] is None
    assert "reference_capacity_unavailable_or_invalid" in response.body["data"]["reason_codes"]
    json.dumps(response.body, allow_nan=False)


def test_long_bounded_api_search_reads_indexed_ranges_and_finds_local_event(tmp_path):
    points = [(offset, 70, -1) for offset in range(0, 601, 60)]
    points += [(660, 67.5, -1), (720, 65, -1)]
    points += [(offset, 65, -1) for offset in range(780, 1201, 60)]
    api, _ = api_environment(tmp_path, rows=history_rows(points), capacity=100,
                             block_records=4)
    response = search_endpoint(api, 1200)
    data = response.body["data"]
    assert response.status == 200
    assert len([item for item in data["events"]
                if item["classification"] == "SOC_CRASH"]) == 1
    assert data["search"]["read_mode"] == "indexed_chunk"
    assert data["search"]["ranges_read"] >= 1
    assert data["search"]["records_decoded"] >= data["search"]["records_materialized"]
    assert data["effective_policy"]["reference_capacity_ah"] == 100
    assert data["reference_capacity_provenance"] == "production_configuration"


def test_six_hour_bounded_search_finds_local_event_without_window_expansion(tmp_path):
    points = [(offset, 80, -1) for offset in range(0, 10_801, 60)]
    points += [(10_860, 77.5, -1), (10_920, 75, -1)]
    points += [(offset, 75, -1) for offset in range(10_980, 21_601, 60)]
    api, source = api_environment(tmp_path, rows=history_rows(points), capacity=100,
                                  block_records=8)
    data = search_endpoint(api, 21_600).body["data"]
    crashes = [item for item in data["events"]
               if item["classification"] == "SOC_CRASH"]
    assert len(crashes) == 1
    assert data["search"]["selected_bytes"] <= source.stat().st_size
    assert data["search"]["requested_from"] == "2026-09-17T12:00:00+00:00"
    assert data["search"]["requested_to"] == "2026-09-17T18:00:00+00:00"


def test_physical_read_scales_with_requested_search_not_unrelated_history(tmp_path,
                                                                          monkeypatch):
    points = [(offset, 70, -1) for offset in range(0, 3601, 60)]
    api, source = api_environment(tmp_path, rows=history_rows(points), capacity=100,
                                  block_records=4)
    unrelated = api.paths.cell_history / "2020-01-01.jsonl"
    write_jsonl(unrelated, [{"timestamp": value, "module_serial": SERIAL}
                            for value in range(20_000)])
    opened = []
    original = type(source).open
    def tracked(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(source), "open", tracked)
    twenty = search_endpoint(api, 1200).body["data"]["search"]
    hour = search_endpoint(api, 3600).body["data"]["search"]
    assert unrelated not in opened
    assert twenty["selected_bytes"] < hour["selected_bytes"] <= source.stat().st_size
    assert twenty["records_decoded"] < hour["records_decoded"]


def test_capacity_override_is_request_local_and_reports_provenance(tmp_path):
    api, _ = api_environment(tmp_path, capacity=10)
    production_before = search_endpoint(api, 120).body["data"]
    override = search_endpoint(api, 120, "&reference_capacity_ah=100").body["data"]
    production_after = search_endpoint(api, 120).body["data"]
    assert production_before["effective_policy"]["reference_capacity_ah"] == 10
    assert production_before["reference_capacity_provenance"] == "production_configuration"
    assert override["effective_policy"]["reference_capacity_ah"] == 100
    assert override["reference_capacity_provenance"] == "request_override"
    assert all(item["reference_capacity_provenance"] == "request_override"
               for item in override["events"])
    assert production_after["effective_policy"]["reference_capacity_ah"] == 10
    assert api.soc_crash_v2_policy.reference_capacity_ah == 10


def test_unset_production_capacity_searches_but_cannot_emit_crash(tmp_path):
    api, _ = api_environment(tmp_path, capacity=None)
    data = search_endpoint(api, 120).body["data"]
    assert data["search"]["records_materialized"] == 3
    assert data["classification"] == "INSUFFICIENT_EVIDENCE"
    assert all(item["classification"] != "SOC_CRASH" for item in data["events"])
