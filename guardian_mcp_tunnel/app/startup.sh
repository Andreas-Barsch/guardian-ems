#!/bin/sh
set -eu

fail() {
    printf '%s\n' "Guardian MCP Tunnel: configuration or preflight failed ($1)." >&2
    exit 1
}

STARTUP_FILE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/$(basename -- "$0")"
RUN_FILE="$(dirname -- "$STARTUP_FILE")/../run.sh"
BUILD_INFO_FILE="$(dirname -- "$STARTUP_FILE")/build-info"

if ! python3 - "$BUILD_INFO_FILE" "$STARTUP_FILE" "$RUN_FILE" <<'PY'
import hashlib
import re
import sys

build_info_path, startup_path, run_path = sys.argv[1:]
required = {
    "addon_version",
    "source_revision",
    "tunnel_client_version",
    "startup_sha256",
    "run_sha256",
}
try:
    values = {}
    with open(build_info_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            key, separator, value = line.partition("=")
            if not separator or key in values or key not in required:
                raise ValueError("invalid field")
            values[key] = value
    if set(values) != required:
        raise ValueError("missing field")
    safe_value = re.compile(r"[0-9A-Za-z._+-]{1,128}\Z")
    for key in ("addon_version", "source_revision", "tunnel_client_version"):
        if not safe_value.fullmatch(values[key]):
            raise ValueError("invalid identity")
    sha256 = re.compile(r"[0-9a-f]{64}\Z")
    if not sha256.fullmatch(values["startup_sha256"]) or not sha256.fullmatch(values["run_sha256"]):
        raise ValueError("invalid fingerprint")

    def digest(path):
        value = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                value.update(chunk)
        return value.hexdigest()

    if digest(startup_path) != values["startup_sha256"]:
        raise ValueError("startup_sha256 mismatch")
    if digest(run_path) != values["run_sha256"]:
        raise ValueError("run_sha256 mismatch")
except (OSError, ValueError) as error:
    print(f"Guardian MCP Tunnel: build identity verification failed ({error}).", file=sys.stderr)
    raise SystemExit(1)

print(
    "Guardian MCP Tunnel: build "
    f"version={values['addon_version']} "
    f"revision={values['source_revision']} "
    f"startup_sha256={values['startup_sha256']} "
    f"run_sha256={values['run_sha256']}"
)
PY
then
    fail build_identity
fi

case "${GUARDIAN_TUNNEL_ID:-}" in
    tunnel_[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
    *) fail tunnel_id ;;
esac

[ -n "${GUARDIAN_CONTROL_PLANE_API_KEY:-}" ] || fail control_plane_api_key
[ -n "${GUARDIAN_MCP_AUTH_TOKEN:-}" ] || fail mcp_auth_token

case "${GUARDIAN_MCP_SERVER_URL:-}" in
    http://*|https://*) ;;
    *) fail mcp_server_url ;;
esac
case "$GUARDIAN_MCP_SERVER_URL" in
    *[[:space:]]*|*@*|*\?*|*\#*) fail mcp_server_url ;;
esac

case "${GUARDIAN_TUNNEL_LOG_LEVEL:-info}" in
    debug|info|warn|error) ;;
    *) fail log_level ;;
esac
case "${GUARDIAN_RUN_DOCTOR:-true}" in
    true|false) ;;
    *) fail run_doctor ;;
esac

TUNNEL_CLIENT_BIN="${TUNNEL_CLIENT_BIN:-/usr/local/bin/tunnel-client}"
[ -x "$TUNNEL_CLIENT_BIN" ] || fail tunnel_client

SECRET_DIR="${GUARDIAN_SECRET_DIR:-/run/guardian-mcp-tunnel}"
umask 077
mkdir -p "$SECRET_DIR" || fail secret_storage
CONTROL_PLANE_KEY_FILE="$SECRET_DIR/control-plane-api-key"
MCP_AUTHORIZATION_FILE="$SECRET_DIR/mcp-authorization"
printf '%s' "$GUARDIAN_CONTROL_PLANE_API_KEY" > "$CONTROL_PLANE_KEY_FILE" \
    || fail secret_storage
printf 'Bearer %s' "$GUARDIAN_MCP_AUTH_TOKEN" > "$MCP_AUTHORIZATION_FILE" \
    || fail secret_storage

export CONTROL_PLANE_TUNNEL_ID="$GUARDIAN_TUNNEL_ID"
export MCP_SERVER_URL="$GUARDIAN_MCP_SERVER_URL"
export MCP_EXTRA_HEADERS="Authorization: file:$MCP_AUTHORIZATION_FILE"
export MCP_MAX_CONCURRENT_REQUESTS=2
export MCP_STARTUP_WAIT_TIMEOUT=30s
unset GUARDIAN_CONTROL_PLANE_API_KEY GUARDIAN_MCP_AUTH_TOKEN

printf '%s\n' 'Guardian MCP Tunnel: configuration validated; secrets redacted.'
"$TUNNEL_CLIENT_BIN" --version

if [ "$GUARDIAN_RUN_DOCTOR" = true ]; then
    printf '%s\n' 'Guardian MCP Tunnel: running tunnel-client doctor.'
    printf '%s\n' 'Guardian MCP Tunnel: doctor policy static-bearer-oauth-metadata-v1 active.'
    DOCTOR_REPORT_FILE="$SECRET_DIR/doctor-report.json"
    if "$TUNNEL_CLIENT_BIN" doctor --json \
        --control-plane.api-key="file:$CONTROL_PLANE_KEY_FILE" \
        > "$DOCTOR_REPORT_FILE"; then
        cat "$DOCTOR_REPORT_FILE"
    elif python3 - "$DOCTOR_REPORT_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        report = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)

failed = report.get("failed_checks")
checks = report.get("checks")
if report.get("result") != "fail" or failed != ["oauth_metadata"] or not isinstance(checks, list):
    raise SystemExit(1)

oauth_failures = [
    check for check in checks
    if check.get("id") == "oauth_metadata" and check.get("status") == "FAIL"
]
if len(oauth_failures) != 1:
    raise SystemExit(1)
summary = oauth_failures[0].get("summary", "")
if "protected resource metadata missing resource" not in summary:
    raise SystemExit(1)
PY
    then
        cat "$DOCTOR_REPORT_FILE"
        printf '%s\n' \
            'Guardian MCP Tunnel: doctor OAuth metadata failure accepted for configured static Bearer authentication.'
    else
        cat "$DOCTOR_REPORT_FILE"
        fail doctor
    fi
fi

printf '%s\n' 'Guardian MCP Tunnel: starting outbound tunnel runtime.'
exec "$TUNNEL_CLIENT_BIN" run \
    --control-plane.api-key="file:$CONTROL_PLANE_KEY_FILE" \
    --log.level="${GUARDIAN_TUNNEL_LOG_LEVEL:-info}" \
    --log.format=json
