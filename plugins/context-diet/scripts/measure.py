#!/usr/bin/env python3
"""measure.py - the A/B run that makes the original claim testable.

Three configs against the same five tasks, three trials each, per model:
  Fat   the instruction layer exactly as it is
  Lean  the same layer after the audit's cuts
  Bare  no CLAUDE.md at all, which is the Reddit prescription tested honestly

45 runs per model. Opus and Fable first. Other models are a separate opt-in
batch, because 45 runs per model is a real cost and running every model at once
buys nothing the first two do not already answer.

Everything is recorded in separate columns. Fresh input, cache creation and
cache read are never summed into one headline number, because merging them is
exactly how a cumulative counter gets quoted as evidence of efficiency.

measure.py refuses any path that is not the committed fixture. A harness that
can run 45 mutating tasks against a real codebase is a hazard, not a tool.

Usage:
    measure.py run --models claude-opus-5,claude-fable-5-1 [--trials 3] [--dry-run]
    measure.py tasks
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import PLUGIN_ROOT, append_ledger, load_config, state_dir  # noqa: E402

FIXTURE = PLUGIN_ROOT / "fixture"
CONFIGS = ("Fat", "Lean", "Bare")

TASKS = [
    {
        "id": "add-field",
        "prompt": "Add a required `currency` field to the Order model in src/models/order.ts, "
                  "update every call site, and add a test that covers it.",
        "check": 'grep -q "currency" src/models/order.ts && grep -rq "currency" tests '
                 "&& npm run typecheck && npm test",
    },
    {
        "id": "fix-broken-test",
        "prompt": "One test is failing. Fix the bug in the source so the suite passes. "
                  "Do not edit the test file.",
        "check": "git diff --quiet main -- tests/ && npm test",
        "branch": "broken-test",
    },
    {
        "id": "explain-module",
        "prompt": "Explain what src/services/pricing.ts does. Write your answer to answer.txt "
                  "and name the functions it actually contains.",
        "check": '[ "$(grep -oE \'export function [A-Za-z0-9_]+\' src/services/pricing.ts '
                 "| awk '{print $3}' | while read -r f; do grep -q \"$f\" answer.txt && echo x; "
                 'done | wc -l | tr -d \' \')" -ge 3 ]',
    },
    {
        "id": "rename-symbol",
        "prompt": "Rename the exported helper makeIdKey to buildIdKey everywhere it is used.",
        "check": '! grep -rq "makeIdKey" src tests '
                 '&& [ "$(grep -rl "buildIdKey" src tests | wc -l | tr -d \' \')" -ge 4 ] '
                 "&& npm run typecheck",
    },
    {
        "id": "add-script",
        "prompt": "Add an npm script named smoke that runs the demo entrypoint once, then run it.",
        "check": '[ "$(npm pkg get scripts.smoke)" != "{}" ] && npm run smoke',
    },
]


def workspace() -> Path:
    """Where the harness actually runs.

    The shipped fixture is never mutated. Every trial runs in a disposable copy
    under the project's own state directory, which keeps the published repo free
    of a nested git repository and makes a botched run recoverable by deleting
    one directory.
    """
    return Path(os.environ.get("CONTEXT_DIET_WORKSPACE")
                or (Path.cwd() / ".claude" / "context-diet" / "ab" / "workspace"))


def guard_workspace(path: Path) -> None:
    """The one tree this harness may mutate."""
    resolved = path.resolve()
    if resolved == FIXTURE.resolve():
        raise SystemExit("the shipped fixture is read-only; runs happen in a workspace copy")
    if not (resolved / "package.json").is_file():
        raise SystemExit("workspace is not prepared: %s" % resolved)
    if ".claude/context-diet" not in str(resolved) and not os.environ.get(
        "CONTEXT_DIET_WORKSPACE"
    ):
        raise SystemExit(
            "measure.py refuses to run against %s. It only runs in its own workspace copy."
            % resolved
        )


def prepare_workspace() -> Path:
    """Copy the fixture, make it a git repo, rebuild the broken-test branch.

    The branch that carries the deliberate bug ships as broken-test.patch rather
    than as a nested repository, because a nested repository inside a published
    repo is an unusable gitlink for anyone who clones it.
    """
    ws = workspace()
    if ws.exists():
        shutil.rmtree(ws)
    ws.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, ws, ignore=shutil.ignore_patterns(".git"))
    guard_workspace(ws)
    run = lambda args: subprocess.run(args, cwd=str(ws), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=180)
    run(["git", "init", "-q", "-b", "main"])
    run(["git", "add", "-A"])
    run(["git", "-c", "user.email=fixture@local", "-c", "user.name=fixture",
         "commit", "-q", "-m", "fixture baseline"])
    patch = ws / "broken-test.patch"
    if patch.is_file():
        run(["git", "checkout", "-q", "-b", "broken-test"])
        run(["git", "apply", str(patch)])
        run(["git", "-c", "user.email=fixture@local", "-c", "user.name=fixture",
             "commit", "-aqm", "introduce the deliberate rounding bug"])
        run(["git", "checkout", "-q", "main"])
    node_modules = FIXTURE / "node_modules"
    if node_modules.is_dir() and not (ws / "node_modules").exists():
        os.symlink(node_modules, ws / "node_modules")
    return ws


def git(args: list, cwd: Path) -> str:
    out = subprocess.run(["git"] + args, cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, timeout=60)
    return out.stdout.decode("utf-8", "replace").strip()


def reset_workspace(ws: Path, branch: str = "main") -> None:
    """Every trial starts identical. Untracked files go, ignored files stay.

    answer.txt is deliberately not ignored, so it is removed between trials. If
    it survived, one arm would pass the explain task on the previous arm's work.
    node_modules is ignored, so the dependency store survives.
    """
    guard_workspace(ws)
    git(["checkout", "--quiet", branch], ws)
    git(["reset", "--hard", "--quiet", branch], ws)
    git(["clean", "-qfdx", "-e", "node_modules"], ws)


def apply_config(config: str, ws: Path) -> dict:
    """Fat keeps CLAUDE.md, Lean uses the cut version, Bare removes it."""
    guard_workspace(ws)
    claude_md = ws / "CLAUDE.md"
    lean_md = PLUGIN_ROOT / "tests" / "fixtures" / "lean-CLAUDE.md"
    state = {"config": config}
    if config == "Fat":
        state["claude_md_present"] = claude_md.is_file()
    elif config == "Lean":
        if lean_md.is_file():
            shutil.copy(lean_md, claude_md)
            state["claude_md_present"] = True
        else:
            state["claude_md_present"] = claude_md.is_file()
            state["note"] = "no lean variant on disk; Lean ran identical to Fat"
    elif config == "Bare":
        if claude_md.is_file():
            claude_md.unlink()
        state["claude_md_present"] = False
    return state


def run_check(check: str, ws: Path) -> tuple:
    started = time.time()
    proc = subprocess.run(["bash", "-lc", check], cwd=str(ws), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=900)
    return (proc.returncode == 0, (time.time() - started) * 1000,
            proc.stdout.decode("utf-8", "replace")[-400:])


def agent_available() -> str:
    """Is there a headless agent runner on this machine?"""
    return shutil.which("claude") or ""


def run_trial(model: str, config: str, task: dict, trial: int, dry_run: bool,
              ws: Path) -> dict:
    branch = task.get("branch", "main")
    reset_workspace(ws, branch)
    cfg_state = apply_config(config, ws)

    record = {
        "ts": int(time.time()),
        "model": model,
        "config": config,
        "task": task["id"],
        "trial": trial,
        "claude_md_present": cfg_state.get("claude_md_present"),
        "fresh_input": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "output": 0,
        "wall_ms": 0,
        "tool_calls": 0,
        "file_reads": 0,
        "passed": False,
        "note": cfg_state.get("note", ""),
    }

    runner = agent_available()
    if dry_run or not runner:
        passed, ms, _tail = run_check(task["check"], ws)
        record["wall_ms"] = int(ms)
        record["passed"] = passed
        record["note"] = (record["note"] + " " if record["note"] else "") + (
            "dry run: the pass check ran, no agent was invoked"
            if dry_run else
            "no headless agent runner on PATH; only the pass check ran"
        )
        return record

    started = time.time()
    proc = subprocess.run(
        [runner, "-p", task["prompt"], "--model", model, "--output-format", "json"],
        cwd=str(ws), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600,
    )
    record["wall_ms"] = int((time.time() - started) * 1000)
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
        usage = data.get("usage") or {}
        record["fresh_input"] = int(usage.get("input_tokens") or 0)
        record["cache_creation"] = int(usage.get("cache_creation_input_tokens") or 0)
        record["cache_read"] = int(usage.get("cache_read_input_tokens") or 0)
        record["output"] = int(usage.get("output_tokens") or 0)
    except Exception:
        record["note"] = (record["note"] + " " if record["note"] else "") + \
            "agent output was not parseable json; token columns are zero, not estimated"

    passed, _ms, _tail = run_check(task["check"], ws)
    record["passed"] = passed
    return record


def do_run(args) -> int:
    ws = prepare_workspace()
    cfg = load_config(args.project)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        sys.stderr.write("no models given\n")
        return 2

    runner = agent_available()
    if not runner and not args.dry_run:
        sys.stdout.write(
            "No headless agent runner is on PATH, so no agent work can be measured. The pass "
            "checks will still run and every token column will be zero. Re-run with --dry-run "
            "to say so explicitly, or install the runner.\n"
        )

    out = state_dir(args.project) / "ab"
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "runs.tsv"
    records = []
    total = len(models) * len(CONFIGS) * len(TASKS) * args.trials
    done = 0

    for model in models:
        for config in CONFIGS:
            for task in TASKS:
                for trial in range(1, args.trials + 1):
                    rec = run_trial(model, config, task, trial, args.dry_run, ws)
                    records.append(rec)
                    done += 1
                    sys.stdout.write(
                        "[%d/%d] %s %s %s trial %d: %s\n"
                        % (done, total, model, config, task["id"], trial,
                           "pass" if rec["passed"] else "fail")
                    )
                    sys.stdout.flush()
                    try:
                        from mlflow_sink import log_ab_run  # noqa: PLC0415

                        log_ab_run(rec, cfg)
                    except Exception:
                        pass

    header = list(records[0].keys())
    with results_path.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(header) + "\n")
        for r in records:
            fh.write("\t".join(str(r[k]).replace("\t", " ") for k in header) + "\n")
    reset_workspace(ws, "main")
    sys.stdout.write("\n%d runs written to %s\n" % (len(records), results_path))
    sys.stdout.write("Render it with: report.py --runs %s\n" % results_path)
    return 0


def do_tasks(_args) -> int:
    for t in TASKS:
        sys.stdout.write("%-18s branch=%-12s %s\n"
                         % (t["id"], t.get("branch", "main"), t["prompt"]))
        sys.stdout.write("%-18s check: %s\n\n" % ("", t["check"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet A/B harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--models", default="claude-opus-5,claude-fable-5-1")
    r.add_argument("--trials", type=int, default=3)
    r.add_argument("--project", default=".")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=do_run)
    t = sub.add_parser("tasks")
    t.set_defaults(fn=do_tasks)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
