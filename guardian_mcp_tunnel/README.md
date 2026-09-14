# Guardian MCP Tunnel

Guardian MCP Tunnel `0.8.1` connects the private Guardian Research MCP to an
OpenAI Secure MCP Tunnel. It runs outbound-only on Home Assistant Green; it
does not publish a host port and does not make Guardian port 8098 public.

## Architecture

`ChatGPT → OpenAI Secure MCP Tunnel → guardian_mcp_tunnel → Guardian Research MCP → Guardian Battery Research API`

The add-on supports only `aarch64`. This matches Home Assistant Green and the
pinned official OpenAI Linux ARM64 artifact. Other architectures are omitted
rather than silently using an unverified binary.

## Prerequisites and installation

1. Install and configure Guardian Battery `0.8.0`.
2. Install and configure Guardian Research MCP `0.8.0` without exposing its
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

## Troubleshooting and security

- `tunnel_id` failure: verify the exact lowercase tunnel identifier.
- `doctor` failure: check outbound connectivity, the restricted key's Tunnel
  Read/Use permissions, and internal MCP DNS. Do not print the secrets.
- Local MCP 401: synchronize this add-on's `mcp_auth_token` with Guardian
  Research MCP, retaining Bearer authentication.
- MCP unavailable: verify `3195b09a-guardian-research-mcp` is running; the
  tunnel add-on requires no interactive login and can be restarted by
  Supervisor after its dependency recovers.

The add-on has no ingress, host port, Home Assistant API permission, storage
mount, Guardian file access, MQTT, RS485, Hycube, maintenance or config write.
