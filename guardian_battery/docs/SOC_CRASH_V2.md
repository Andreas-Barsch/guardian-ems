# Guardian SOC Crash v2

`guardian_soc_crash_v1` remains a legacy compatibility detector and is unchanged.
Its thresholds have no documented fachliche requirement in this repository.

`guardian_soc_crash_v2` deterministically evaluates one explicitly bounded window
for one `physical_serial`. It means a sufficiently observed, temporally localized
negative SOC discontinuity whose magnitude cannot be sufficiently explained by
the electrical charge removed from that physical module in the interval. The
classification never asserts a cause.

Only observed timestamp, physical serial, SOC, module current, and the historical
identity epoch are inputs. Guardian's established sign convention is positive
current for charge and negative current for discharge. Removed charge is integrated
as the trapezoidal integral of `max(0, -current_a)` over consecutive samples and
reported in Ah as `trapezoidal_discharge_current_v1`.

The initial `guardian_soc_crash_v2_initial_calibration_v1` values are calibration
starting points, not Pylontech specifications or validated scientific limits:

- maximum event window: 300 s
- maximum adjacent sample gap: 120 s
- minimum samples: 3
- minimum observed SOC drop: 5.0 percentage points
- minimum unexplained SOC drop: 3.0 percentage points
- minimum unexplained fraction: 0.50
- minimum discharge current: 0.2 A magnitude
- reference capacity: no implicit default; it must be supplied explicitly in
  Guardian's `reference_capacity_ah` option (Ah) or as a request-local override

Results distinguish OBSERVED inputs from DERIVED charge/SOC calculations and use
`NORMAL`, `SOC_DISCONTINUITY`, `SOC_CRASH`, or `INSUFFICIENT_EVIDENCE`. Missing
values are never converted to zero. Request-local numerical policy overrides are
reported in the result and never mutate or persist the production policy.

The Research endpoint is `GET /api/research/events/soc-crashes-v2`. `from..to` is
the bounded search window; 300 seconds is the maximum duration of each local
candidate, not a maximum search duration. Observations are split on missing required
evidence, identity-epoch changes, and sample gaps over 120 seconds. Within each
segment the earliest unused sample starts a candidate. The candidate extends until
the SOC drop first reaches the configured threshold or 300 seconds would be
exceeded. A threshold-reaching candidate is emitted and consumes its samples, so
events never overlap and no sample belongs to two emitted events. An unsuccessful
start advances by one sample. NORMAL intervals are not emitted and no v1 merge rule
is applied.

The response separates bounded search metadata from an ordered `events` collection.
Its Cell History reader requires a valid block-range index, exposes range/byte/record
counts, and has no full-history/full-day fallback. Consequently the implementation
follows GRA-001 (no acquisition lock or write), GRA-002 (physically bounded reads),
and GRA-003 (cost follows the requested search window rather than system age).

`reference_capacity_ah` is stored as an optional string because an empty value means
explicitly unconfigured; a non-empty value must be finite and greater than zero Ah.
The result identifies its provenance as `production_configuration` or
`request_override`. Overrides are immutable request copies and are never persisted.
There is no MCP tool for v2 in this change.
