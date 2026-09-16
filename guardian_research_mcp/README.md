# Guardian Research MCP

Version `0.8.1` is a separate, provider-neutral, read-only
Streamable HTTP MCP adapter for Guardian Battery's Research API.

## Contract

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

The additive `build_soc_crash_core_evidence(event_id)` tool is the sixteenth
read-only tool. It forwards only the event ID to Guardian's fixed-window Core
contract; it has no caller-controlled time-window expansion. Existing tools,
including `build_evidence_package`, retain their contracts.

For the local Home Assistant application network, the safe default Host
allowlist is `guardian_research_mcp,3195b09a-guardian-research-mcp,localhost,127.0.0.1`.
The repository-qualified DNS name is the Host used by Guardian MCP Tunnel's
default internal URL. Keep the explicit entries and do not replace them with a
wildcard. An HTTP 421 `Misdirected Request` from the tunnel preflight indicates
that the configured internal MCP hostname is missing from this allowlist.
