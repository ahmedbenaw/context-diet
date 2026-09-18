#!/usr/bin/env bash
# mlflow_bootstrap.sh - register the MLflow MCP server at PROJECT scope, idempotently.
#
# Why not the obvious `claude mcp add`: that writes user-level config, which puts
# the server's tool schemas into the prefix of every session on the machine. On
# this machine MCP schemas are already the largest single prefix cost, so a
# global registration would add to the exact number the plugin exists to measure.
# This script registers at project scope and prints the promote command for the
# user to run themselves once they have seen the cost.
#
# Run twice and the .mcp.json is byte-identical.

set -euo pipefail

REPO="${1:-$(pwd)}"
TRACKING_URI="${MLFLOW_TRACKING_URI:-file://${REPO}/.claude/context-diet/mlruns}"
MCP_JSON="${REPO}/.mcp.json"
MLRUNS="${REPO}/.claude/context-diet/mlruns"
STATUS="ok"

say() { printf 'context-diet bootstrap: %s\n' "$1"; }

if ! command -v uv >/dev/null 2>&1; then
  say "uv is not on PATH; mlflow marked unavailable"
  say "install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
  STATUS="unavailable"
fi

if [ "$STATUS" = "ok" ]; then
  if ! python3 - <<'PY' >/dev/null 2>&1
import sys
try:
    import mlflow
except Exception:
    sys.exit(1)
parts = tuple(int(x) for x in mlflow.__version__.split(".")[:3] if x.isdigit())
sys.exit(0 if parts >= (3, 5, 1) else 1)
PY
  then
    say "mlflow >= 3.5.1 not importable by python3; scorers still run as plain checks"
    say "install it into this repo's environment with: uv pip install 'mlflow[mcp]>=3.5.1'"
    STATUS="unavailable"
  fi
fi

mkdir -p "$MLRUNS"

if [ -f "$MCP_JSON" ] && grep -q '"mlflow-mcp"' "$MCP_JSON" 2>/dev/null; then
  say "mlflow-mcp already registered (project scope)"
else
  python3 - "$MCP_JSON" "$TRACKING_URI" <<'PY'
import json, sys, pathlib
target = pathlib.Path(sys.argv[1])
uri = sys.argv[2]
data = {}
if target.is_file():
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        data = {}
servers = data.setdefault("mcpServers", {})
servers["mlflow-mcp"] = {
    "command": "uv",
    "args": ["run", "--with", "mlflow[mcp]>=3.5.1", "mlflow", "mcp", "run"],
    "env": {"MLFLOW_TRACKING_URI": uri},
}
target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  say "wrote ${MCP_JSON} (project scope)"
fi

say "tracking ${TRACKING_URI} · status ${STATUS}"
cat <<EOF
context-diet bootstrap: to promote this server to every session on the machine, run
  claude mcp add mlflow-mcp -e MLFLOW_TRACKING_URI=${TRACKING_URI} -- uv run --with "mlflow[mcp]>=3.5.1" mlflow mcp run
Check its schema cost in /context-diet:budget first. A user-scope server pays its
schema cost on every turn of every session.
EOF
