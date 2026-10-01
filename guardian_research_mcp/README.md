# Guardian Research MCP

Prepared, unpublished version `0.8.2` is a separate, provider-neutral, read-only
Streamable HTTP MCP adapter for Guardian Battery's Research API.

Battery 0.8.2 and Research-MCP 0.8.2 require a coordinated update and client
adaptation. Battery 0.8.1 already identifies the other development line; 0.8.2
distinguishes this release. Tunnel stays 0.8.1 and Diagnostic Engine stays
0.4.12. No publication or production acceptance is claimed.

## Contract

This prepared release removes the legacy `find_soc_crashes`,
`build_evidence_package`, `query_raw_evidence` and
`build_soc_crash_core_evidence` tools present in the previous GitHub main
source. The new `list_soc_crash_events` and `get_soc_crash_event` tools read
persisted events only and forward exactly one Battery GET response; they do
not replace raw-evidence or core-evidence retrieval. The list tool requires
`timestamp_from` and `timestamp_to` (at most 31 days), forwarded as Battery API
parameters `from` and `to`, with `max_records` 1..500 (default 100).
No event creation, historical detection, evidence packages, automatic
follow-up, raw-evidence extension or causal interpretation is added.
The target catalogue contains 15 read-only tools, compared with 17 in the
previous GitHub main source; the adapter version is 0.8.2. Existing clients
may hold an older cached catalogue. Refresh the connection metadata after the
coordinated update and verify the 15-tool catalogue; do not infer it from the
version string alone.
Historical events (including drop exactly X) remain readable with their original
IDs and payloads; retrieval does not reclassify them under the new strict > X
recognition rule. A null `reference_module` (and null reference serial) denotes
no uniquely determined previous weakest module at an initial tie. Both tools
forward these values unchanged.

The old API routes `/api/research/events/soc-crashes`,
`/api/research/evidence-package` and `/api/research/evidence-core` return 404.
The replacement tools use `/api/research/soc-crash-events` and its event-ID
route. Old and new calls and results are not equivalent. The coordinated
Battery release follows these confirmed recognition and persistence rules:
configurable X = 5 percentage points and Y = 300 seconds, fixed candidate start,
strictly > X within <= Y, immediate recognition, one candidate ending on role
change, no candidate on an initial tie and a null reference on its subsequent
strict minimum; a later tie alone starts no candidate. No median or additional
recognition criterion is introduced.

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
