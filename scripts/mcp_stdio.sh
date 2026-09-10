#!/usr/bin/env bash
# stdio entrypoint for the MCP server, meant to be launched over SSH by a remote
# agent VM. Encapsulates env (venv, .env, PYTHONPATH) so the agent's MCP config
# only needs:  command: ssh   args: [index-vm, /path/to/scripts/mcp_stdio.sh]
#
# IMPORTANT: stdout is the MCP protocol channel — keep it clean. All diagnostics
# must go to stderr (we force .env/other chatter off stdout below).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# load secrets/config (VOYAGE_API_KEY, DATABASE_URL, ...) without leaking to stdout
if [ -f .env ]; then
  set -a; . ./.env >/dev/null 2>&1; set +a
fi

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

PY="$PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" -m odoo_index.mcp_server "$@"
