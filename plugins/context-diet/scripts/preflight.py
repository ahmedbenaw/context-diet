#!/usr/bin/env python3
"""preflight.py - name every dependency as present, missing, or written.

A dependency that fails quietly is worse than one that is absent, so nothing
here is silent. Each row says what was looked for, what was found, and what to
run if it is missing.

Usage:
    preflight.py [--project DIR] [--fetch-tokenizer] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import PLUGIN_ROOT, TOKENIZER_CACHE, load_config, project_root  # noqa: E402


def row(name: str, status: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def check_uv() -> dict:
    path = shutil.which("uv")
    if path:
        return row("uv", "present", path)
    return row("uv", "missing", "not on PATH",
               "curl -LsSf https://astral.sh/uv/install.sh | sh")


def check_tokenizer(fetch: bool) -> dict:
    try:
        cached = TOKENIZER_CACHE.is_dir() and any(TOKENIZER_CACHE.iterdir())
    except OSError:
        cached = False
    try:
        import tiktoken  # noqa: F401,PLC0415

        installed = True
    except Exception:
        installed = False

    if not installed:
        return row("tiktoken", "missing", "not importable; counts fall back to chars/4",
                   "pip install tiktoken")
    if cached:
        return row("tiktoken", "present", "encoding cached at %s" % TOKENIZER_CACHE)
    if not fetch:
        return row("tiktoken", "missing", "installed but encoding not cached; counts use chars/4",
                   "preflight.py --fetch-tokenizer (one network call, then offline forever)")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(TOKENIZER_CACHE)
    TOKENIZER_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        import tiktoken  # noqa: PLC0415

        tiktoken.get_encoding("cl100k_base")
        return row("tiktoken", "written", "encoding downloaded to %s" % TOKENIZER_CACHE)
    except Exception as exc:
        return row("tiktoken", "missing", "download failed: %s" % type(exc).__name__,
                   "check network access to the tokeniser host, then retry")


# MLflow is the one required extra (Ben, 4 Oct 2026). Everything else degrades
# one feature and names which one.
REQUIRED = {"mlflow"}


def check_mlflow(project: Path) -> dict:
    """Asks the interpreter the scripts actually hand MLflow work to.

    Importing MLflow here would test only this python3, and send telemetry on
    import; the subprocess gets the same switches the sink sets.
    """
    from cdlib import mlflow_python  # noqa: PLC0415
    from mlflow_sink import INSTALL  # noqa: PLC0415

    python = mlflow_python(str(project))
    if python is None:
        return row("mlflow", "missing",
                   "required: measure, report, census and the full gate run stop without it",
                   INSTALL)
    env = dict(os.environ, MLFLOW_DISABLE_TELEMETRY="true", DO_NOT_TRACK="true",
               MLFLOW_DISABLE_AGENT_HINT="1")
    try:
        version = subprocess.run([python, "-c", "import mlflow;print(mlflow.__version__)"],
                                 capture_output=True, text=True, timeout=120,
                                 env=env).stdout.strip().splitlines()[-1]
    except (OSError, subprocess.SubprocessError, IndexError):
        version = "unknown"
    return row("mlflow", "present", "version %s (%s)" % (version, Path(python).name))


def check_mcp_json(project: Path, cfg: dict, write: bool) -> dict:
    target = project / ".mcp.json"
    uri = cfg.get("mlflow_tracking_uri")
    if target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except ValueError:
            return row(".mcp.json", "missing", "present but unparseable", "fix or delete the file")
        if "mlflow-mcp" in (data.get("mcpServers") or {}):
            return row(".mcp.json", "present", "mlflow-mcp registered at project scope")
        return row(".mcp.json", "missing", "present without an mlflow-mcp entry",
                   "scripts/mlflow_bootstrap.sh")
    if not write:
        return row(".mcp.json", "missing", "no project-scope MCP registration",
                   "scripts/mlflow_bootstrap.sh")
    return row(".mcp.json", "missing", "not written by preflight; bootstrap owns it (%s)" % uri,
               "scripts/mlflow_bootstrap.sh")


def check_state(project: Path) -> dict:
    d = project / ".claude" / "context-diet"
    if d.is_dir():
        return row("state dir", "present", str(d))
    return row("state dir", "missing", "created on first hook run", "no action")


def check_config(project: Path, cfg: dict) -> dict:
    path = project / ".claude" / "context-diet.json"
    if path.is_file():
        return row("config", "present", str(path))
    return row("config", "missing", "running on defaults; print them with --json",
               "write .claude/context-diet.json to override any threshold")


def check_git() -> dict:
    path = shutil.which("git")
    if not path:
        return row("git", "missing", "handoff manifests cannot record a commit", "install git")
    try:
        out = subprocess.run(["git", "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5)
        return row("git", "present", out.stdout.decode().strip())
    except Exception:
        return row("git", "present", path)


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet preflight")
    ap.add_argument("--project", default=".")
    ap.add_argument("--fetch-tokenizer", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    project = project_root(args.project).resolve()
    cfg = load_config(args.project)
    rows = [
        check_uv(),
        check_tokenizer(args.fetch_tokenizer),
        check_mlflow(project),
        check_mcp_json(project, cfg, write=False),
        check_config(project, cfg),
        check_state(project),
        check_git(),
    ]

    required_missing = [r for r in rows if r["name"] in REQUIRED and r["status"] == "missing"]
    if args.json:
        sys.stdout.write(json.dumps({"project": str(project), "plugin": str(PLUGIN_ROOT),
                                     "checks": rows, "effective_config": cfg}, indent=2) + "\n")
        return 1 if required_missing else 0

    sys.stdout.write("context-diet preflight  (project: %s)\n\n" % project)
    sys.stdout.write("%-12s %-9s %s\n" % ("dependency", "status", "detail"))
    sys.stdout.write("-" * 76 + "\n")
    for r in rows:
        sys.stdout.write("%-12s %-9s %s\n" % (r["name"], r["status"], r["detail"]))
    sys.stdout.write("\n")
    fixes = [r for r in rows if r["status"] == "missing" and r["fix"] and r["fix"] != "no action"]
    if fixes:
        sys.stdout.write("to fix:\n")
        for r in fixes:
            sys.stdout.write("  %-12s %s\n" % (r["name"], r["fix"]))
        sys.stdout.write("\n")
    if required_missing:
        sys.stdout.write("Required and missing: %s. Install it before running /measure, the "
                         "report, the census or the gates.\n"
                         % ", ".join(r["name"] for r in required_missing))
        return 1
    sys.stdout.write("MLflow is required and present. Every other missing item degrades one "
                     "feature and names which one.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
