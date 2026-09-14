# Guardian MCP Tunnel

Guardian MCP Tunnel `0.8.1` connects the private Guardian Research MCP to an
OpenAI Secure MCP Tunnel. It runs outbound-only on Home Assistant Green; it
does not publish a host port and does not make Guardian port 8098 public.

## Architecture

`ChatGPT → OpenAI Secure MCP Tunnel → guardian_mcp_tunnel → Guardian Research MCP → Guardian Battery Research API`

This path was accepted end to end on Home Assistant Green/aarch64 with Guardian
MCP Tunnel 0.8.1, tunnel-client 0.0.14, Guardian Research MCP 0.8.0 after the
explicit HA DNS host was configured, and Guardian Battery 0.8.0. Guardian
Research MCP 0.8.1 now incorporates that host in its safe default. The path is
outbound-only: normal operation requires no inbound router rule, public MCP
port, reverse proxy, Cloudflare dependency, Mac, or other external host.

The add-on supports only `aarch64`. This matches Home Assistant Green and the
pinned official OpenAI Linux ARM64 artifact. Other architectures are omitted
rather than silently using an unverified binary.

## Prerequisites and installation

1. Install and configure Guardian Battery `0.8.0`.
2. Install and configure Guardian Research MCP `0.8.1` without exposing its
   port publicly.
3. Create the Secure MCP Tunnel and restricted runtime key in OpenAI Platform.
4. Add this directory as a local Home Assistant add-on, build it, configure the
   options below, and start it. No router port-forward is required.

The Docker build downloads only the official
`openai/tunnel-client` v0.0.14 Linux ARM64 release archive and verifies its
pinned SHA-256 before installation. To upgrade, update the version, exact
official release URL and published checksum together, then repeat build and
runtime acceptance on aarch64.

Home Assistant supplies `BUILD_VERSION` from `config.yaml`. The Dockerfile also
supports a non-secret `SOURCE_REVISION` build argument; its deterministic local
build default is `guardian-mcp-tunnel-0.8.1` because the Supervisor does not
inject a Git revision. Release builds may override it with an approved source
revision without requiring `.git` in the build context. Both values and the
pinned tunnel-client version are recorded as OCI labels.

## Configuration

- `tunnel_id`: OpenAI tunnel ID (`tunnel_` plus 32 lowercase hex characters).
- `control_plane_api_key`: restricted OpenAI tunnel runtime key. It is used
  only by tunnel-client for OpenAI control-plane communication.
- `mcp_auth_token`: local Guardian Research MCP client token.
- `mcp_server_url`: private MCP URL; defaults to
  `http://3195b09a-guardian-research-mcp:8098/mcp`.
- `run_doctor`: run the official preflight before the long-lived process.
- `log_level`: tunnel-client log level.

The three Guardian secrets have distinct roles:

- Guardian Battery `guardian_research_api_token` authenticates Research API
  machine access.
- Guardian Research MCP `guardian_api_token` must match that Battery token;
  its separate `mcp_auth_token` protects MCP clients.
- Guardian MCP Tunnel `control_plane_api_key` authenticates to OpenAI;
  its `mcp_auth_token` must match Guardian Research MCP's MCP client token.

Never commit any of these values. Production must keep the Research MCP port
private and its authentication enabled.

The authentication boundaries remain separate:

1. Guardian Battery's `guardian_research_api_token` matches Guardian Research
   MCP's `guardian_api_token` for the read-only Research API.
2. Guardian Research MCP's `mcp_auth_token` protects `/mcp`; Guardian MCP
   Tunnel supplies the same value locally through the file-backed Bearer header.
3. The restricted OpenAI tunnel runtime key authenticates only the outbound
   tunnel-client control-plane connection.

The ChatGPT connector uses the assigned OpenAI Secure MCP Tunnel and no
additional ChatGPT-side authentication. This does not remove local MCP Bearer
authentication and does not disclose `mcp_auth_token` to ChatGPT.

## Startup and verification

Startup validates required values without printing them. The short startup
process writes both credentials with mode `0600` below container-local `/run`,
unsets their input variables, and passes only `file:` references to the
long-lived process. Local MCP bearer auth uses the official v0.0.14 contract:

`MCP_EXTRA_HEADERS=Authorization: file:/run/guardian-mcp-tunnel/mcp-authorization`

The resolved header is scoped to the configured MCP origin and is not sent to
the OpenAI control plane. After the optional `doctor` preflight, `run` becomes
the container's foreground process. Fatal validation or preflight errors exit
non-zero for Supervisor visibility; tunnel-client handles normal reconnects.

At build time `/app/build-info` records the add-on version, source revision,
tunnel-client version, and SHA-256 fingerprints of `/app/startup.sh` and
`/run.sh`. Startup recalculates both fingerprints and exits fail-closed on a
missing, malformed, or mismatching identity. A successful check emits one
compact `Guardian MCP Tunnel: build version=...` line without configuration or
secret values. Revalidate both this fingerprint contract and the Doctor
contract whenever tunnel-client is upgraded.

Verify Supervisor state and redacted logs, then confirm the tunnel connector
in OpenAI Platform and call `guardian_status` from the assigned ChatGPT
workspace. Do not expose port 8098 or add firewall/router rules.

The production acceptance successfully invoked `guardian_status` from a new
ChatGPT conversation through the complete tunnel, MCP, and Guardian Research
API path. No causal interpretation or write capability is added by this path.

## Troubleshooting and security

- `tunnel_id` failure: verify the exact lowercase tunnel identifier.
- `doctor` failure: check outbound connectivity, the restricted key's Tunnel
  Read/Use permissions, and internal MCP DNS. Do not print the secrets.
- HTTP 421 `Misdirected Request`: Guardian Research MCP must explicitly allow
  the actual internal host `3195b09a-guardian-research-mcp`. Retain the existing
  explicit hosts; never substitute a wildcard.
- `oauth_metadata` with `protected resource metadata missing resource`:
  tunnel-client 0.0.14 probes OAuth metadata even though this private MCP uses
  static Bearer authentication. The startup policy accepts only this sole,
  exact Doctor failure after the file-backed Bearer header is configured. Any
  additional or different Doctor failure remains fatal.
- Local MCP 401: synchronize this add-on's `mcp_auth_token` with Guardian
  Research MCP, retaining Bearer authentication.
- MCP unavailable: verify `3195b09a-guardian-research-mcp` is running; the
  tunnel add-on requires no interactive login and can be restarted by
  Supervisor after its dependency recovers.

For runtime verification, require the `build version=0.8.1 ...` fingerprint
line, the `static-bearer-oauth-metadata-v1 active` policy marker, successful
configuration validation, and tunnel-client startup in the same latest
Supervisor start log.

Treat app option output as sensitive: `ha apps info` may expose password or
token fields in some environments. Never copy unreviewed option sections into
screenshots, tickets, logs, or chats. If a token is accidentally exposed,
rotate it immediately and do not preserve the old or replacement value in
documentation.

The add-on has no ingress, host port, Home Assistant API permission, storage
mount, Guardian file access, MQTT, RS485, Hycube, maintenance or config write.
