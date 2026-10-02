# Guardian Research MCP Changelog

## 0.8.4 – Coordinated Research time budget (locally prepared, unpublished)

- Coordinates with Battery 0.8.6: HTTP default and maximum 75 s for Battery's
  cooperative 60-s processing budget. Queue remains independently 15 s, with
  unchanged capacity, concurrency and full-resolution limits; no retries.
- Existing smaller environment/options overrides remain valid and must be checked
  before installation. Cancellation closes the MCP transport, not Battery's
  synchronous reader immediately. The outer Connector/Tunnel deadline is unknown.
- Manifest, server/health/initialize and User-Agent identify 0.8.4. Tool names,
  argument/result contracts, error forwarding and mixed historical event semantics
  remain unchanged. Tunnel 0.8.1 and Diagnostic Engine 0.4.12 stay unchanged.
- MCP-only delivery against rechecked GitHub main; no Battery branch merge.
  Current Battery 0.8.5/MCP 0.8.3 images/options and current data must be available
  for rollback before a separately approved installation. No publication or live
  acceptance is claimed.

## 0.8.3 – Versioned SOC gap events and Research contract (locally prepared, unpublished)

- Coordinates with Battery 0.8.4. Manifest, server/health/initialize version and
  User-Agent identify 0.8.3; this preparation changes no functional code from the
  reviewed local checkpoint `3b3f3a4`. No publication or operational acceptance.
- Forwards mixed stored events without reinterpretation: historical schema 1 /
  `guardian_soc_crash_simple_v1` keeps eigen-drop fields and equality-read
  compatibility; schema 2 / `guardian_soc_gap_v1` uses the current gap and all
  tied current second-minimum references. Original old bytes/IDs remain unchanged.
  Lists identify `guardian_soc_events_v2`; details retain the event's detector.
- Battery's configurable defaults are gap X=5 pp and Y=300 s: current gap >= X
  within <= Y of the fixed candidate start, immediately including the start frame.
  One candidate, role change first, unique initial minimum only seeds the role,
  new tied minima wait for uniqueness, incumbent ties/reference changes do not
  reset the clock, fewer than two values suspend comparison without extending Y,
  no re-arm without a role change. No additional own-drop/median criterion.
  `soc_crash_gap_pp` is explicit; legacy `soc_crash_drop_pp` is ignored with a warning.
- Preserves structured domain errors in text and structuredContent (`isError=true`,
  explicit origin), all eight Cell-History metrics, units, sample-time metadata
  and coverage bounds independent of pagination. No timeout increase.
- The existing 15 tool names and event endpoints remain. Refresh cached schemas;
  adapt clients to the versioned event fields and structured errors. Previously
  removed SOC/raw/core-Evidence tools remain absent and have no drop-in replacement.
- Release scope is MCP only on a separately rechecked GitHub main; do not replace
  its other Battery development line. Paired Battery/MCP integration tests require
  the matching local Battery source and are distinct from isolated MCP release tests.
- A current image/configuration/data rollback record is required before installation.
  Old Battery readers skip schema 2; preserve bytes and do not promise old readers
  can retrieve new events. Historical replay results require separate explicit
  import approval; different schema IDs do not prevent semantic double counting.
  Tunnel 0.8.1 and Diagnostic Engine 0.4.12 remain unchanged.


## 0.8.2 – Persisted SOC crash event access (prepared, unpublished)

- Removes `find_soc_crashes`, `build_evidence_package`, `query_raw_evidence` and `build_soc_crash_core_evidence` from the previous GitHub main source. Adds `list_soc_crash_events` and `get_soc_crash_event`, forwarding compact persisted Battery events without interpretation; these are not substitutes for raw-evidence or core-evidence retrieval. The target catalogue contains 15 read-only tools, compared with 17 in that source. Refresh cached client tool metadata after the coordinated update and verify the new catalogue.
- The list tool accepts MCP arguments `timestamp_from` and `timestamp_to`, forwarded as Battery API parameters `from` and `to`; `max_records` remains bounded to 1..500 (default 100).
- Requires a coordinated Battery 0.8.2 / MCP 0.8.2 update and client adaptation: old and new calls and results are not equivalent. Battery's legacy `/api/research/events/soc-crashes`, `/api/research/evidence-package` and `/api/research/evidence-core` return 404; replacements use `/api/research/soc-crash-events` and its event-ID route.
- Battery uses configurable defaults X = 5 percentage points, Y = 300 seconds: fixed candidate start (prior drop excluded), immediate first-measurement recognition at drop > X within <= Y, one candidate cancelled before evaluation on role change. Initial ties select no module; the subsequent strict minimum has a null reference. Later ties alone start no candidate. No median or additional recognition criteria.
- Historical equality events remain readable with unchanged IDs and payloads and are not reclassified; new creation and append require > X. Null references pass through unchanged. Event format and detector identifier remain unchanged.
- Battery 0.8.1 already identifies another development line; the coordinated pair uses 0.8.2. Tunnel remains 0.8.1 and Diagnostic Engine remains 0.4.12. This version preparation changes no phase-1 behavior and claims no publication or production acceptance.

## 0.8.1 – Home Assistant Tunnel Host Compatibility

- Adds the repository-qualified internal Home Assistant DNS name
  `3195b09a-guardian-research-mcp` to the explicit safe Host allowlist default.
- Retains Bearer authentication, DNS-rebinding protection, all existing Host
  entries, the wildcard prohibition, read-only tools and private port default.

## 0.8.0 – Read-only Guardian Research Transport

- Adds a separate provider-neutral MCP 2.2.0 add-on using stateless Streamable HTTP at `/mcp`.
- Exposes exactly 15 read-only Research tools through `GuardianResearchClient`; Guardian Research envelopes, provenance, evidence classes, coverage and cursors pass through without interpretation.
- Adds separate Bearer authentication for MCP clients, explicit Host/Origin allowlists, bounded concurrency and responses, cancellation, metadata-only audit and operational `/health`.
- Keeps running while Guardian is unavailable and recovers without restart. It does not read Guardian files or write MQTT, RS485, Hycube, maintenance, configuration or Home Assistant state.
- Publishes no host port by default and mounts neither `/share` nor `/config`.
