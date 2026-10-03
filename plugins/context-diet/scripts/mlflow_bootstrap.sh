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

# Absolutise. A relative REPO leaks a relative path into the tracking URI and
# into the promote command, which then resolve against whatever cwd runs them.
REPO="$(cd "${1:-$(pwd)}" && pwd)"
# MLflow 3.16 put the filesystem store into maintenance mode and refuses a
# file:// URI, so the default is a local SQLite file. It must be the SAME store
# the Python sink writes to, or the MCP server reads an empty database while
# every script logs somewhere else.
TRACKING_URI="${MLFLOW_TRACKING_URI:-sqlite:///${REPO}/.claude/context-diet/mlflow.db}"
MCP_JSON="${REPO}/.mcp.json"
STORE_DIR="${REPO}/.claude/context-diet"
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

mkdir -p "$STORE_DIR"

# Reconcile on content, not on presence. Checking only that the key existed left
# a stale tracking URI in place forever, so the MCP server read an empty store
# while every script logged to a different one.
CURRENT_URI=""
if [ -f "$MCP_JSON" ]; then
  CURRENT_URI="$(python3 -c 'import json,sys,pathlib
try:
    d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    print(""); raise SystemExit
s = (d.get("mcpServers") or {}).get("mlflow-mcp") or {}
print((s.get("env") or {}).get("MLFLOW_TRACKING_URI") or "")' "$MCP_JSON" 2>/dev/null || true)"
fi

if [ "$CURRENT_URI" = "$TRACKING_URI" ]; then
  say "mlflow-mcp already registered with this tracking URI (project scope)"
else
  if python3 - "$MCP_JSON" "$TRACKING_URI" <<'PY'
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
# An unwritable .mcp.json is an ordinary outcome, not a crash: it is a
# security-sensitive file that a sandbox or a read-only checkout may protect.
# Say which value it needs and exit non-zero so the caller can report it.
try:
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
except OSError as exc:
    sys.stderr.write("cannot write %s: %s\n" % (target, exc))
    raise SystemExit(3)
PY
  then
    say "wrote ${MCP_JSON} (project scope)"
  else
    say "could not write ${MCP_JSON}; it needs MLFLOW_TRACKING_URI set to:"
    say "  ${TRACKING_URI}"
    say "until then the MCP server reads a different store than the scripts write"
    STATUS="unregistered"
  fi
fi

say "tracking ${TRACKING_URI} · status ${STATUS}"
cat <<EOF
context-diet bootstrap: to promote this server to every session on the machine, run
  claude mcp add mlflow-mcp -e MLFLOW_TRACKING_URI=${TRACKING_URI} -- uv run --with "mlflow[mcp]>=3.5.1" mlflow mcp run
Check its schema cost in /context-diet:budget first. A user-scope server pays its
schema cost on every turn of every session.
EOF
