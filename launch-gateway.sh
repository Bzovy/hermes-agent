#!/bin/bash
# Launch wrapper for the Hermes gateway (launchd plist invokes this).
#
# Two responsibilities, both idempotent and safe to re-run:
#   1. Ensure ~/.hermes/hermes-agent is checked out on a branch that
#      contains the AMO custom endpoints (academy-assist, journal-reflect,
#      brain-ask).  The "main" branch of hermes-agent upstream does NOT
#      have them; only "amo-customizations" does.  Without this guard,
#      a reboot + a `git checkout main` (or a future git migration script)
#      would silently break AMO's AI features again.
#   2. Source ~/.hermes/.env (mode 600) so the gateway process inherits
#      OPENROUTER_API_KEY and API_SERVER_KEY.  hermes's own env_loader
#      also reads ~/.hermes/.env, but doing it here makes the dependency
#      explicit and survives any future refactor of that loader.
#      .env is mode 600 + single source of truth — no secrets ever go
#      in the plist (which is mode 644).
set -eo pipefail  # NOTE: not -u — launchd may not set $HOME for some
                     # Aqua/Background contexts; we handle that explicitly
                     # below with ${HOME:-...} defaults.

REPO="${HOME:-/Users/bzovy}/.hermes/hermes-agent"
ENV_FILE="${HOME:-/Users/bzovy}/.hermes/.env"
# Local branch in the main worktree that tracks the AMO endpoint code.
# We accept either this local name OR a direct "amo-customizations" checkout
# (the local ref is sometimes held by other worktrees, so we don't try to
# recreate it here).
EXPECTED_BRANCHES=("amo-customizations-live" "amo-customizations")

# 1) Branch guard.
if [[ -d "$REPO/.git" ]]; then
  cd "$REPO"
  current=$(git symbolic-ref --short HEAD 2>/dev/null || git rev-parse --short HEAD)
  ok=0
  for b in "${EXPECTED_BRANCHES[@]}"; do
    if [[ "$current" == "$b" ]]; then ok=1; break; fi
  done
  if [[ "$ok" -eq 0 ]]; then
    for b in "${EXPECTED_BRANCHES[@]}"; do
      if git show-ref --verify --quiet "refs/heads/$b"; then
        git checkout "$b" >/dev/null 2>&1 || true
        break
      fi
    done
  fi
fi

# 2) Source .env into the gateway's process env (secrets in files only,
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

# 3) Hand off to the real gateway.  exec so the python process keeps the
#    same PID (no extra wrapper process for launchd to track).
exec /Users/bzovy/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run --replace
