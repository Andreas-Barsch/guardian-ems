"""Deterministic, physics-oriented SOC discontinuity evaluation v2."""
from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime


DETECTOR_VERSION = "guardian_soc_crash_v2"
POLICY_VERSION = "guardian_soc_crash_v2_initial_calibration_v1"
INTEGRATION_METHOD = "trapezoidal_discharge_current_v1"


@dataclass(frozen=True)
class SocCrashV2Policy:
    """Versioned calibration policy; field names carry their documented units."""

    max_event_window_s: float = 300.0
    max_sample_gap_s: float = 120.0
    min_samples: int = 3
    min_observed_soc_drop_pp: float = 5.0
    min_unexplained_soc_drop_pp: float = 3.0
    min_unexplained_fraction: float = 0.50
    min_discharge_current_a: float = 0.2
    reference_capacity_ah: float | None = None
    policy_version: str = POLICY_VERSION

    def validated(self) -> "SocCrashV2Policy":
        numeric = {
            "max_event_window_s": self.max_event_window_s,
            "max_sample_gap_s": self.max_sample_gap_s,
            "min_observed_soc_drop_pp": self.min_observed_soc_drop_pp,
            "min_unexplained_soc_drop_pp": self.min_unexplained_soc_drop_pp,
            "min_unexplained_fraction": self.min_unexplained_fraction,
            "min_discharge_current_a": self.min_discharge_current_a,
        }
        if type(self.min_samples) is not int or self.min_samples < 2:
            raise ValueError("min_samples must be an integer >= 2")
        if any(isinstance(value, bool) or not math.isfinite(float(value))
               for value in numeric.values()):
            raise ValueError("policy values must be finite numbers")
        if self.max_event_window_s <= 0 or self.max_sample_gap_s <= 0:
            raise ValueError("time limits must be positive")
        if self.max_sample_gap_s > self.max_event_window_s:
            raise ValueError("max_sample_gap_s must not exceed max_event_window_s")
        if self.min_observed_soc_drop_pp < 0 or self.min_unexplained_soc_drop_pp < 0:
            raise ValueError("SOC thresholds must be non-negative")
        if not 0 <= self.min_unexplained_fraction <= 1:
            raise ValueError("min_unexplained_fraction must be within 0..1")
        if self.min_discharge_current_a < 0:
            raise ValueError("min_discharge_current_a must be non-negative")
        return self

    def with_overrides(self, **values) -> "SocCrashV2Policy":
        return replace(self, **values).validated()

    def parameters(self) -> dict:
        values = asdict(self)
        capacity = values["reference_capacity_ah"]
        # Keep fail-visible analytical output valid JSON even for a non-finite
        # explicitly supplied capacity.  The quality gate still reports it as
        # invalid and never treats it as a usable capacity.
        if isinstance(capacity, float) and not math.isfinite(capacity):
            values["reference_capacity_ah"] = None
        return values

    def identity(self) -> str:
        encoded = json.dumps(self.parameters(), sort_keys=True,
                             separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _event_id(serial: str, start: str, end: str, policy: SocCrashV2Policy) -> str:
    value = {"v": DETECTOR_VERSION, "s": serial, "f": start, "t": end,
             "p": policy.identity()}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"SCE2-{token}.{digest}"


def evaluate_soc_window(*, physical_serial: str, observations: list[dict],
                        interval_start: str, interval_end: str,
                        policy: SocCrashV2Policy,
                        reference_capacity_provenance: str =
                        "production_configuration") -> dict:
    """Evaluate exactly one caller-bounded interval without expanding evidence."""
    policy = policy.validated()
    reasons: list[str] = []
    try:
        requested_start = datetime.fromisoformat(interval_start).timestamp()
        requested_end = datetime.fromisoformat(interval_end).timestamp()
    except (TypeError, ValueError):
        raise ValueError("interval timestamps must be ISO-8601") from None
    requested_duration = requested_end - requested_start
    if requested_duration < 0:
        raise ValueError("interval start must not exceed interval end")
    if requested_duration > policy.max_event_window_s:
        reasons.append("event_window_exceeded")

    parsed: list[tuple[float, dict]] = []
    missing_soc = missing_current = missing_timestamp = False
    wrong_serial = False
    for row in observations:
        if row.get("physical_serial") != physical_serial:
            wrong_serial = True
        try:
            epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
        except (KeyError, TypeError, ValueError):
            missing_timestamp = True
            continue
        if row.get("soc") is None: missing_soc = True
        if row.get("current") is None: missing_current = True
        parsed.append((epoch, row))
    if wrong_serial: reasons.append("physical_serial_mismatch")
    if missing_timestamp: reasons.append("timestamp_missing_or_invalid")
    if missing_soc: reasons.append("soc_missing_or_invalid")
    if missing_current: reasons.append("current_missing_or_invalid")
    if len(parsed) < policy.min_samples: reasons.append("insufficient_samples")
    epochs = [item[0] for item in parsed]
    if any(right < left for left, right in zip(epochs, epochs[1:])):
        reasons.append("observations_not_chronological")
    gaps = [right - left for left, right in zip(epochs, epochs[1:])]
    maximum_gap = max(gaps) if gaps else None
    if maximum_gap is not None and maximum_gap > policy.max_sample_gap_s:
        reasons.append("sample_gap_exceeded")
    epoch_ids = [row.get("identity_epoch_id") for _, row in parsed]
    if any(value is None for value in epoch_ids):
        reasons.append("identity_epoch_unavailable")
    elif len(set(epoch_ids)) > 1:
        reasons.append("identity_epoch_boundary")
    capacity = policy.reference_capacity_ah
    capacity_valid = not (capacity is None or isinstance(capacity, bool) or
        not isinstance(capacity, (int, float)) or
        not math.isfinite(float(capacity)) or float(capacity) <= 0)
    evidence_complete = not reasons
    if not capacity_valid:
        reasons.append("reference_capacity_unavailable_or_invalid")

    complete = evidence_complete and capacity_valid
    soc_start = soc_end = observed_drop = discharged_ah = None
    expected_drop = unexplained_drop = unexplained_for_classification = None
    unexplained_fraction = None
    has_qualifying_discharge = False
    if evidence_complete:
        soc_values = [float(row["soc"]) for _, row in parsed]
        current_values = [float(row["current"]) for _, row in parsed]
        if any(not math.isfinite(value) for value in (*soc_values, *current_values)):
            reasons.append("required_value_non_finite")
            complete = False
        else:
            soc_start, soc_end = soc_values[0], soc_values[-1]
            observed_drop = soc_start - soc_end
            discharge = [max(0.0, -value) for value in current_values]
            has_qualifying_discharge = any(
                value >= policy.min_discharge_current_a for value in discharge)
            discharged_ah = sum(
                (left + right) * 0.5 * gap / 3600.0
                for left, right, gap in zip(discharge, discharge[1:], gaps))
            if capacity_valid:
                expected_drop = 100.0 * discharged_ah / float(capacity)
                unexplained_drop = observed_drop - expected_drop
                unexplained_for_classification = max(0.0, unexplained_drop)
                unexplained_fraction = (unexplained_for_classification / observed_drop
                                        if observed_drop > 0 else 0.0)

    if not complete:
        classification = "INSUFFICIENT_EVIDENCE"
    elif observed_drop < policy.min_observed_soc_drop_pp:
        classification = "NORMAL"
        reasons.append("observed_soc_drop_below_threshold")
    else:
        if unexplained_for_classification < policy.min_unexplained_soc_drop_pp:
            reasons.append("unexplained_soc_drop_below_threshold")
        if unexplained_fraction < policy.min_unexplained_fraction:
            reasons.append("unexplained_fraction_below_threshold")
        if not has_qualifying_discharge:
            reasons.append("qualifying_discharge_absent")
        classification = "SOC_CRASH" if not reasons else "SOC_DISCONTINUITY"

    result = {
        "detector_version": DETECTOR_VERSION,
        "policy_version": policy.policy_version,
        "policy_id": policy.identity(),
        "effective_policy": policy.parameters(),
        "reference_capacity_provenance": reference_capacity_provenance,
        "physical_serial": physical_serial,
        "interval": {"start": interval_start, "end": interval_end,
                     "duration_seconds": requested_duration},
        "sample_count": len(parsed), "maximum_sample_gap_s": maximum_gap,
        "integration_method": INTEGRATION_METHOD,
        "classification": classification, "reason_codes": reasons,
        "evidence": {
            "OBSERVED": {"soc_start": soc_start, "soc_end": soc_end,
                         "physical_serial": physical_serial,
                         "identity_epoch_ids": list(dict.fromkeys(epoch_ids))},
            "DERIVED": {"observed_soc_drop_pp": observed_drop,
                        "discharged_ah": discharged_ah,
                        "expected_soc_drop_pp": expected_drop,
                        "unexplained_soc_drop_pp": unexplained_drop,
                        "unexplained_soc_drop_for_classification_pp":
                            unexplained_for_classification,
                        "unexplained_fraction": unexplained_fraction,
                        "contains_qualifying_discharge": has_qualifying_discharge},
        },
    }
    if classification in {"SOC_CRASH", "SOC_DISCONTINUITY"}:
        result["event_id"] = _event_id(
            physical_serial, interval_start, interval_end, policy)
    return result


def discover_soc_crash_events(*, physical_serial: str, observations: list[dict],
                              policy: SocCrashV2Policy,
                              reference_capacity_provenance: str =
                              "production_configuration") -> dict:
    """Discover deterministic, non-overlapping local v2 candidates.

    Observations are processed in timestamp order and split whenever required
    evidence is missing, the identity epoch changes, or an adjacent timestamp
    gap exceeds ``max_sample_gap_s``. In each segment the earliest unused sample
    starts a candidate. It extends one sample at a time until the observed SOC
    drop first reaches ``min_observed_soc_drop_pp`` or its duration would exceed
    ``max_event_window_s``. A threshold-reaching candidate is evaluated once
    and emitted, then all of its samples are consumed. Thus one sample cannot
    belong to two emitted events. Starts that do not reach the threshold within
    the event window advance by one sample. NORMAL intervals are not emitted,
    and no v1 merge rule is used.
    """
    policy = policy.validated()
    parsed = []
    invalid_observations = 0
    for row in observations:
        try:
            epoch = datetime.fromisoformat(row["timestamp"]).timestamp()
        except (KeyError, TypeError, ValueError):
            invalid_observations += 1
            continue
        value = dict(row); value["_epoch"] = epoch
        parsed.append(value)
    parsed.sort(key=lambda item: (item["_epoch"], item.get("identity_epoch_id") or ""))

    segments: list[list[dict]] = []
    current: list[dict] = []
    for row in parsed:
        valid = (row.get("physical_serial") == physical_serial
                 and row.get("soc") is not None and row.get("current") is not None
                 and row.get("identity_epoch_id") is not None)
        if not valid:
            invalid_observations += 1
            if current: segments.append(current); current = []
            continue
        if current:
            gap = row["_epoch"] - current[-1]["_epoch"]
            if (gap < 0 or gap > policy.max_sample_gap_s
                    or row["identity_epoch_id"] != current[-1]["identity_epoch_id"]):
                segments.append(current); current = []
        current.append(row)
    if current: segments.append(current)

    events = []
    candidates_evaluated = 0
    for segment in segments:
        start_index = 0
        while start_index + policy.min_samples <= len(segment):
            emitted = False
            for end_index in range(start_index + policy.min_samples - 1, len(segment)):
                duration = segment[end_index]["_epoch"] - segment[start_index]["_epoch"]
                if duration > policy.max_event_window_s: break
                observed_drop = float(segment[start_index]["soc"]) - float(
                    segment[end_index]["soc"])
                if observed_drop < policy.min_observed_soc_drop_pp: continue
                rows = [{key: value for key, value in row.items() if key != "_epoch"}
                        for row in segment[start_index:end_index + 1]]
                result = evaluate_soc_window(physical_serial=physical_serial,
                    observations=rows, interval_start=rows[0]["timestamp"],
                    interval_end=rows[-1]["timestamp"], policy=policy,
                    reference_capacity_provenance=reference_capacity_provenance)
                candidates_evaluated += 1
                if result["classification"] != "NORMAL": events.append(result)
                start_index = end_index + 1
                emitted = True
                break
            if not emitted: start_index += 1

    classifications = {item["classification"] for item in events}
    if "SOC_CRASH" in classifications: classification = "SOC_CRASH"
    elif "INSUFFICIENT_EVIDENCE" in classifications:
        classification = "INSUFFICIENT_EVIDENCE"
    elif "SOC_DISCONTINUITY" in classifications:
        classification = "SOC_DISCONTINUITY"
    elif invalid_observations:
        classification = "INSUFFICIENT_EVIDENCE"
    else: classification = "NORMAL"
    reasons = (["invalid_observations_excluded"] if invalid_observations else [])
    capacity = policy.reference_capacity_ah
    if (capacity is None or isinstance(capacity, bool)
            or not isinstance(capacity, (int, float))
            or not math.isfinite(float(capacity)) or float(capacity) <= 0):
        reasons.append("reference_capacity_unavailable_or_invalid")
        if classification == "NORMAL": classification = "INSUFFICIENT_EVIDENCE"
    return {"classification": classification, "reason_codes": reasons,
            "events": events, "segments": len(segments),
            "candidates_evaluated": candidates_evaluated,
            "invalid_observations": invalid_observations}
