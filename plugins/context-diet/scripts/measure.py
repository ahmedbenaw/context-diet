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


MARKER = ".context-diet-workspace"


def force_rmtree(path: Path) -> None:
    """Remove a tree that contains a git object store.

    Git writes its objects read-only, at mode 0444, so a plain delete stops on
    the first one with "Operation not permitted". Adding the write bit to the
    file is not enough either: unlinking an entry needs the write bit on the
    directory that holds it, so the walk then fails with "Directory not empty"
    instead. The whole tree gets owner write and execute first, then one delete.
    Anything that still fails is raised, not swallowed, because a half-deleted
    workspace is not a clean baseline.
    """
    for parent, dirnames, filenames in os.walk(path):
        for name in [parent] + [os.path.join(parent, n) for n in dirnames + filenames]:
            try:
                os.chmod(name, os.stat(name).st_mode | 0o700)
            except OSError:
                pass
    shutil.rmtree(path)


def default_workspace(project: str = ".") -> Path:
    """The path this script builds for itself. Deleting it is always its own business."""
    return Path(project).resolve() / ".claude" / "context-diet" / "ab" / "workspace"


def workspace(project: str = ".") -> Path:
    """Where the harness actually runs.

    The shipped fixture is never mutated. Every trial runs in a disposable copy
    under the project's own state directory, which keeps the published repo free
    of a nested git repository and makes a botched run recoverable by deleting
    one directory.

    The path comes from --project, not from the current working directory.
    Deriving it from cwd put the results in a directory that held none of the
    work whenever the two differed.
    """
    override = os.environ.get("CONTEXT_DIET_WORKSPACE")
    if override:
        return Path(override)
    return default_workspace(project)


def guard_workspace(path: Path) -> None:
    """The one tree this harness may mutate.

    CONTEXT_DIET_WORKSPACE used to switch this fence off, so pointing that
    variable at any directory made it writable, and the delete in
    prepare_workspace ran before the fence anyway. The variable now only chooses
    the path. What authorises writing is the marker file, which nothing but this
    script writes, so a directory context-diet did not create cannot be claimed
    by setting an environment variable.
    """
    resolved = path.resolve()
    if resolved == FIXTURE.resolve():
        raise SystemExit("the shipped fixture is read-only; runs happen in a workspace copy")
    if not (resolved / MARKER).is_file():
        raise SystemExit(
            "measure.py refuses to touch %s: no %s marker, so context-diet did not create it."
            % (resolved, MARKER)
        )
    if not (resolved / "package.json").is_file():
        raise SystemExit("workspace is not prepared: %s" % resolved)


def clear_workspace(ws: Path, project: str = ".") -> None:
    """Delete a previous workspace, and only ever one this script owns.

    The delete used to run before any check at all, so CONTEXT_DIET_WORKSPACE
    pointed at a real directory removed it.

    Two cases, because they carry different risk. The derived path is built by
    this script out of --project and is always <project>/.claude/context-diet/ab/
    workspace, so the name itself proves whose it is. A path handed in through
    CONTEXT_DIET_WORKSPACE proves nothing, so it has to carry the marker this
    script writes, and is refused without one.
    """
    resolved = ws.resolve()
    if not resolved.exists():
        return
    if resolved == FIXTURE.resolve() or resolved == Path(resolved.anchor):
        raise SystemExit("refusing to delete %s" % resolved)
    if resolved == default_workspace(project).resolve():
        force_rmtree(resolved)
        return
    if not (resolved / MARKER).is_file():
        raise SystemExit(
            "refusing to delete %s: it was chosen with CONTEXT_DIET_WORKSPACE and carries no "
            "%s marker, so context-diet did not create it. Remove it yourself if that really "
            "is the workspace." % (resolved, MARKER)
        )
    force_rmtree(resolved)


def make_room(ws: Path) -> bool:
    """Clear the way for a fresh workspace. True means reuse what is already there.

    A git object store is not always removable: in a sandbox that refuses to
    unlink this tree's .git, the delete stops part way and leaves a directory
    that is no longer the fixture. Renaming does not need that permission, so a
    tree that will not delete is moved aside under a dated name and a clean copy
    is made beside it. Nothing is destroyed, which is also the right default for
    a directory this script may have misjudged.

    Reuse is the last resort, and only for a tree that still holds the fixture
    and its history, because a forced checkout and a clean return that to the
    same baseline recreating it would have produced. A half-cleared tree is not
    a baseline, and measuring one silently is worse than stopping.
    """
    if not ws.exists():
        return False
    try:
        clear_workspace(ws, ws_project_hint(ws))
        return False
    except SystemExit:
        raise
    except OSError as exc:
        aside = ws.with_name("%s.undeletable-%d" % (ws.name, int(time.time())))
        try:
            os.rename(ws, aside)
            sys.stdout.write(
                "Could not delete %s (%s), so it was moved to %s and a clean workspace was "
                "made in its place. Nothing was destroyed; remove it yourself when convenient.\n"
                % (ws, exc.strerror or exc, aside.name))
            return False
        except OSError:
            pass
        if not (ws / "package.json").is_file() or not (ws / ".git").is_dir():
            raise SystemExit(
                "cannot clear %s (%s), cannot move it aside, and it no longer holds the "
                "fixture, so there is no clean baseline to measure from. Remove it by hand "
                "and re-run." % (ws, exc))
        marker = ws / MARKER
        if not marker.is_file():
            marker.write_text(
                "Created by context-diet measure.py. Its presence is what allows this "
                "directory to be rewritten and deleted between trials.\n", encoding="utf-8")
        sys.stdout.write(
            "Reusing the existing workspace at %s: it could not be deleted or moved (%s), and "
            "it still holds the fixture and its git history, so every trial is reset with a "
            "forced checkout and a clean instead.\n" % (ws, exc.strerror or exc))
        return True


def ws_project_hint(ws: Path) -> str:
    """The project a derived workspace belongs to, for clear_workspace's own check."""
    try:
        return str(ws.resolve().parents[3])
    except IndexError:
        return "."


def prepare_workspace(project: str = ".") -> Path:
    """Copy the fixture, make it a git repo, rebuild the broken-test branch.

    The branch that carries the deliberate bug ships as broken-test.patch rather
    than as a nested repository, because a nested repository inside a published
    repo is an unusable gitlink for anyone who clones it.
    """
    ws = workspace(project)
    if make_room(ws):
        reset_workspace(ws, "main")
        return ws
    ws.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE, ws, ignore=shutil.ignore_patterns(".git"))
    # Written before the fence runs, because the fence is what it proves.
    (ws / MARKER).write_text(
        "Created by context-diet measure.py. Its presence is what allows this "
        "directory to be rewritten and deleted between trials.\n", encoding="utf-8")
    guard_workspace(ws)
    ident = ["-c", "user.email=fixture@local", "-c", "user.name=fixture"]
    git_or_die(["init", "-q", "-b", "main"], ws)

    # Prove the workspace is its own repository before writing a single commit
    # into it. `git init` can fail part way, and every later git command then
    # walks up and finds the enclosing project instead. That is not theoretical:
    # with the return codes discarded, `git add -A` and `git commit` ran against
    # the parent repository and committed the whole working tree there under the
    # message "fixture baseline". Nothing was lost, and nothing should ever have
    # to be recovered. An exit code is not enough here, because the fall-through
    # succeeds; only the toplevel says which repository is being written to.
    top = git_or_die(["rev-parse", "--show-toplevel"], ws)
    try:
        same = Path(top).resolve() == ws.resolve()
    except OSError:
        same = False
    if not same:
        raise SystemExit(
            "git init did not make %s its own repository: git reports the enclosing "
            "repository at %s. Refusing to continue, because the next commit would land "
            "there. This usually means the environment would not let git create "
            "%s/.git; choose a writable location with CONTEXT_DIET_WORKSPACE."
            % (ws, top, ws))

    git_or_die(["add", "-A"], ws)
    git_or_die(ident + ["commit", "-q", "-m", "fixture baseline"], ws)
    patch = ws / "broken-test.patch"
    if patch.is_file():
        git_or_die(["checkout", "-q", "-b", "broken-test"], ws)
        git_or_die(["apply", str(patch)], ws)
        git_or_die(ident + ["commit", "-aqm", "introduce the deliberate rounding bug"], ws)
        git_or_die(["checkout", "-q", "main"], ws)
    node_modules = FIXTURE / "node_modules"
    if node_modules.is_dir() and not (ws / "node_modules").exists():
        os.symlink(node_modules, ws / "node_modules")
    return ws


def git(args: list, cwd: Path) -> tuple:
    """(returncode, stdout). The code is returned because ignoring it lost a run.

    `git checkout broken-test` aborts on a dirty tree. The return code was
    discarded, so the next line reset onto broken-test while HEAD was still
    main, and moved main itself onto the buggy commit. Every later trial then
    started from that commit, which made the fix-broken-test task trivially
    true for the rest of the run.
    """
    out = subprocess.run(["git"] + args, cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=60)
    return (out.returncode, out.stdout.decode("utf-8", "replace").strip())


def git_or_die(args: list, cwd: Path) -> str:
    rc, out = git(args, cwd)
    if rc != 0:
        raise SystemExit("git %s failed in %s (exit %d). The workspace is not in a known "
                         "state, so the run stops rather than measuring one."
                         % (" ".join(args), cwd, rc))
    return out


def reset_workspace(ws: Path, branch: str = "main") -> None:
    """Every trial starts identical. Untracked files go, ignored files stay.

    answer.txt is deliberately not ignored, so it is removed between trials. If
    it survived, one arm would pass the explain task on the previous arm's work.
    node_modules is ignored, so the dependency store survives.

    Clean first, then force the checkout, so nothing in the tree can abort it
    and leave the reset pointed at the wrong branch. Then confirm HEAD really
    is the branch asked for before resetting onto it.
    """
    guard_workspace(ws)
    git_or_die(["clean", "-qfdx", "-e", "node_modules"], ws)
    git_or_die(["checkout", "--quiet", "--force", branch], ws)
    head = git_or_die(["rev-parse", "--abbrev-ref", "HEAD"], ws)
    if head != branch:
        raise SystemExit("expected to be on %s after checkout but HEAD is %s; refusing to "
                         "reset from here." % (branch, head))
    git_or_die(["reset", "--" + "hard", "--quiet", branch], ws)


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
        # Did the agent actually execute? A run killed by a usage limit, or one
        # whose output could not be parsed, produces no evidence about the task
        # at all. It is missing data, not a failure, and the difference decides
        # whether a report may draw a conclusion from it.
        "ran": True,
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
    def note(text: str) -> None:
        record["note"] = (record["note"] + " " if record["note"] else "") + text

    def did_not_run(text: str) -> None:
        """Record a run that produced no evidence, and say why."""
        note(text)
        record["ran"] = False

    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
        usage = data.get("usage") or {}
        record["fresh_input"] = int(usage.get("input_tokens") or 0)
        record["cache_creation"] = int(usage.get("cache_creation_input_tokens") or 0)
        record["cache_read"] = int(usage.get("cache_read_input_tokens") or 0)
        record["output"] = int(usage.get("output_tokens") or 0)
        record["num_turns"] = int(data.get("num_turns") or 0)
        # A runner that fails to authenticate returns a well formed object full
        # of zeros. Without this check the run would look like a cheap success.
        if data.get("is_error"):
            did_not_run("agent run failed: %s" % str(data.get("result") or
                                                     data.get("terminal_reason") or "unknown")[:160])
        elif record["fresh_input"] == 0 and record["cache_read"] == 0:
            note("agent reported zero tokens; the token columns carry no information")
    except Exception:
        did_not_run("agent output was not parseable json; token columns are zero, not estimated")

    if not record["ran"]:
        # The check would run against a workspace the agent never touched and
        # report a failure that says nothing about the task. Forty-five runs of
        # a ninety-run batch died on a usage limit and every one recorded a
        # clean-looking `passed=False`, which the report then read as fifteen
        # lost tasks. Leave it empty and let the reader see there is no datum.
        record["passed"] = ""
        return record

    passed, _ms, _tail = run_check(task["check"], ws)
    record["passed"] = passed
    return record


COLUMNS = (
    "ts", "model", "config", "task", "trial", "claude_md_present",
    "fresh_input", "cache_creation", "cache_read", "output",
    "wall_ms", "tool_calls", "file_reads", "num_turns", "ran", "passed", "note",
)


def cell(value) -> str:
    """One TSV cell. Tabs and newlines are flattened, never written through.

    The writer stripped tabs but not newlines, while note() embeds agent output
    that contains them. One such row spanned two lines and both were then
    dropped by every reader, silently.
    """
    text = "" if value is None else str(value)
    for ch in ("\t", "\r\n", "\r", "\n"):
        text = text.replace(ch, " ")
    return text


def append_row(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\t".join(cell(record.get(k)) for k in COLUMNS) + "\n")


def do_run(args) -> int:
    ws = prepare_workspace(args.project)
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
    # The header used to be taken from the first record's keys, while num_turns
    # is only added when the agent's JSON parses. One unparsed trial late in a
    # run then raised KeyError after every paid run had already happened, with
    # nothing written. The columns are fixed, and each row is appended as it is
    # produced, so a crash costs the remaining runs and not the finished ones.
    with results_path.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(COLUMNS) + "\n")
    records = []
    total = len(models) * len(CONFIGS) * len(TASKS) * args.trials
    done = 0

    for model in models:
        for config in CONFIGS:
            for task in TASKS:
                for trial in range(1, args.trials + 1):
                    rec = run_trial(model, config, task, trial, args.dry_run, ws)
                    records.append(rec)
                    append_row(results_path, rec)
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
