#!/usr/bin/with-contenv bashio
set -eu

export GUARDIAN_TUNNEL_ID="$(bashio::config 'tunnel_id')"
export GUARDIAN_CONTROL_PLANE_API_KEY="$(bashio::config 'control_plane_api_key')"
export GUARDIAN_MCP_AUTH_TOKEN="$(bashio::config 'mcp_auth_token')"
export GUARDIAN_MCP_SERVER_URL="$(bashio::config 'mcp_server_url')"
export GUARDIAN_RUN_DOCTOR="$(bashio::config 'run_doctor')"
export GUARDIAN_TUNNEL_LOG_LEVEL="$(bashio::config 'log_level')"

exec /app/startup.sh
