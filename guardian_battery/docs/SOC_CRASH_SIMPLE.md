# SOC events — Battery 0.8.5 / MCP 0.8.3 release preparation

Battery 0.8.5 only changes Research reader I/O. All confirmed SOC gap rules,
configuration gap=5/window=300, schema versions, IDs and historical read
compatibility remain unchanged. The 70 historical events and their separate
provenance were imported under a later explicit approval; do not import again.
They remain historical derived observations, not newly detected live crashes.
MCP stays 0.8.3, Tunnel 0.8.1 and Diagnostic Engine 0.4.12. The following
0.8.4 preparation is retained as historical documentation, not current status.

## Historical 0.8.4 preparation

# SOC crash events — Battery 0.8.4 / MCP 0.8.3, prepared and unpublished

## Current version preparation (2026-10-01)

The confirmed target pair is **Battery 0.8.4 / Research-MCP 0.8.3**, with Tunnel
0.8.1 and Diagnostic Engine 0.4.12 unchanged. This prepares versions, documentation
and directly affected tests only. The gap/event/coverage code is unchanged from
reviewed local commit `3b3f3a4`; no installation or production acceptance is claimed.
Battery uses the existing local package path; only MCP is later integrated narrowly
on a separately checked GitHub main. Do not overwrite its other Battery line.
The final packaging source SHA can only be assigned after a later release commit.

## Historical phase-1 release preparation

Base: Battery 0.8.0 (`53367d2`) plus the accepted tunnel chain through
`c41956b`. Prepared, unpublished release: Battery 0.8.2 and Research-MCP 0.8.2;
Tunnel remains 0.8.1 and Diagnostic Engine remains 0.4.12.
Battery 0.8.1 already identifies the other development line, so this release
uses 0.8.2. This version preparation does not change the phase-1 behavior.
This change has no deployment or production acceptance.

## Historical gap implementation checkpoint (before version preparation)

The release paragraph above records the earlier phase-1 preparation. The current
worktree is based on `2d026dd` (Battery 0.8.3, Research-MCP 0.8.2, Tunnel 0.8.1,
Diagnostic Engine 0.4.12). This local change does not bump component versions,
install anything or certify production. It replaces the eigen-drop recognition
rule with the explicitly confirmed **current gap** rule; historical events keep
their old meaning.

## Definition and state

Inputs are only module number, SOC, the existing PWR sample timestamp and
optional documented serial. There are no new battery queries, median conditions
or additional diagnostic recognition criteria. Comparison requires at least two
currently observed modules; no fixed six-module or identity requirement exists.

An initially unique minimum seeds the weakest role without starting a candidate.
An initial tie selects no module. Once a unique minimum emerges, its candidate
starts then, with no previous uniquely weakest module. Module number never breaks
a minimum tie. When two other modules jointly become the new minimum, the old
candidate ends and the detector waits for a unique minimum before starting the
new candidate and its clock. A later tie including the incumbent alone retains
its role and candidate start.

A strict role change to one new weakest module starts one candidate at that
measurement time. Start SOC and time are fixed metadata. At every comparable
frame, use the lowest SOC of **all other currently observed modules** as the
current second minimum. Confirm at the first measurement where:

`second_lowest_soc_now - candidate_soc_now >= soc_crash_gap_pp`

and `current_time - candidate_start_time <= soc_crash_window_seconds`.
Exactly X and exactly Y are included. Confirmation can occur on the candidate's
first frame (elapsed time zero). No further decline from its own start SOC is
required; that historical eigen-drop is no longer the criterion. A rising
reference SOC can also meet the gap; this establishes no cause.

Process role changes before threshold evaluation: an old candidate cannot confirm
on its replacement frame. A change of the second-minimum reference does not reset
the start or deadline. At a tied second minimum, document every tied reference;
do not select one arbitrarily. A candidate emits at most once. Confirmation or
expiry does not re-arm the same weakest role; a subsequent role change is needed.

Fewer than two observations suspend comparison, preserve the role/start, and do
not extend the original deadline. On return, apply normal role changes first.
With at least two observations, the existing missing-incumbent path still seeds
a new baseline without claiming a crossing. Missing observations do not prove
physical removal. Duplicate/older frames are ignored; invalid/non-finite samples
are rejected. No interpolation or end-of-window waiting.

Confirmed configurable defaults:
- `soc_crash_gap_pp`: 5.0 percentage points, schema 0.01..100.
- `soc_crash_window_seconds`: 300 seconds, integer 1..86400.

Legacy `soc_crash_drop_pp` remains an optional accepted configuration field only.
It is not used or silently converted into a gap threshold. Its presence emits a
startup warning. If the new gap field is absent, the explicitly confirmed new
5.0-pp default applies; the configured window remains effective. Existing custom
drop values therefore require deliberate review before a later update. Options
load at startup. There is no new custom UI, MQTT projection, alarm or notification.

The synchronous hook follows existing PWR parsing and uses `pwr_sample_at`.
Identity comes only from the documentary resolver already loaded by the existing
cell round; unknown serials remain null. Candidate and previous-role identity are
frozen at start. Second-minimum identities are those documented at confirmation.
Identity is metadata, never an event-recognition condition or extra query.

## Event persistence

Runtime destination: `/share/guardian_battery/soc_crash_events.jsonl`.
All tests inject temporary paths. Import/construction/GET create no files.

### New gap events

Schema **2**, detector **`guardian_soc_gap_v1`** contains:
`event_id`, `schema_version`, `detector_version`, `observation_started_at`,
`confirmed_at`, `candidate_module`, `candidate_physical_serial`,
`previous_weakest_module`, `previous_weakest_physical_serial`,
`candidate_soc_at_start`, `candidate_soc_at_confirmation`,
`reference_modules_at_confirmation`, `reference_serials_at_confirmation`,
`second_lowest_soc_at_confirmation`, `observed_gap_pp`, `x_pp`, `y_seconds`,
`causality`. Causality is always `not_determined`.

`previous_weakest_module` is documentary start metadata and may be null after a
minimum-tie phase. It is not the changing comparison reference. Reference arrays
contain all tied current second-minimum modules, sorted by position, with aligned
nullable serials. New events have neither `observed_drop_pp` nor the old singular
`reference_module`. Creation, append and read validate gap >= X and elapsed <= Y,
as well as module, numeric, identity, causality and ID integrity.

Gap IDs hash detector version, start timestamp, candidate module, start SOC,
x and y. Moving reference or identity metadata cannot rename one candidate
episode. A differing immutable payload for an existing ID is rejected, not
rewritten. Repeating the same live/replay input produces identical IDs/payloads.

### Historical eigen-drop events (unchanged)

Schema **1**, detector **`guardian_soc_crash_simple_v1`** retains exactly its old
fields, including `reference_module`, `reference_physical_serial` and
`observed_drop_pp`. These mean previous weakest role and further drop from the
fixed candidate start, not a current second-minimum gap. Its original ID algorithm
(including the old reference module) remains unchanged. Null references retain
their historical meaning: no unique previous weakest module at initial tie.

Stored schema-1 records retain the historical `drop >= X` read threshold, with
all other validation and ID checks intact. Legacy creation/append still require
`drop > X`; current live/replay detection creates only schema 2. Equality records
remain readable over API/MCP with original bytes, IDs and payloads. No migration,
rewrite, relabeling or claim that old records meet the new gap rule takes place.
The store can contain both schemas. No measurement arrays or evidence packages
are persisted.

A separate `.lock` protects cross-process read/check/append. Writes append,
flush and fsync. Complete invalid lines are preserved, warned about and
skipped. Reads ignore an unterminated tail. Append terminates that tail
without rewriting bytes, recovering a complete JSON event with missing
newline before deduplication. Oversized records (over 4096 bytes), source
replacement/truncation and conflicting valid revisions fail closed.

An in-memory ID index consumes newly appended event-log lines; list queries
sort these small persisted events. No measurement history is scanned.

Store errors do not end acquisition. Up to 64 confirmed events remain in a
process-local retry buffer; overflow is explicitly logged. Pending
observations and retries do not survive process restart. A process crash
before durable append can lose an unpersisted event; there is no automatic historical
replay. Persisted events remain idempotent across restarts. Permanent storage
errors require operator repair.

## Read-only API and MCP

- `GET /api/research/soc-crash-events`: required timezone-aware `from`/`to`,
  inclusive filter on confirmation time, maximum 31 days. `max_records`
  defaults to 100, range 1..500. Ascending confirmation time/event-ID order.
  Excess events set `truncated=true`; phase 1 has no cursor. Narrow the window
  explicitly when necessary.
- `GET /api/research/soc-crash-events/{event_id}`: one persisted event;
  unknown ID is 404. No additional query parameters.
- List `semantics_version` is `guardian_soc_events_v2` for the mixed collection;
  every event retains its own schema/detector version. Single-event envelopes
  use that stored event's detector version. Clients must distinguish the two
  contracts; the new gap is not a renamed eigen-drop field.
- Existing Research envelopes, authentication, GET-only access and 2 MiB
  response limit remain. Event requests do not load identity or raw history.
- MCP: `list_soc_crash_events`, `get_soc_crash_event`. Each performs one GET,
  forwarding the result without interpretation or automatic follow-up.

Old MCP registrations `find_soc_crashes` and `build_evidence_package` are
removed. Legacy API routes `events/soc-crashes`, `evidence-package` and
`evidence-core` return 404 before history access. Historical private functions
remain unregistered. Tunnel code and security boundaries are unchanged.
The phase-1 Battery/MCP 0.8.2 transition introduced the replacement tools;
old and new calls/results are not equivalent. The current Battery 0.8.4 / MCP
0.8.3 update additionally requires schema-aware event handling and refreshed
client parameter/error schemas. MCP list arguments are `timestamp_from` and
`timestamp_to`, mapped to API `from`/`to`. The historical GitHub tool transition
also removed `query_raw_evidence` and `build_soc_crash_core_evidence`, without
raw/core-Evidence replacements. The current catalogue remains 15 read-only tools.

Raw Cell/RS485 APIs, packages, automatic AI access, UI, MQTT, alarms,
notifications and deployment are outside phase 1.

## Separater lokaler historischer Probelauf (noch nicht installiert)

Das optionale [Offline-Werkzeug und seine Nachweisgrenzen](RESEARCH_COVERAGE_REPLAY.md)
verwenden denselben Detektor auf gemeinsam gespeicherten PWR-Beobachtungen
von mindestens zwei Modulen. Die historische Vollständigkeit bleibt unbekannt,
die Ergebnisse sind vorläufig. Standard ist Dry-run. Gespeicherte Stichproben sind keine
vollständige Live-Folge. Keine automatische Nachbefüllung und keine Änderung historischer Ereignisse.
Live und Replay verwenden gemeinsam die oben bestätigte Abstandsregel.


## Update and return boundary

Before installation, verify a current backup of the actually installed Battery
and MCP images, options and complete `/share` data. Keep the new gap key explicit;
legacy drop values are not a migration mapping. Coordinate Battery/MCP and client
updates; preserve the existing add-on identities and authentication. Existing
source archives alone do not establish image availability or tested restoration.

Compatibility is forward-reading: the new reader accepts old/new records. An old
Battery reader accepts schema 1 only and skips schema 2 with a warning. A software
rollback therefore preserves bytes but cannot expose new gap events through the
old API. Keep a protected, unchanged copy of all data collected since the update;
do not silently replace it with an older snapshot. Old configuration must accompany
the old software. This preparation authorizes neither restoration nor installation.

Historical replay stays separate: fixed input hashes, measurement period, detector
and parameters must accompany any later explicitly approved import. The current
70-run is provisional and not a production event store. New IDs distinguish
contracts and deduplicate identical new candidate episodes; they do not eliminate
semantic overlap with old events. Analyse by detector version, never sum both
formats as distinct crashes without a separate comparison. No automatic SOC
historical filling at startup or productive import is enabled.
