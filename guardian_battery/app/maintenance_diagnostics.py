"""Conservative diagnostic context projected from documented maintenance."""

from __future__ import annotations


SEGMENT_BOUNDARY_CATEGORIES = frozenset({
    "module_removed", "module_added", "module_replacement", "module_position_change",
})
CONTEXT_CATEGORIES = frozenset({
    "repair", "battery_cell_test", "maintenance", "manual_balancing", "balancing",
})


def project_maintenance_boundaries(events) -> list[dict]:
    """Classify documented interventions without inferring an undocumented cause."""

    result = []
    for event in events:
        raw = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        if raw.get("archived_at") is not None or raw.get("status") == "inactive":
            continue
        category = raw.get("category") or raw.get("metadata", {}).get("category")
        if category in SEGMENT_BOUNDARY_CATEGORIES:
            effect = "operating_segment_boundary"
        elif category in CONTEXT_CATEGORIES:
            effect = "maintenance_context"
        else:
            effect = "documented_event"
        result.append({
            "maintenance_event_id": raw.get("maintenance_event_id"),
            "occurred_at": raw.get("occurred_at") or raw.get("timestamp"),
            "category": category,
            "module_number": raw.get("module_number"),
            "module_serial": raw.get("resolved_module_serial") or raw.get("module_serial"),
            "cell_number": raw.get("cell_number"),
            "diagnostic_effect": effect,
            "evidence_level": "direct_evidence",
            "cause_confirmed": False,
        })
    return sorted(result, key=lambda item: (item["occurred_at"] or "",
                                             item["maintenance_event_id"] or ""))


def split_samples_at_boundaries(samples, boundaries) -> list[list[dict]]:
    """Split continuous analysis windows while retaining every raw sample."""

    timestamps = sorted(item["occurred_at"] for item in boundaries
                        if item["diagnostic_effect"] == "operating_segment_boundary"
                        and item.get("occurred_at"))
    segments = [[]]
    boundary_index = 0
    for sample in sorted(samples, key=lambda item: str(item["timestamp"])):
        timestamp = str(sample["timestamp"])
        while boundary_index < len(timestamps) and timestamp >= timestamps[boundary_index]:
            if segments[-1]:
                segments.append([])
            boundary_index += 1
        segments[-1].append(sample)
    return [segment for segment in segments if segment]
