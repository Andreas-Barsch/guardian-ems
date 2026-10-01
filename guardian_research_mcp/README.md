# Guardian Research MCP

Version **0.8.3**, locally prepared and **unpublished**, is a provider-neutral,
read-only Streamable HTTP MCP adapter for Guardian Battery's Research API.
The coordinated target is **Battery 0.8.4 / Research-MCP 0.8.3**. Tunnel remains
0.8.1 and Diagnostic Engine 0.4.12. No installation or production acceptance is
claimed. This version/documentation preparation does not change the reviewed
SOC/Research behavior of local checkpoint `3b3f3a4`.

## Contract

The catalogue contains **15 read-only tools**. `list_soc_crash_events` accepts
MCP arguments `timestamp_from` and `timestamp_to` (at most 31 days), forwarded as
Battery API `from` and `to`; `max_records` is 1..500, default 100.
`get_soc_crash_event` accepts `event_id`. Both perform exactly one Battery GET,
without automatic follow-up, event creation, replay, evidence packages or causality.

The earlier 0.8.2 transition removed `find_soc_crashes`, `build_evidence_package`,
`query_raw_evidence` and `build_soc_crash_core_evidence` from the prior 17-tool
GitHub source and added the two persisted-event tools. These removals remain in
force; no raw/core-Evidence replacement or call/result equivalence is implied.
Refresh cached client metadata and inspect the actual names and parameter enums;
do not infer a current catalogue from the version string alone.

The retired Battery API routes `/api/research/events/soc-crashes`,
`/api/research/evidence-package` and `/api/research/evidence-core` remain 404 on
the coordinated Battery line. Current event routes remain
`/api/research/soc-crash-events` and `/api/research/soc-crash-events/{event_id}`.

### Mixed event versions

- Historical schema **1**, detector `guardian_soc_crash_simple_v1`: original
  `observed_drop_pp`, singular `reference_module`, bytes, IDs and payloads keep
  their eigen-drop/previous-role meaning. Stored equality events (drop exactly X)
  remain readable; legacy creation/append still requires >X. A null old reference
  means no uniquely determined previous weakest module at the initial tie.
- New schema **2**, detector `guardian_soc_gap_v1`: `observed_gap_pp`,
  `second_lowest_soc_at_confirmation`, all sorted `reference_modules_at_confirmation`
  and aligned nullable `reference_serials_at_confirmation`. Distinct
  `previous_weakest_module`/serial are start metadata, nullable after a tie phase,
  not the changing comparison reference. Start SOC remains metadata, not an
  extra own-drop condition.
- Mixed lists use `semantics_version=guardian_soc_events_v2`; single-event
  envelopes use the stored event's detector version. The generic Research
  envelope schema remains 1; it is distinct from each event's schema version.
  Both event formats pass through unchanged. Clients must interpret them by
  detector/schema, never rename eigen-drop into gap or silently combine counts.

### Battery rule used by the coordinated release

Configurable defaults: `soc_crash_gap_pp=5.0` percentage points and
`soc_crash_window_seconds=300`. Confirm at the first comparable measurement with
**current second-minimum SOC minus current candidate SOC >= X**, within
**elapsed measurement time <= Y** of the fixed candidate start. The start frame
may confirm immediately. No further own SOC decline or median condition applies.

There is one candidate. Process role changes before confirmation. A unique initial
minimum only seeds the role; initial/new joint minima wait for a unique minimum,
without choosing by module number. Ties including the incumbent and changes of
second-minimum reference do not restart the clock. All tied second minima are
reported. Fewer than two observations suspend comparison without extending Y;
normal role handling resumes on return. No re-arm after confirmation/expiry
without a role change. With at least two observations and an absent incumbent,
the existing baseline path remains. Missing observations do not prove removal.

Legacy `soc_crash_drop_pp` is accepted only as an optional obsolete option and
ignored with a startup warning. It is not converted into a gap value; if the new
key is absent, its confirmed default 5 applies. Custom old values require review.
No new battery/RS485 query, alarm, UI or diagnosis is part of this contract.

- Endpoint: `/mcp` (stateless Streamable HTTP)
- Operations: exactly the documented read-only Guardian evidence tools
- Health: `GET /health`, containing operational state only
- Evidence: Research envelopes are forwarded without interpretation
- Identity: `physical_serial` is primary; position is resolved by Guardian at time
- Access: bearer authentication plus explicit Host and Origin allowlists
- Exposure: port 8098 is not published by default
- Storage: the add-on has no Guardian `/share` or `/config` mount

Set the same non-empty secret in Guardian Battery's
`guardian_research_api_token` option and this add-on's `guardian_api_token`
option. Set a separate non-empty `mcp_auth_token` for MCP clients. Empty tokens
do not enable unauthenticated production access. `development_auth_mode` is
only for isolated tests and must remain false in production.

`guardian_base_url` is mandatory because Home Assistant custom-repository DNS
names depend on the installed repository identifier. Configure the Guardian
Battery internal DNS name shown by Supervisor, replace underscores with
hyphens, append port 8099 and `/api/research`. The URL must not contain
credentials.

The MCP layer never reads Guardian evidence files, writes MQTT/RS485/Hycube,
calls Home Assistant services, rebuilds projections, or stores conversations.

For the local Home Assistant application network, the safe default Host
allowlist is `guardian_research_mcp,3195b09a-guardian-research-mcp,localhost,127.0.0.1`.
The repository-qualified DNS name is the Host used by Guardian MCP Tunnel's
default internal URL. Keep the explicit entries and do not replace them with a
wildcard. An HTTP 421 `Misdirected Request` from the tunnel preflight indicates
that the configured internal MCP hostname is missing from this allowlist.

## Coverage and errors

Matching domain errors appear in MCP text and `structuredContent.error` with
`isError=true` and explicit `origin`. Backend `timeout` remains `timeout`, not
`INVALID_ARGUMENT`. Input schemas enumerate supported metrics, sources,
resolutions and datasets. `query_cell_history` retains all eight metrics:
`soc`, `module_voltage`, `module_current`, `module_temperature`, `cell_voltage`,
`cell_temperature`, `cell_deviation`, `cell_spread`. Units are %, V, A, degC and mV
as appropriate. Invalid source/metric combinations remain Battery errors.

Cell-history results retain original `pwr_sample_at`/`cell_sample_at` metadata.
Coverage describes finite observations across the requested window independently
of pagination/downsampling, exposes leading/trailing/internal gaps and does not
extrapolate. Policy uses validity intervals; maintenance-event counts do not
establish continuous coverage (`unknown`). The descriptive coverage cadence is
not a SOC recognition criterion. No new MCP replay/write tool or timeout increase.

## Separate release paths and validation

Battery remains the existing local add-on `local_guardian_battery`, packaged from
a clean fixed Battery release commit with its source marker. Research-MCP remains
the repository add-on `3195b09a_guardian_research_mcp`. Only a narrowly scoped MCP
change belongs on a separately rechecked GitHub main; do not merge the entire
local Battery branch or replace main's other Battery implementation. Preserve
existing secrets, authentication and Host/Origin restrictions. Tunnel is unchanged.

The paired `tests/test_research_contract.py` requires the matching Battery gap/
replay/API source. It proves the local Battery/MCP combination, not compatibility
with the different Battery source on GitHub main. Keep that paired proof separate
from isolated MCP release tests on the eventual MCP publication branch; do not
blindly copy its Battery-dependent imports there. Check the exact released MCP
source against the tested paired source. This README is self-contained for that
MCP-only publication and does not require unavailable Battery documentation links.

Before a later coordinated installation, secure a current, verified return set
of actual Battery/MCP images and options plus complete Guardian `/share` data.
Source code alone is not an installed image. Old Battery readers skip schema-2
records with a warning; preserve their bytes and do not claim they remain
retrievable through old software. Retain matching old configuration and never
blindly restore an old data snapshot over newly collected data. Restore remains
unverified until separately tested/authorized.

After separately authorized installation, verify Battery startup version/source,
MCP version/build evidence and status, refresh the client catalogue, then inspect
a small fixed existing-data window and coverage under normal collector load.
No extra PWR/RS485 poll. `guardian_status` alone does not prove product versions,
fresh sampling or schema-2 event output. Empty event lists do not justify creating
a production test event. No automatic historical filling on startup.

The 70 historical gap results are provisional, separate from the old 23-run.
Any future import needs explicit approval, fixed source hashes/period, identical
detector/parameters and an immutable provenance record. Different old/new IDs do
not deduplicate semantic overlap; analyses must distinguish detector versions.
The current inventory mode cannot write events and no productive import is enabled.

## Historical release context

Battery 0.8.1 identifies the other development line. Phase 1 prepared Battery/MCP
0.8.2 and replaced the legacy SOC/Evidence tools as described above. The subsequent
local maintenance base used Battery 0.8.3 (`2d026dd`) with MCP 0.8.2; the gap-rule
implementation was secured as `3b3f3a4` without a component version bump.
Those facts are historical checkpoints, not acceptance of the new 0.8.4/0.8.3 pair.
