# Guardian Research MCP Changelog

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
