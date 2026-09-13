# Guardian Research MCP

Development version `0.0.0-dev` is a separate, provider-neutral, read-only
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

Set the same non-empty `guardian_api_token` in Guardian Battery and this
add-on. Set a separate non-empty `mcp_auth_token` for MCP clients. Empty tokens
do not enable unauthenticated production access. `development_auth_mode` is
only for isolated tests and must remain false in production.

`guardian_base_url` is mandatory because Home Assistant custom-repository DNS
names depend on the installed repository identifier. Configure the Guardian
Battery internal DNS name shown by Supervisor, replace underscores with
hyphens, append port 8099 and `/api/research`. The URL must not contain
credentials.

The MCP layer never reads Guardian evidence files, writes MQTT/RS485/Hycube,
calls Home Assistant services, rebuilds projections, or stores conversations.
