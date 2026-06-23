#!/bin/bash
# Launch wrapper for the Hermes WebUI server (launchd plist invokes this).
#
# Mirrors the gateway's launch-gateway.sh pattern:
#   - Sources ~/.hermes/.env (mode 600) so the webui inherits OPENROUTER_API_KEY
#     and API_SERVER_KEY without ever putting secrets in the plist (which is
#     mode 644 / world-readable).
#   - Pins the bind host to 127.0.0.1 (loopback only — never expose to LAN).
#     The webui refuses to serve on a public host without an auth password
#     configured; pinning to 127.0.0.1 keeps that posture.
#   - exec so the python process keeps the same PID (no extra wrapper process
#     for launchd to track) — matches the gateway's convention.
#
# Idempotent and safe to re-run.
#
# Never touch aionrs.
set -eo pipefail  # NOTE: not -u — launchd may not set $HOME for some
                     # Aqua/Background contexts; we handle that explicitly
                     # below with ${HOME:-...} defaults.

ENV_FILE="${HOME:-/Users/bzovy}/.hermes/.env"
PYTHON="/Users/bzovy/.hermes/hermes-agent/venv/bin/python"
WEBUI_DIR="${HOME:-/Users/bzovy}/hermes-webui"
LOG_DIR="${HOME:-/Users/bzovy}/.hermes/logs"

# 1) Source .env into the webui's process env (secrets in files only,
#    mode 600, plist stays clean).
#
#    Why line-by-line: ~/.hermes/.env contains values with spaces (e.g.
#    AGENT_BROWSER_EXECUTABLE_PATH=/Applications/Google Chrome.app/...)
#    which `set -a; source .env` would try to EXEC as a command under
#    `set -e`, killing the wrapper before it can exec python.  Parsing
#    each KEY=VALUE and exporting explicitly skips the exec path entirely.
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip blanks + comments
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
    fi
  done < "$ENV_FILE"
fi

# 2) WebUI-specific env (overrides defaults in api/config.py):
#    - HERMES_HOME tells the webui where to find state/sessions/etc.
#    - HERMES_WEBUI_HOST=127.0.0.1 pins to loopback only.
#    - HERMES_WEBUI_PORT=8787 matches the AMO embed's expected endpoint.
#    - PYTHONUNBUFFERED=1 makes stdout/stderr line-flushed so launchd's
#      StandardOutPath log shows the server's banner immediately on boot.
export HERMES_HOME="${HERMES_HOME:-/Users/bzovy/.hermes}"
export HERMES_WEBUI_HOST="127.0.0.1"
export HERMES_WEBUI_PORT="8787"
export PYTHONUNBUFFERED="1"

# 3) Make sure the log dir exists for the webui's own logging.
mkdir -p "$LOG_DIR"

# 4) Hand off to the real webui server.  exec so the python process keeps
#    the same PID (no extra wrapper process for launchd to track).
cd "$WEBUI_DIR"
exec "$PYTHON" "$WEBUI_DIR/server.py"