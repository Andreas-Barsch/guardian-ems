# SOC Crash Evidence Builder

The SOC Crash Evidence Builder provides reproducible evidence around a detected SOC crash. It does not determine causality.

The existing read-only `GET /api/research/evidence-package` endpoint accepts a
deterministic `event_id` returned by `GET /api/research/events/soc-crashes`.
Its default bounded range is 24 hours before the event and 30 minutes after it;
callers may supply supported ISO-8601 durations explicitly.

The versioned `research_soc_crash_evidence_v2` response contains the unchanged
detector result and thresholds, identity and position valid at event time,
module and all available cell channels, explicitly derived cell statistics,
canonical phase, alarms, low-voltage and BMS-management evidence, maintenance,
Hycube policy/capacity and daily diagnostics where those stores are available.
It also contains structured comparison windows and evidence from modules that
were peers in the historical stack at the event time.

Only `OBSERVED` and `DERIVED` evidence classes are emitted. Missing sources are
reported as unavailable or partial and are never represented as zero. Recorded
temperature channels retain their source semantics; the builder does not
invent per-cell temperatures. Historical position is never replaced with a
module's current position.

The package is bounded by the Research API query limits, source date ranges,
record caps and response-size gate. Immediate pre-crash and crash records retain
the highest available stored resolution. Wider comparison windows are summaries;
the underlying bounded time-series sections expose their selected resolution,
coverage, truncation state and cursor contract. The input fingerprint is stable
for identical source inputs; `package_created_at` records when a response was
assembled and is intentionally not part of that fingerprint.
