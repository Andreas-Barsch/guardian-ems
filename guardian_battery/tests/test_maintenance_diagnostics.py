from maintenance_diagnostics import (project_maintenance_boundaries,
                                     split_samples_at_boundaries)


def test_module_intervention_splits_trend_window_but_context_only_event_does_not():
    events = [
        {"maintenance_event_id": "MEV-1", "occurred_at": "2026-08-01T01:00:00+00:00",
         "category": "maintenance", "archived_at": None},
        {"maintenance_event_id": "MEV-2", "occurred_at": "2026-08-01T02:00:00+00:00",
         "category": "module_position_change", "archived_at": None},
    ]
    boundaries = project_maintenance_boundaries(events)
    assert [item["diagnostic_effect"] for item in boundaries] == [
        "maintenance_context", "operating_segment_boundary"]
    samples = [{"timestamp": f"2026-08-01T0{hour}:30:00+00:00"} for hour in range(4)]
    assert [len(segment) for segment in split_samples_at_boundaries(samples, boundaries)] == [2, 2]


def test_archived_revision_does_not_create_a_boundary():
    boundaries = project_maintenance_boundaries([{
        "maintenance_event_id": "MEV-1", "occurred_at": "2026-08-01T01:00:00+00:00",
        "category": "module_removed", "archived_at": "2026-08-02T00:00:00+00:00",
    }])
    assert boundaries == []


def test_repair_and_manual_balancing_are_context_without_causality_claim():
    boundaries = project_maintenance_boundaries([
        {"maintenance_event_id": "MEV-1", "occurred_at": "2026-08-01T01:00:00+00:00",
         "category": "repair", "archived_at": None},
        {"maintenance_event_id": "MEV-2", "occurred_at": "2026-08-01T02:00:00+00:00",
         "category": "manual_balancing", "archived_at": None},
    ])
    assert all(item["diagnostic_effect"] == "maintenance_context" for item in boundaries)
    assert all(item["evidence_level"] == "direct_evidence" for item in boundaries)
    assert all(item["cause_confirmed"] is False for item in boundaries)
