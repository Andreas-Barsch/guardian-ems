# SOC Crash Core Evidence

The SOC Crash Core Evidence package provides small, bounded and reproducible
evidence around a detected SOC crash so that an external research/AI layer can
decide which additional evidence to request. It does not determine causality.

## Contract

`GET /api/research/evidence-core?event_id=...` accepts only the stable event ID
and the optional diagnostic `profile=true|false` flag. Its semantics identifier
is `research_soc_crash_core_evidence_v1`.

- Target evidence is fixed to `event_start - PT10M` through
  `event_end + PT30M`.
- Historical peers are resolved from topology at the event timestamp and read
  together from `event_start - PT5M` through `event_end + PT5M`.
- The endpoint performs at most one target Cell History scan, one shared peer
  Cell History scan, and one bounded RS485 scan.
- The hard monotonic deadline is 10 seconds. The response hard limit remains
  2 MiB; the normal operating target is below 500 KiB and five seconds.
- No caller-controlled window can expand this contract.

Target records retain observed SOC, current, module voltage, all stored cell
voltages, stored temperature channels, and balancing evidence when present.
Power, minimum/maximum/median cell voltage, spread, lowest/highest cell, and
per-cell median deviations are deterministic `DERIVED` evidence. Peer records
are intentionally smaller and contain only immediate comparison evidence.

## Evidence, identity, and coverage

Only `OBSERVED` and `DERIVED` evidence classes are emitted. `INFERRED` evidence
and causal conclusions are forbidden. Physical serial is primary identity;
position, position-history ID, and identity epoch are resolved at event time.
Missing optional sources remain `unavailable`, never zero. In particular SOC
recalibration stays `OBSERVED / unavailable / []`; no SOC-jump heuristic exists.

Coverage is calculated from records already read for the core request and
states requested and observed intervals, sample counts, boundaries, gaps, and
complete/partial/unavailable quality. Provenance includes the event, detector,
fixed intervals, sources, evidence classes, configuration revision when
available, creation time, and a deterministic secret-free source fingerprint.

## Drill-down and Extended Evidence v2

Core is additive. `research_soc_crash_evidence_v2` remains available unchanged
for its extended package. After Core, a client can use the existing read-only
module/timeseries, cell-history, alarm, low-voltage, maintenance, canonical
phase, daily-diagnostics, coverage, and SOC-crash tools. Core does not add a
broad drill-down API.

Peer DCL/CCL and charge/discharge-enable history remain an explicit open gap:
they live in the separate RS485-management path and currently have no matching
public peer-history contract.
