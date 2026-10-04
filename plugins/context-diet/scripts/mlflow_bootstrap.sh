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
#
# Usage: mlflow_bootstrap.sh [--install] [REPO]
# Without --install it only checks and prints. With --install, when no
# interpreter can import MLflow, it creates REPO/.venv with uv and installs
# mlflow[mcp] there, never into system site-packages.

set -euo pipefail

INSTALL=0
REPO_ARG=""
for arg in "$@"; do
  case "$arg" in
    --install) INSTALL=1 ;;
    *) REPO_ARG="$arg" ;;
  esac
done

# Absolutise. A relative REPO leaks a relative path into the tracking URI and
# into the promote command, which then resolve against whatever cwd runs them.
REPO="$(cd "${REPO_ARG:-$(pwd)}" && pwd)"
# MLflow 3.16 put the filesystem store into maintenance mode and refuses a
# file:// URI, so the default is a local SQLite file. It must be the SAME store
# the Python sink writes to, or the MCP server reads an empty database while
# every script logs somewhere else.
#
# Two spellings of one store. .mcp.json is project scope and committed, and the
# MCP server starts in the project directory, so it gets the relative form: an
# absolute one put a home directory into a public repo. The promote command is
# user scope and runs from anywhere, so it gets the absolute form. They are
# compared by the file they resolve to, never as strings, or each spelling
# reads as a mismatch and the bootstrap rewrites the other one.
PROJECT_URI="${MLFLOW_TRACKING_URI:-sqlite:///./.claude/context-diet/mlflow.db}"
resolve_uri() {
  python3 -c 'import sys, pathlib
uri, repo = sys.argv[1], pathlib.Path(sys.argv[2])
if uri.startswith("sqlite:///"):
    tail = uri[len("sqlite:///"):]
    p = pathlib.Path(tail)
    print("sqlite:///" + str((p if p.is_absolute() else repo / p).resolve()))
else:
    print(uri)' "$1" "$REPO"
}
TRACKING_URI="$(resolve_uri "$PROJECT_URI")"
MCP_JSON="${REPO}/.mcp.json"
STORE_DIR="${REPO}/.claude/context-diet"
STATUS="ok"

say() { printf 'context-diet bootstrap: %s\n' "$1"; }

if ! command -v uv >/dev/null 2>&1; then
  say "uv is not on PATH; mlflow marked unavailable"
  say "install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
  STATUS="unavailable"
fi

# Same order as cdlib.mlflow_python: python3 first, then the project's .venv.
VENV_PY="${REPO}/.venv/bin/python"
has_mlflow() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
try:
    import mlflow
except Exception:
    sys.exit(1)
parts = tuple(int(x) for x in mlflow.__version__.split(".")[:3] if x.isdigit())
sys.exit(0 if parts >= (3, 5, 1) else 1)
PY
}
mlflow_found() {
  has_mlflow python3 || { [ -x "$VENV_PY" ] && has_mlflow "$VENV_PY"; }
}

if [ "$STATUS" = "ok" ] && ! mlflow_found; then
  if [ "$INSTALL" = "1" ]; then
    say "installing mlflow[mcp] into ${REPO}/.venv"
    # A failed step is reported, never a crash: set -e would otherwise end the
    # run before the .mcp.json step and the status line.
    if { [ -x "$VENV_PY" ] || uv venv "${REPO}/.venv" >/dev/null; } \
        && uv pip install --python "$VENV_PY" 'mlflow[mcp]>=3.5.1' >/dev/null \
        && mlflow_found; then
      say "mlflow installed into ${REPO}/.venv"
    else
      say "the install did not leave an importable mlflow; scorers still run as plain checks"
      STATUS="unavailable"
    fi
  else
    say "mlflow >= 3.5.1 not importable by python3 or ${REPO}/.venv; scorers still run as plain checks"
    say "install it into this repo's .venv with: $0 --install ${REPO}"
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

if [ -n "$CURRENT_URI" ] && [ "$(resolve_uri "$CURRENT_URI")" = "$TRACKING_URI" ]; then
  say "mlflow-mcp already registered with this tracking URI (project scope)"
else
  if python3 - "$MCP_JSON" "$PROJECT_URI" <<'PY'
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
