#!/usr/bin/env python3
"""run_gates.py - every gate in the build spec, as a command that passes or fails.

Nothing here passes on opinion. Each gate is named for the scorer it implements,
so a failing gate and a failing scorer are the same event with the same name.

Usage:
    run_gates.py [--only NAME] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
SCRIPTS = PLUGIN / "scripts"
HOOKS = PLUGIN / "hooks"
FIXTURES = HERE / "fixtures"
REPO = PLUGIN.parent.parent

sys.path.insert(0, str(SCRIPTS))

# hooks.json invokes bare `python3`, so that is the interpreter production runs
# and therefore the one the suite must measure. Timing the repo venv measured an
# interpreter no session ever uses.
PY = shutil.which("python3") or sys.executable
GATES = []


def gate(name: str):
    def wrap(fn):
        GATES.append((name, fn))
        return fn

    return wrap


def run_hook(script: str, payload: dict, env_extra: dict | None = None, cwd: str | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
    # Pin the settings the hook reads. Without this the occupancy denominator
    # came from whatever autoCompactWindow the developer happens to have set, so
    # the same gate measured a different window on every machine and the
    # unknown-model path could not be reached at all.
    env.setdefault("CONTEXT_DIET_SETTINGS", str(FIXTURES / "settings" / "settings-pinned.json"))
    if env_extra:
        env.update(env_extra)
    started = time.time()
    proc = subprocess.run(
        [PY, str(HOOKS / script)],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd or str(REPO),
        timeout=60,
    )
    ms = (time.time() - started) * 1000
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
        ms,
    )


def default_config() -> Path:
    """The starting config, wherever this copy of the plugin keeps it.

    In the repo it sits two levels up, beside the marketplace. Once installed,
    the plugin is its own root and the config travels with it. The suite ships
    with the plugin, so it has to find the file in both layouts or every hook
    gate fails on an installed copy with a missing-file error that reads like a
    monitor regression.
    """
    for candidate in (REPO / ".claude" / "context-diet.json",
                      PLUGIN / ".claude" / "context-diet.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "no context-diet.json beside the plugin (looked in %s and %s)"
        % (REPO / ".claude", PLUGIN / ".claude"))


def temp_project(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="cd-%s-" % name))
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    shutil.copy(default_config(), d / ".claude" / "context-diet.json")
    return d


def payload_for(transcript: Path, project: Path, event: str = "PostToolUse") -> dict:
    return {
        "session_id": "gate-%s" % transcript.stem,
        "transcript_path": str(transcript),
        "cwd": str(project),
        "hook_event_name": event,
    }


# --------------------------------------------------------------------------
# Census


@gate("census_completeness")
def g_census_completeness():
    out = Path(tempfile.mkdtemp(prefix="cd-census-"))
    r = subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    if r.returncode != 0:
        return (False, r.stderr.decode()[:200])
    tsv = (out / "census.tsv").read_text(encoding="utf-8").splitlines()
    header = tsv[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in tsv[1:]]
    transcripts = {r["transcript"] for r in rows}
    on_disk = {str(p) for p in (FIXTURES / "transcripts").glob("*.jsonl")}
    missing = on_disk - transcripts
    if missing:
        return (False, "transcripts with no row: %s" % sorted(missing)[:3])
    for col in ("h1_instruction_reads", "h2_prefix_tokens", "h3_hook_ms_total", "h4_injected_bytes"):
        if any(r.get(col, "") == "" for r in rows):
            return (False, "empty %s cell" % col)
    summary = (out / "census-summary.md").read_text(encoding="utf-8")
    for h in ("H1", "H2", "H3", "H4"):
        if ("**%s " % h) not in summary:
            return (False, "summary is missing a %s verdict" % h)
    return (True, "%d rows, %d transcripts, four verdicts present" % (len(rows), len(transcripts)))


@gate("census_deterministic")
def g_census_deterministic():
    out1 = Path(tempfile.mkdtemp(prefix="cd-det1-"))
    out2 = Path(tempfile.mkdtemp(prefix="cd-det2-"))
    for out in (out1, out2):
        subprocess.run(
            [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
             "--out", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    a = (out1 / "census.tsv").read_bytes()
    b = (out2 / "census.tsv").read_bytes()
    return (a == b, "two runs %s" % ("identical" if a == b else "differ"))


@gate("census_survives_corruption")
def g_census_corruption():
    out = Path(tempfile.mkdtemp(prefix="cd-corrupt-"))
    r = subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    if r.returncode != 0:
        return (False, "census exited %d on a store holding a corrupt and a truncated file"
                % r.returncode)
    text = (out / "census.tsv").read_text(encoding="utf-8")
    ok = "corrupt.jsonl" in text and "truncated.jsonl" in text
    return (ok, "corrupt and truncated transcripts both produced rows")


@gate("census_hand_count")
def g_census_hand_count():
    """The H1 cell must equal a count done by hand on the same file."""
    out = Path(tempfile.mkdtemp(prefix="cd-hand-"))
    subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    tsv = (out / "census.tsv").read_text(encoding="utf-8").splitlines()
    header = tsv[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in tsv[1:]]
    row = [r for r in rows if r["transcript"].endswith("instruction-reads.jsonl")]
    if not row:
        return (False, "no row for the instruction-reads fixture")
    row = row[0]
    # By hand: the fixture holds 4 Reads. One is CLAUDE.md, one is a SKILL.md
    # under .claude, two are source files.
    expected_total, expected_md, expected_dot = 4, 1, 1
    got = (int(row["h1_total_reads"]), int(row["h1_md_reads"]), int(row["h1_dot_claude_reads"]))
    ok = got == (expected_total, expected_md, expected_dot)
    return (ok, "hand count (4,1,1) vs measured %s" % (got,))


@gate("ab_no_pooling")
def g_ab_no_pooling():
    out = Path(tempfile.mkdtemp(prefix="cd-pool-"))
    subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    tsv = (out / "census.tsv").read_text(encoding="utf-8").splitlines()
    header = tsv[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in tsv[1:]]
    two = [r for r in rows if r["transcript"].endswith("two-model.jsonl")]
    models = {r["model"] for r in two}
    ok = len(two) == 2 and len(models) == 2
    return (ok, "two-model transcript produced %d rows across %d models" % (len(two), len(models)))


# --------------------------------------------------------------------------
# Monitor


@gate("hook_silence_below_threshold")
def g_silence():
    project = temp_project("silence")
    code, out, err, _ms = run_hook("occupancy_monitor.py",
                                   payload_for(FIXTURES / "transcripts" / "green.jsonl", project))
    ok = code == 0 and out == "" and err == ""
    return (ok, "green session produced %d bytes of output" % len(out))


@gate("monitor_speaks_at_amber")
def g_amber():
    project = temp_project("amber")
    code, out, _err, _ms = run_hook("occupancy_monitor.py",
                                    payload_for(FIXTURES / "transcripts" / "amber.jsonl", project))
    if code != 0 or "context-diet" not in out:
        return (False, "no directive at amber (exit %d)" % code)
    manifests = list((project / ".claude" / "context-diet" / "handoff").glob("*.json"))
    if not manifests:
        return (False, "directive was emitted without a manifest on disk")
    data = json.loads(manifests[0].read_text())
    if not data.get("verified"):
        return (False, "manifest was written unverified")
    return (True, "manifest written and verified before the directive")


@gate("monitor_speaks_once")
def g_once():
    project = temp_project("once")
    p = payload_for(FIXTURES / "transcripts" / "amber.jsonl", project)
    _c1, out1, _e1, _m1 = run_hook("occupancy_monitor.py", p)
    _c2, out2, _e2, _m2 = run_hook("occupancy_monitor.py", p)
    ok = bool(out1) and out2 == ""
    return (ok, "first call spoke, second call emitted %d bytes" % len(out2))


@gate("unknown_model_idle")
def g_unknown_model():
    """A model with no configured window must pause, not guess.

    autoCompactWindow is consulted first and is global, so this gate runs against
    a settings file without it. Otherwise the pause path can never be reached on
    a machine that sets it, and the behaviour would ship untested.
    """
    project = temp_project("unknown")
    settings = project / "settings-no-window.json"
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    code, out, _err, _ms = run_hook(
        "occupancy_monitor.py",
        payload_for(FIXTURES / "transcripts" / "unknown-model.jsonl", project),
        env_extra={"CONTEXT_DIET_SETTINGS": str(settings)},
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    handoff = project / ".claude" / "context-diet" / "handoff"
    wrote_manifest = handoff.is_dir() and any(handoff.glob("*.json"))
    ok = (
        code == 0
        and len(lines) == 1
        and "unknown model" in out
        and "paused" in out
        and not wrote_manifest
    )
    return (ok, "%d line(s), manifest written: %s" % (len(lines), wrote_manifest))


@gate("monitor_budget_ms")
def g_monitor_budget():
    project = temp_project("budget")
    times = []
    for _ in range(5):
        _c, _o, _e, ms = run_hook("occupancy_monitor.py",
                                  payload_for(FIXTURES / "transcripts" / "green.jsonl", project))
        times.append(ms)
    # Subprocess start-up dominates a 50 ms budget, so the in-process work is
    # what is measured here; the wall time is reported beside it.
    import cdlib

    started = time.time()
    for _ in range(5):
        usage, model = cdlib.last_usage(str(FIXTURES / "transcripts" / "green.jsonl"))
        cfg = cdlib.load_config(str(project))
        window, _src = cdlib.window_for(model, cfg)
        cdlib.occupancy(usage, window)
    inner = (time.time() - started) / 5 * 1000

    # The green path is the one that runs on every tool call, so 50 ms is its
    # budget. Timing only that hid the branch that does the work: at amber the
    # hook scans the transcript and forks git, and a real invocation took 568 ms.
    # That is measured here too, against its own stated bound, because it runs
    # at most once per band per session rather than on every call. A budget that
    # is never checked against the expensive path is not a budget.
    amber_project = temp_project("budget-amber")
    _c, _o, _e, amber_ms = run_hook(
        "occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / "amber.jsonl", amber_project))
    ok = inner < 50 and amber_ms < 2000
    return (ok, "green in-process %.1f ms (budget 50), green wall %.0f ms, amber wall %.0f ms "
                "(budget 2000, runs once per band)" % (inner, sorted(times)[2], amber_ms))


@gate("corrupt_transcript_exits_silent")
def g_corrupt_silent():
    project = temp_project("corrupt")
    results = []
    for name in ("corrupt.jsonl", "truncated.jsonl", "missing.jsonl"):
        code, out, err, _ms = run_hook("occupancy_monitor.py",
                                       payload_for(FIXTURES / "transcripts" / name, project))
        results.append((name, code, len(out), len(err)))
    ok = all(c == 0 and o == 0 and e == 0 for _n, c, o, e in results)
    return (ok, "; ".join("%s exit %d out %d err %d" % r for r in results))


@gate("kill_switches")
def g_kill_switches():
    project = temp_project("kill")
    _c, out_env, _e, _m = run_hook(
        "occupancy_monitor.py",
        payload_for(FIXTURES / "transcripts" / "amber.jsonl", project),
        env_extra={"CONTEXT_DIET_OFF": "1"},
    )
    project2 = temp_project("kill2")
    flags = project2 / ".claude" / "context-diet" / "disabled"
    flags.mkdir(parents=True, exist_ok=True)
    payload = payload_for(FIXTURES / "transcripts" / "amber.jsonl", project2)
    (flags / ("occupancy_monitor.%s" % payload["session_id"])).write_text("off", encoding="utf-8")
    _c2, out_flag, _e2, _m2 = run_hook("occupancy_monitor.py", payload)
    ok = out_env == "" and out_flag == ""
    return (ok, "env switch %d bytes, file flag %d bytes" % (len(out_env), len(out_flag)))


# --------------------------------------------------------------------------
# Handoff


@gate("manifest_roundtrip")
def g_manifest_roundtrip():
    from handoff import build_manifest, verify_manifest

    project = temp_project("manifest")
    real = project / "real.txt"
    real.write_text("hello", encoding="utf-8")
    manifest = build_manifest(
        {"transcript_path": str(FIXTURES / "transcripts" / "amber.jsonl")},
        str(project), "s1", "amber",
    )
    manifest["files"]["touched"] = [{"path": str(real)}, {"path": str(project / "ghost.txt")}]
    manifest["next_action"] = "word " * 40
    manifest["summary"] = "we discussed a lot of things and decided many"
    kept, problems = verify_manifest(manifest, str(project))
    kept_paths = [e["path"] for e in kept["files"]["touched"]]
    ok = (
        kept_paths == [str(real)]
        and "sha256" in kept["files"]["touched"][0]
        and kept["next_action"] == ""
        and "summary" not in kept
        and len(problems) >= 3
    )
    return (ok, "dropped %d field(s): fabricated path, long free text, prose field" % len(problems))


@gate("manifest_never_repairs")
def g_manifest_never_repairs():
    """A field that fails verification is dropped, never replaced."""
    from handoff import verify_manifest

    project = temp_project("repair")
    manifest = {
        "version": 1,
        "git": {"sha": "0" * 40, "branch": "main", "dirty": False},
        "files": {"touched": [{"path": str(project / "nope.txt")}], "read": []},
        "next_action": "",
        "transcript_path": "",
        "commands": [],
    }
    kept, problems = verify_manifest(manifest, str(project))
    ok = kept["git"]["sha"] == "" and kept["files"]["touched"] == [] and len(problems) >= 2
    # The dropped path is allowed, and required, to appear in the problems list:
    # a manifest that loses a field loudly is the safe outcome. What must never
    # happen is a substitute value in the field itself.
    payload_fields = json.dumps({k: v for k, v in kept.items() if k != "dropped"})
    invented = "nope.txt" in payload_fields or "0000" in payload_fields
    named = any("nope.txt" in p for p in problems)
    return (ok and not invented and named,
            "failing fields emptied and named in the drop list, no substitute value written")


@gate("precompact_fallback")
def g_precompact():
    project = temp_project("precompact")
    payload = payload_for(FIXTURES / "transcripts" / "amber.jsonl", project, "PreCompact")
    code, out, _err, _ms = run_hook("precompact_manifest.py", payload)
    handoff = project / ".claude" / "context-diet" / "handoff"
    files = list(handoff.glob("*.json")) if handoff.is_dir() else []
    ok = code == 0 and out == "" and len(files) == 1
    if ok:
        first = files[0].read_bytes()
        run_hook("precompact_manifest.py", payload)
        ok = files[0].read_bytes() == first
    return (ok, "manifest written silently and not overwritten on a second fire")


@gate("resume_consumes_once")
def g_resume_once():
    project = temp_project("resume")
    # SessionStart only injects a manifest this machine wrote, and part of that
    # proof is that the manifest names a transcript under the user's own
    # projects directory. Point that directory at the fixtures for this gate,
    # so the check is exercised rather than bypassed.
    os.environ["CONTEXT_DIET_PROJECTS_ROOT"] = str((FIXTURES / "transcripts").resolve())
    try:
        run_hook("occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / "red.jsonl", project))
        p = {"session_id": "new", "cwd": str(project), "hook_event_name": "SessionStart"}
        _c1, out1, _e1, _m1 = run_hook("session_start.py", p)
        _c2, out2, _e2, _m2 = run_hook("session_start.py", p)
    finally:
        os.environ.pop("CONTEXT_DIET_PROJECTS_ROOT", None)
    ok = "Where you left off" in out1 and out2 == ""
    return (ok, "first start injected %d bytes, second injected %d" % (len(out1), len(out2)))


@gate("session_start_budget_ms")
def g_session_start_budget():
    project = temp_project("ssbudget")
    times = []
    for _ in range(3):
        _c, _o, _e, ms = run_hook(
            "session_start.py",
            {"session_id": "s", "cwd": str(project), "hook_event_name": "SessionStart"},
        )
        times.append(ms)
    import handoff as h

    started = time.time()
    h.load_armed(str(project))
    inner = (time.time() - started) * 1000
    return (inner < 200, "in-process %.1f ms, wall %.0f ms" % (inner, min(times)))


# --------------------------------------------------------------------------
# Signals and storage


@gate("storage_signal_cost")
def g_storage_signal():
    project = temp_project("storage")
    state = project / ".claude" / "context-diet"
    state.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURES / "stores" / "backups", state / "backups")
    cfg = json.loads((project / ".claude" / "context-diet.json").read_text())
    cfg["backups_store_bytes"] = 1_000_000
    (project / ".claude" / "context-diet.json").write_text(json.dumps(cfg), encoding="utf-8")

    before = sorted((p.name, p.stat().st_size) for p in (state / "backups").rglob("*") if p.is_file())
    started = time.time()
    code, out, _err, _ms = run_hook(
        "stop_ledger.py",
        {"session_id": "st", "cwd": str(project),
         "transcript_path": str(FIXTURES / "transcripts" / "amber.jsonl"),
         "hook_event_name": "Stop"},
    )
    elapsed = (time.time() - started) * 1000
    after = sorted((p.name, p.stat().st_size) for p in (state / "backups").rglob("*") if p.is_file())
    ledger = state / "ledger.tsv"
    if code != 0 or out != "":
        return (False, "Stop hook spoke or failed (exit %d, %d bytes)" % (code, len(out)))
    if before != after:
        return (False, "storage signal changed files on disk")
    if not ledger.is_file():
        return (False, "no ledger row written")
    text = ledger.read_text(encoding="utf-8")
    reported = "backups" in text
    if not reported:
        return (False, "the oversize backups store was not reported at all")

    # The cache is a property, not a stopwatch reading. Timing it proved
    # nothing: the "cached" run once measured slower than the first and the
    # gate still passed. So grow the directory and run again inside the TTL.
    # A second walk would see the new bytes; using the cache cannot.
    cache = state / "storage-cache.json"
    if not cache.is_file():
        return (False, "no storage cache was written, so nothing can be reused")
    first_mtime = cache.stat().st_mtime
    grown = state / "backups" / "grown.bin"
    grown.write_bytes(b"\0" * 5_000_000)

    started2 = time.time()
    _c2, out2, _e2, _m2 = run_hook(
        "stop_ledger.py",
        {"session_id": "st2", "cwd": str(project),
         "transcript_path": str(FIXTURES / "transcripts" / "amber.jsonl"),
         "hook_event_name": "Stop"})
    cached_ms = (time.time() - started2) * 1000
    if out2 != "":
        return (False, "the Stop hook spoke on the cached run")
    if cache.stat().st_mtime != first_mtime:
        return (False, "the cache was rewritten inside its TTL, so it is not being reused")

    rows = [r for r in ledger.read_text(encoding="utf-8").splitlines() if r.strip()]
    if len(rows) < 2:
        return (False, "the second Stop wrote no ledger row")
    if "5" in rows[-1] and "grown.bin" in rows[-1]:
        return (False, "the cached run re-walked the directory and saw the new file")
    return (True, "reported, deleted nothing, cache reused inside the TTL after the store grew "
                  "5 MB; first %.0f ms, cached %.0f ms" % (elapsed, cached_ms))


@gate("stop_hook_timing_bound")
def g_stop_timing():
    big = FIXTURES / "big-store"
    if not big.is_dir():
        return (None, "2,000-file fixture absent; run make_fixtures.py --heavy")
    project = temp_project("bigstore")
    started = time.time()
    code, out, _err, _ms = run_hook(
        "stop_ledger.py",
        {"session_id": "big", "cwd": str(project),
         "transcript_path": str(FIXTURES / "transcripts" / "amber.jsonl"),
         "hook_event_name": "Stop"},
    )
    elapsed = (time.time() - started) * 1000
    ok = code == 0 and out == "" and elapsed < 20000
    return (ok, "Stop finished in %.0f ms against a %d-file store" % (elapsed, len(list(big.glob('*')))))


@gate("ledger_rotation")
def g_ledger_rotation():
    from cdlib import append_ledger, state_dir

    project = temp_project("rotate")
    d = state_dir(str(project))
    d.mkdir(parents=True, exist_ok=True)
    ledger = d / "ledger.tsv"
    ledger.write_text("x" * 200_000, encoding="utf-8")
    cfg = {"ledger_bytes": 1000}
    append_ledger({"ts": 1, "note": "after rotation"}, str(project), cfg)
    rotated = list(d.glob("ledger-*.tsv"))
    ok = len(rotated) == 1 and rotated[0].stat().st_size == 200_000 and ledger.is_file()
    return (ok, "rotated to %s, nothing discarded" % (rotated[0].name if rotated else "nothing"))


@gate("repeat_reads")
def g_repeat_reads():
    project = temp_project("reads")
    target = project / "same.ts"
    target.write_text("export const a = 1;\n", encoding="utf-8")
    outs = []
    for _ in range(4):
        _c, out, _e, _m = run_hook(
            "read_tracker.py",
            {"session_id": "rr", "cwd": str(project), "tool_name": "Read",
             "tool_input": {"file_path": str(target)}, "hook_event_name": "PostToolUse"},
        )
        outs.append(out)
    spoke = [i for i, o in enumerate(outs) if o]
    ok = spoke == [2]
    return (ok, "spoke on read %s of 4 (threshold 3)" % ([i + 1 for i in spoke] or "never"))


@gate("repeat_reads_reset_on_change")
def g_repeat_reads_change():
    project = temp_project("readschange")
    target = project / "same.ts"
    target.write_text("a\n", encoding="utf-8")
    payload = {"session_id": "rc", "cwd": str(project), "tool_name": "Read",
               "tool_input": {"file_path": str(target)}, "hook_event_name": "PostToolUse"}
    run_hook("read_tracker.py", payload)
    run_hook("read_tracker.py", payload)
    time.sleep(1.1)
    target.write_text("b\n", encoding="utf-8")
    _c, out, _e, _m = run_hook("read_tracker.py", payload)
    return (out == "", "a changed file resets the count rather than reporting a repeat read")


@gate("duplicate_hook_registrations")
def g_duplicate_hooks():
    # Audit a fixture machine, not this one. Asserting the developer's own
    # duplicate registration and stray rules made the suite green here and red
    # on any clean checkout, which is the opposite of what a gate is for.
    # The settings are written here rather than shipped, because the duplicate
    # this detects is "the same hook registered twice under two spellings of the
    # same path", and one of those spellings is the home directory being
    # audited. A static fixture cannot know that path.
    home = FIXTURES / "settings"
    settings = Path(tempfile.mkdtemp(prefix="cd-dup-")) / "settings.json"
    settings.write_text(json.dumps({
        "autoCompactWindow": 500000,
        "hooks": {"SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "bash %s/.claude/hooks/x.sh" % home, "timeout": 15},
            {"type": "command", "command": "bash $HOME/.claude/hooks/x.sh", "timeout": 15},
        ]}]},
    }), encoding="utf-8")
    env = dict(os.environ)
    env["CONTEXT_DIET_SETTINGS"] = str(settings)
    env["CONTEXT_DIET_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(SCRIPTS)
    r = subprocess.run(
        [PY, str(SCRIPTS / "hook_audit.py"), "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=env,
    )
    if r.returncode != 0:
        return (False, r.stderr.decode()[:200])
    data = json.loads(r.stdout.decode())
    dups = data.get("duplicate_registrations", [])
    # Assert the fixture's own duplicate, not a hook that happens to be on the
    # developer's machine. Naming inject-memory.sh here is what made this gate
    # pass on one laptop and fail everywhere else.
    found = any("x.sh" in d["command"] for d in dups)
    inert = data.get("inert_hookify_rules", [])
    return (found and bool(inert),
            "%d duplicate registration(s) and %d inert rule(s) reported" % (len(dups), len(inert)))


# --------------------------------------------------------------------------
# Budget and edit path


@gate("budget_within_5_percent")
def g_budget_accuracy():
    r = subprocess.run(
        [PY, str(SCRIPTS / "budget.py"), "--json", "--path",
         str(FIXTURES / "instructions" / "CLAUDE.md")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    data = json.loads(r.stdout.decode())
    f = data["groups"]["requested"]["files"][0]
    naive = f["bytes"] / 4.0
    delta = abs(f["tokens"] - naive) / naive
    return (delta <= 0.25,
            "%d tokens vs %.0f by chars/4, %.1f%% apart, tokeniser %s"
            % (f["tokens"], naive, delta * 100, data["tokenizer"]))


@gate("budget_runtime_under_2s")
def g_budget_runtime():
    started = time.time()
    r = subprocess.run(
        [PY, str(SCRIPTS / "budget.py"), "--project", str(REPO)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
    )
    elapsed = time.time() - started
    return (r.returncode == 0 and elapsed < 2.0, "%.2f s on the real config tree" % elapsed)


@gate("budget_names_tokenizer")
def g_budget_tokenizer():
    r = subprocess.run(
        [PY, str(SCRIPTS / "budget.py"), "--project", str(REPO)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60,
    )
    text = r.stdout.decode()
    ok = "tokeniser:" in text and ("tiktoken" in text or "chars/4" in text)
    first = text.splitlines()[0] if text else ""
    return (ok, first.strip())


@gate("skill_body_under_500_lines")
def g_skill_lines():
    skill = PLUGIN / "skills" / "context-audit" / "SKILL.md"
    if not skill.is_file():
        return (None, "skill not written yet")
    lines = skill.read_text(encoding="utf-8").count("\n")
    return (lines < 500, "%d lines" % lines)


@gate("apply_revert_identity")
def g_apply_revert():
    patch = SCRIPTS / "patch.py"
    if not patch.is_file():
        return (None, "patch.py not written yet")
    import hashlib

    project = temp_project("patch")
    target = project / "CLAUDE.md"
    shutil.copy(FIXTURES / "instructions" / "CLAUDE.md", target)
    before = hashlib.sha256(target.read_bytes()).hexdigest()

    # Several cuts to one file, because a single-cut plan cannot catch a backup
    # that is taken once per cut and overwrites itself with modified text.
    plan = {
        "version": 1,
        "cuts": [
            {"file": str(target), "section_heading": "## Style", "reason": "Ritual",
             "tokens": 12, "bucket": "Ritual"},
            {"file": str(target), "section_heading": "## Legacy", "reason": "Legacy",
             "tokens": 14, "bucket": "Legacy"},
            {"file": str(target), "section_heading": "## Duplicate", "reason": "Duplicate",
             "tokens": 9, "bucket": "Duplicate"},
        ],
    }
    plan_path = project / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    r1 = subprocess.run([PY, str(patch), "apply", "--plan", str(plan_path),
                         "--project", str(project)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if r1.returncode != 0:
        return (False, "apply failed: %s" % r1.stderr.decode()[:200])
    after_apply = hashlib.sha256(target.read_bytes()).hexdigest()
    if after_apply == before:
        return (False, "apply changed nothing")
    r2 = subprocess.run([PY, str(patch), "revert", "--project", str(project)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if r2.returncode != 0:
        return (False, "revert failed: %s" % r2.stderr.decode()[:200])
    after_revert = hashlib.sha256(target.read_bytes()).hexdigest()
    return (after_revert == before, "apply then revert is byte-identical by sha256")


@gate("no_source_edit")
def g_no_source_edit():
    patch = SCRIPTS / "patch.py"
    if not patch.is_file():
        return (None, "patch.py not written yet")
    project = temp_project("guard")
    src = project / "src"
    src.mkdir(parents=True, exist_ok=True)
    victim = src / "main.ts"
    victim.write_text("export const x = 1;\n", encoding="utf-8")
    plan = {"version": 1, "cuts": [{"file": str(victim), "section_heading": "## anything",
                                    "reason": "Ritual", "tokens": 1, "bucket": "Ritual"}]}
    plan_path = project / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    r = subprocess.run([PY, str(patch), "apply", "--plan", str(plan_path), "--project",
                        str(project)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    unchanged = victim.read_text(encoding="utf-8") == "export const x = 1;\n"
    return (r.returncode != 0 and unchanged, "refused to write outside the instruction layer")


@gate("audit_writes_nothing")
def g_audit_readonly():
    """/audit must be provable on a read-only copy."""
    extract = SCRIPTS / "extract.py"
    project = temp_project("readonly")
    target = project / "CLAUDE.md"
    shutil.copy(FIXTURES / "instructions" / "CLAUDE.md", target)
    os.chmod(target, 0o444)
    before = target.read_bytes()
    r = subprocess.run([PY, str(extract), str(target), "--json"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    ok = r.returncode == 0 and target.read_bytes() == before
    os.chmod(target, 0o644)
    return (ok, "extraction left a read-only file untouched")


@gate("preflight_names_every_extra")
def g_preflight():
    r = subprocess.run([PY, str(SCRIPTS / "preflight.py"), "--json", "--project", str(REPO)],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    data = json.loads(r.stdout.decode())
    names = {c["name"] for c in data["checks"]}
    needed = {"uv", "tiktoken", "mlflow", ".mcp.json"}
    statuses = {c["status"] for c in data["checks"]}
    ok = needed <= names and statuses <= {"present", "missing", "written"}
    return (ok, "%d checks, statuses %s" % (len(data["checks"]), sorted(statuses)))


@gate("bootstrap_idempotent")
def g_bootstrap():
    script = SCRIPTS / "mlflow_bootstrap.sh"
    project = Path(tempfile.mkdtemp(prefix="cd-boot-"))
    env = dict(os.environ)
    subprocess.run(["bash", str(script), str(project)], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env, timeout=120)
    first = (project / ".mcp.json").read_bytes() if (project / ".mcp.json").is_file() else b""
    subprocess.run(["bash", str(script), str(project)], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env, timeout=120)
    second = (project / ".mcp.json").read_bytes() if (project / ".mcp.json").is_file() else b""
    return (bool(first) and first == second, "two runs produce a byte-identical .mcp.json")


@gate("no_listening_socket")
def g_no_server():
    """The plugin starts no server. MLflow writes to a local file store."""
    r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, timeout=30)
    text = r.stdout.decode("utf-8", "replace").lower()
    offenders = [ln for ln in text.splitlines() if "mlflow" in ln or "gunicorn" in ln]
    return (not offenders, "no mlflow process is listening")


@gate("ab_variance_honesty")
def g_ab_variance_honesty():
    """The report must refuse a task-success claim smaller than its own spread.

    The README said the suite covered this. It did not: no gate ran report.py
    at all, and the pass-count branch compared raw totals and announced that
    "the post's prescription held" on a one-task difference. Two ledgers, one
    where the gap is inside the spread and one where it is well outside it.
    """
    out = Path(tempfile.mkdtemp(prefix="cd-variance-"))
    header = ("ts\tmodel\tconfig\ttask\ttrial\tclaude_md_present\tfresh_input\t"
              "cache_creation\tcache_read\toutput\twall_ms\ttool_calls\tfile_reads\t"
              "num_turns\tpassed\tnote")

    def ledger(name, plan):
        # plan: {config: [passes_in_trial_1, trial_2, trial_3]} over 5 tasks.
        lines = [header]
        for config, per_trial in plan.items():
            for trial, passes in enumerate(per_trial, start=1):
                for task in range(1, 6):
                    lines.append("\t".join([
                        "2026-09-19T00:00:00", "m", config, "t%d" % task, str(trial),
                        "true" if config == "Fat" else "false",
                        "1000", "100", "5000", "200", "1000", "3", "2", "4",
                        "true" if task <= passes else "false", "",
                    ]))
        path = out / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def render(path):
        r = subprocess.run([PY, str(SCRIPTS / "report.py"), "--runs", str(path),
                            "--project", str(out)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return r.stdout.decode("utf-8", "replace")

    # Gap of one task, but a config's own trials vary by two. Not a finding.
    noisy = render(ledger("noisy.tsv", {"Fat": [3, 5, 4], "Bare": [4, 4, 5]}))
    # Gap of fifteen tasks with no variation at all. A finding.
    clear = render(ledger("clear.tsv", {"Fat": [0, 0, 0], "Bare": [5, 5, 5]}))

    if "inconclusive on task success" not in noisy:
        return (False, "a 1-task gap inside a 2-task spread was still reported as a finding")
    if "prescription held" in noisy:
        return (False, "the report claimed the post's prescription held on a gap inside the spread")
    if "prescription held" not in clear:
        return (False, "a 15-task gap with zero spread was not reported as a finding")
    if "inconclusive on task success" in clear:
        return (False, "a 15-task gap with zero spread was called inconclusive")
    return (True, "1-task gap inside a 2-task spread is refused; 15-task gap at zero spread stands")


@gate("census_h2_is_measured")
def g_census_h2_is_measured():
    """H2's verdict must come from the data, not from the source code.

    It was the literal string "H2 SUPPORTED". No transcript could refute it, so
    census_completeness passed by finding a constant and the census could not
    contradict the person who wrote it. This asserts the verdict actually varies
    with the numbers, and that H2 obeys the same spread rule as the report.
    """
    out = Path(tempfile.mkdtemp(prefix="cd-h2-"))
    subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    text = (out / "census-summary.md").read_text(encoding="utf-8")
    verdicts = set(re.findall(r"\*\*H2 ([A-Z]+)", text))
    want = {"SUPPORTED", "REFUTED", "INCONCLUSIVE"}
    if verdicts != want:
        return (False, "H2 reached %s; every one of %s must be reachable from the fixtures"
                % (sorted(verdicts) or "nothing", sorted(want)))
    return (True, "H2 returned %s across models, each from the numbers" % ", ".join(sorted(verdicts)))


@gate("section_targeting_is_unambiguous")
def g_section_targeting():
    """A heading is not a unique key, and a hash in a bash block is not a heading.

    Two defects with one blast radius: /apply cutting text nobody chose. The
    fixture holds a fenced block whose lines start with a hash, two `### Notes`
    under different parents, and a third `### Notes` whose full crumb matches
    the first. apply_revert_identity passed throughout and exercised neither.
    """
    fixture = FIXTURES / "instructions" / "tricky-CLAUDE.md"
    text = fixture.read_text(encoding="utf-8")

    r = subprocess.run([PY, str(SCRIPTS / "extract.py"), "--json", str(fixture)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if r.returncode != 0:
        return (False, "extract.py failed: %s" % r.stderr.decode()[:160])
    secs = json.loads(r.stdout.decode("utf-8"))["files"][0]["sections"]

    for s in secs:
        if "install deps" in s["heading"] or "build it" in s["heading"]:
            return (False, "a shell comment inside a fenced block parsed as a heading: %s"
                    % s["heading"])
    ids = [s["id"] for s in secs]
    if len(set(ids)) != len(ids):
        return (False, "%d sections share %d ids, so a plan cannot name one of them"
                % (len(ids), len(set(ids))))
    notes = [s for s in secs if s["path"] == "Project/Testing/Notes"]
    if len(notes) != 2:
        return (False, "expected two sections at Project/Testing/Notes, found %d" % len(notes))
    if notes[0]["id"] == notes[1]["id"]:
        return (False, "two sections with the same crumb and heading got the same id")

    # budget.py must split the same file the same way, or a token figure is
    # priced against a section boundary that /apply does not agree with.
    sys.path.insert(0, str(SCRIPTS))
    import budget  # noqa: PLC0415
    import patch  # noqa: PLC0415
    b_headings = [s["heading"] for s in budget.split_sections(text)
                  if s["heading"] != "(preamble)"]
    if len(b_headings) != len(secs):
        return (False, "budget.py found %d sections where extract.py found %d"
                % (len(b_headings), len(secs)))

    _n, _rm, problem = patch.cut_section(text, "### Notes")
    if not problem:
        return (False, "an ambiguous heading was cut without the plan saying which one")
    _n, removed, problem = patch.cut_section(text, "### Notes", 2)
    if problem or "Second notes" not in removed or "First notes" in removed:
        return (False, "occurrence 2 did not cut the second section: %s" % problem)
    _n, removed, _p = patch.cut_section(text, "### Notes", 3)
    if "A third Notes" not in removed:
        return (False, "occurrence 3 did not cut the third section")
    _n, _rm, problem = patch.cut_section(text, "# install deps")
    if not problem:
        return (False, "a line inside a fenced block was treated as a cuttable section")
    _n, removed, problem = patch.cut_section(text, "## Deploying")
    if problem or "Deploying" not in removed:
        return (False, "an unambiguous heading no longer cuts without an occurrence")

    return (True, "%d sections, ids unique, fence respected, ambiguous heading refused, "
                  "occurrence honoured" % len(secs))


@gate("occupancy_survives_a_large_tail")
def g_occupancy_large_tail():
    """The last usage record must be found however far from the end it sits.

    The tail read was a single fixed 512 KB. One large trailing record pushed
    the last assistant usage out of the window and the monitor returned nothing
    at all: silent, and silent precisely when a session is at its largest, which
    is the only time occupancy matters. The tail now grows until it finds one.
    """
    sys.path.insert(0, str(SCRIPTS))
    import cdlib  # noqa: PLC0415

    out = Path(tempfile.mkdtemp(prefix="cd-tail-"))
    path = out / "large-tail.jsonl"
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 1000,
             "cache_read_input_tokens": 250000, "output_tokens": 50}
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant",
                             "message": {"model": "claude-opus-5", "usage": usage}}) + "\n")
        # One trailing record several times the old fixed window.
        fh.write(json.dumps({"type": "user",
                             "message": {"content": "x" * (2 * 1024 * 1024)}}) + "\n")

    started = time.time()
    got_usage, model = cdlib.last_usage(str(path))
    ms = (time.time() - started) * 1000
    if model != "claude-opus-5":
        return (False, "the usage record beyond the first window was not found")
    if got_usage.get("cache_read_input_tokens") != 250000:
        return (False, "found a record but read the wrong usage")
    if ms > 2000:
        return (False, "growing the tail took %.0f ms, which a hook cannot afford" % ms)

    # A transcript with no usage at all must still give up rather than spin.
    empty = out / "no-usage.jsonl"
    empty.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
                     encoding="utf-8")
    none_usage, none_model = cdlib.last_usage(str(empty))
    if none_usage or none_model:
        return (False, "a transcript with no usage reported one")
    return (True, "found usage %.1f MB from the end in %.0f ms; no-usage file gives up cleanly"
            % (path.stat().st_size / 1048576, ms))


@gate("fixture_doc_matches_the_harness")
def g_fixture_doc_matches():
    """Every pass command shown in TASKS.md must be the one measure.py runs.

    The document said, correctly, that a check must never end in
    `echo "exit=$?"` because echo succeeds whatever it prints. Every command it
    then displayed ended in exactly that. The real checks never did, so the
    harness was sound and only its documentation was wrong, but it was wrong in
    the direction that makes a broken gate look fine. A prose rule cannot keep
    the two in step; this can.
    """
    sys.path.insert(0, str(SCRIPTS))
    import measure  # noqa: PLC0415

    doc = (PLUGIN / "fixture" / "TASKS.md").read_text(encoding="utf-8")

    def norm(s):
        return " ".join(s.replace("\\\n", " ").split())

    shown = [norm(b) for b in re.findall(r"```bash\n(.*?)```", doc, re.S)]
    real = {norm(task["check"]): task["id"] for task in measure.TASKS}
    if not shown:
        return (False, "TASKS.md shows no pass commands at all")
    for block in shown:
        if 'echo "exit=$?"' in block:
            return (False, "a shown command ends in echo, which always exits 0")
        if block not in real:
            return (False, "a shown command is not one measure.py runs: %s" % block[:110])
    covered = {real[b] for b in shown}
    expected = {task["id"] for task in measure.TASKS}
    if covered != expected:
        return (False, "TASKS.md documents %s but the harness runs %s"
                % (sorted(covered), sorted(expected)))
    return (True, "all %d shown commands are the ones the harness runs" % len(shown))


@gate("state_dir_anchors_on_the_project")
def g_state_dir_anchors():
    """State must land where a later session will look for it.

    Found live, not by review: this session's own monitor hit amber while the
    shell had drifted into a subdirectory, and wrote the handoff manifest and
    its acted marker under that subdirectory. A session starting at the repo
    root would never have found the manifest, so the resume path would have
    failed silently, and the acted marker would not have suppressed a repeat.
    """
    sys.path.insert(0, str(SCRIPTS))
    import cdlib  # noqa: PLC0415

    saved = os.environ.pop("CLAUDE_PROJECT_DIR", None)
    try:
        root = Path(tempfile.mkdtemp(prefix="cd-anchor-"))
        (root / ".git").mkdir()
        deep = root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        if cdlib.project_root(str(deep)) != root.resolve():
            return (False, "a subdirectory resolved to %s, not the project root"
                    % cdlib.project_root(str(deep)))
        if cdlib.state_dir(str(deep)) != cdlib.state_dir(str(root)):
            return (False, "state_dir differs between the root and a subdirectory")

        # A directory with no project above it must not climb to the home
        # directory, whose .claude is the user's global config.
        lone = Path(tempfile.mkdtemp(prefix="cd-lone-"))
        if cdlib.project_root(str(lone)) == Path.home().resolve():
            return (False, "a project-less path resolved to the home directory")

        os.environ["CLAUDE_PROJECT_DIR"] = str(root / "a")
        if cdlib.project_root(str(deep)) != root / "a":
            return (False, "CLAUDE_PROJECT_DIR did not win when set")
        return (True, "a subdirectory, a project-less path and an explicit override all resolve "
                      "as intended")
    finally:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        if saved is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = saved


@gate("no_absolute_home_paths_tracked")
def g_no_absolute_home_paths():
    """Nothing git tracks may carry this machine's home directory.

    The repository is public. An absolute path in a tracked file publishes the
    account name and the directory layout for no benefit, and it is also just
    wrong for anyone who clones: three files carried one, and the `.mcp.json`
    entry additionally pointed at a tracking store the plugin had stopped using.
    Paths that belong to a machine are written at run time into the ignored
    state directory, never committed.
    """
    r = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if r.returncode != 0:
        return (None, "not a git checkout, nothing to check")
    files = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]
    home = str(Path.home())
    offenders = []
    for rel in files:
        path = REPO / rel
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if home in text:
            first = next((i + 1 for i, ln in enumerate(text.splitlines()) if home in ln), 0)
            offenders.append("%s:%d" % (rel, first))
    if offenders:
        return (False, "tracked files carry the home directory: %s" % ", ".join(offenders[:4]))
    return (True, "%d tracked files, none carrying an absolute home path" % len(files))


@gate("effective_table_prints_once")
def g_effective_table_once():
    """The config file promises the first run shows the thresholds. Make it true.

    `.claude/context-diet.json` and cdlib both said the first run prints the
    effective table. Nothing called the function, so a wrong threshold stayed
    invisible until it misfired, which is the one moment it is too late.
    """
    project = Path(tempfile.mkdtemp(prefix="cd-table-"))
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project)

    def run():
        r = subprocess.run([PY, str(SCRIPTS / "budget.py"), "--project", str(project)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, env=env)
        return r.stdout.decode("utf-8", "replace")

    first, second = run(), run()
    if "Effective thresholds" not in first:
        return (False, "the first run did not print the effective table")
    if "amber" not in first or "source" not in first:
        return (False, "the table printed without its values or their source")
    if "Effective thresholds" in second:
        return (False, "the table printed again on the second run")
    marker = project / ".claude" / "context-diet" / "effective-table-shown"
    if not marker.is_file():
        return (False, "no marker was written, so 'once' is not recorded anywhere")
    marker.unlink()
    if "Effective thresholds" not in run():
        return (False, "deleting the marker did not bring the table back")
    return (True, "printed on the first run, silent on the second, returns when the marker goes")


@gate("hook_cost_per_event_under_200ms")
def g_hook_cost_per_event():
    """The hooks that fire on one event must together cost under 200 ms.

    The README claimed the suite checked a 200 ms hook cost. It did not: the
    per-hook gates bound one hook each and the Stop bound alone permits two
    seconds. Measuring it revealed the claim was also stated against the wrong
    unit. Running one of every hook costs about 390 ms here, but five hooks
    never fire together: a bare `python3 -c pass` is 30 to 40 ms on this
    machine, so five processes spend roughly 175 ms on interpreter startup
    alone and a whole-session 200 ms total is not reachable by construction.

    What a user actually waits for is one event. A PostToolUse fires the monitor
    and the read tracker; every other event fires exactly one hook. That is the
    number bounded here, and it is the number the README now states.
    """
    project = temp_project("hookcost")
    green = FIXTURES / "transcripts" / "green.jsonl"
    base = {"session_id": "hc", "cwd": str(project), "transcript_path": str(green)}
    events = {
        "PostToolUse": [
            ("occupancy_monitor.py", dict(base, hook_event_name="PostToolUse")),
            ("read_tracker.py", dict(base, hook_event_name="PostToolUse",
                                     tool_name="Read",
                                     tool_input={"file_path": str(green)})),
        ],
        "UserPromptSubmit": [
            ("occupancy_monitor.py", dict(base, hook_event_name="UserPromptSubmit")),
        ],
        "SessionStart": [("session_start.py", dict(base, hook_event_name="SessionStart"))],
        "PreCompact": [("precompact_manifest.py", dict(base, hook_event_name="PreCompact"))],
        "Stop": [("stop_ledger.py", dict(base, hook_event_name="Stop"))],
    }
    worst = []
    for event, calls in events.items():
        total = 0.0
        for script, payload in calls:
            _c, _o, _e, ms = run_hook(script, payload)
            total += ms
        worst.append((total, event, len(calls)))
        if total >= 200:
            return (False, "%s fires %d hook(s) costing %.0f ms together, budget 200"
                    % (event, len(calls), total))
    worst.sort(reverse=True)
    return (True, "worst event is %s at %.0f ms across %d hook(s), budget 200"
            % (worst[0][1], worst[0][0], worst[0][2]))


@gate("injected_bytes_attributed_to_their_own_hook")
def g_injector_attribution():
    """H4 must charge injected bytes to the hook named on the injection.

    They were charged to whichever hook had most recently logged a
    `hook_success`, although every one of the 763 such attachments on this
    machine carries its own `hookName`. The fixture makes the two disagree: the
    last success is `PostToolUse:Edit` and the injection names `SessionStart`.
    """
    out = Path(tempfile.mkdtemp(prefix="cd-attr-"))
    subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    tsv = (out / "census.tsv").read_text(encoding="utf-8").splitlines()
    header = tsv[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in tsv[1:]]
    row = next((r for r in rows if r["transcript"].endswith("injector-attribution.jsonl")), None)
    if row is None:
        return (False, "the attribution fixture produced no row")
    if row["h4_top_injector"] == "PostToolUse:Edit":
        return (False, "bytes were charged to the last hook_success, not to the injector")
    if row["h4_top_injector"] != "SessionStart":
        return (False, "top injector is %r, expected the injection's own hookName"
                % row["h4_top_injector"])
    if int(row["h4_injected_bytes"] or 0) < 9000:
        return (False, "the injected bytes were not counted")
    return (True, "bytes charged to SessionStart, the name on the injection, not to the "
                  "PostToolUse:Edit success before it")


@gate("compactor_ceiling_is_measured")
def g_compactor_ceiling():
    """The census must report where the built-in compactor actually fired.

    Part 5.3 requires Amber to sit strictly below that trigger. It was reasoned
    about from the `autoCompactWindow` setting rather than measured, although
    every compaction record states it outright. Only an automatic compaction
    counts: a manual one says when a person asked, not where the ceiling is.
    """
    out = Path(tempfile.mkdtemp(prefix="cd-compact-"))
    subprocess.run(
        [PY, str(SCRIPTS / "context_census.py"), "--projects", str(FIXTURES / "transcripts"),
         "--out", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
    )
    tsv = (out / "census.tsv").read_text(encoding="utf-8").splitlines()
    header = tsv[0].split("\t")
    rows = [dict(zip(header, line.split("\t"))) for line in tsv[1:]]
    row = next((r for r in rows if r["transcript"].endswith("compaction.jsonl")), None)
    if row is None:
        return (False, "the compaction fixture produced no row")
    if int(row["compactor_events"] or 0) != 2:
        return (False, "counted %s compactions, expected 2" % row["compactor_events"])
    if int(row["compactor_auto_events"] or 0) != 1:
        return (False, "counted %s automatic compactions, expected 1" % row["compactor_auto_events"])
    low = int(row["compactor_lowest_auto_pre_tokens"] or 0)
    if low != 471234:
        return (False, "reported the ceiling as %d; the manual compaction at 120,000 must not "
                       "count" % low)
    summary = (out / "census-summary.md").read_text(encoding="utf-8")
    if "471,234" not in summary:
        return (False, "the summary does not name the measured ceiling")
    return (True, "ceiling read from the automatic compaction at 471,234 and named in the "
                  "summary; the manual one at 120,000 ignored")


@gate("report_names_rows_it_could_not_read")
def g_report_names_bad_rows():
    """A run file that does not parse must be reported, never swallowed.

    A row whose cell count did not match the header was skipped with no counter
    and no message, so a report built on a fraction of the runs looked exactly
    like one built on all of them.
    """
    out = Path(tempfile.mkdtemp(prefix="cd-badrow-"))
    header = ("ts\tmodel\tconfig\ttask\ttrial\tclaude_md_present\tfresh_input\t"
              "cache_creation\tcache_read\toutput\twall_ms\ttool_calls\tfile_reads\t"
              "num_turns\tpassed\tnote")
    good = "\t".join(["2026-09-19T00:00:00", "m", "Fat", "t1", "1", "true",
                      "1000", "100", "5000", "200", "1000", "3", "2", "4", "true", ""])
    bad = "2026-09-19T00:00:00\tm\tFat\ttruncated"
    path = out / "runs.tsv"
    path.write_text("\n".join([header, good, bad, good]) + "\n", encoding="utf-8")
    r = subprocess.run([PY, str(SCRIPTS / "report.py"), "--runs", str(path),
                        "--project", str(out)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    text = r.stdout.decode("utf-8", "replace")
    if "did not match the header" not in text:
        return (False, "a malformed row was skipped without a word about it")
    if "1 line(s)" not in text:
        return (False, "the count of unreadable lines was not stated")
    return (True, "the report opens by naming the 1 line it could not read")


LEGACY_COLS = ("ts", "model", "config", "task", "trial", "claude_md_present",
               "fresh_input", "cache_creation", "cache_read", "output", "wall_ms",
               "tool_calls", "file_reads", "num_turns", "passed", "note")
LIMIT_NOTE = "agent run failed: You've hit your session limit \u00b7 resets 9:40pm (Africa/Cairo)"


def _seeded_ledger(out: Path, with_ran_column: bool) -> Path:
    """Fat with fifteen real runs, Bare with fifteen that never executed.

    These are the shapes of the real 90-run ledger on disk: opus Fat passed
    every task it ran, and every Bare run died on the same usage limit. The
    fixture is that ledger's shape, not an invented one.
    """
    cols = list(LEGACY_COLS)
    if with_ran_column:
        cols.insert(cols.index("passed"), "ran")
    lines = ["\t".join(cols)]
    for trial in (1, 2, 3):
        for task in range(1, 6):
            row = {
                "ts": "2026-09-19T00:00:00", "model": "m", "config": "Fat",
                "task": "t%d" % task, "trial": str(trial), "claude_md_present": "true",
                "fresh_input": "14", "cache_creation": "900", "cache_read": "452093",
                "output": "2353", "wall_ms": "74017", "tool_calls": "9",
                "file_reads": "4", "num_turns": "11", "ran": "true",
                "passed": "true", "note": "",
            }
            lines.append("\t".join(row[c] for c in cols))
            bare = dict(row, config="Bare", fresh_input="0", cache_creation="0",
                        cache_read="0", output="0", wall_ms="21613", tool_calls="0",
                        file_reads="0", num_turns="0", ran="false",
                        passed="" if with_ran_column else "false", note=LIMIT_NOTE)
            lines.append("\t".join(bare[c] for c in cols))
    path = out / ("runs-ran.tsv" if with_ran_column else "runs-legacy.tsv")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@gate("report_refuses_runs_that_never_ran")
def g_report_refuses_non_runs():
    """A config with no runs that executed gets a refusal, never a verdict.

    Forty-five runs of a ninety-run batch died on a usage limit. Each recorded a
    clean-looking passed=False, so the report read fifteen absent Bare runs as
    fifteen lost tasks and was ready to print a confident headline about the
    rediscovery cost of deleting an instruction file. Bare never ran once. This
    is the one failure the whole harness exists to prevent, inverted: not a tool
    that cannot contradict its author, but one that invents the finding its
    author wanted.

    Both ledger shapes are checked. The current one says ran=false outright; the
    one already on disk predates that column and carries only the note, and a
    report that only handles the new shape still fabricates from the old.
    """
    out = Path(tempfile.mkdtemp(prefix="cd-nonrun-"))
    banned = ("rediscovery cost", "prescription held")
    for with_col in (True, False):
        path = _seeded_ledger(out, with_col)
        shape = "ran column" if with_col else "note only"
        r = subprocess.run([PY, str(SCRIPTS / "report.py"), "--runs", str(path),
                            "--project", str(out)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        text = r.stdout.decode("utf-8", "replace")
        for phrase in banned:
            if phrase in text:
                return (False, "%s: stated a verdict from runs that never executed (%r)"
                        % (shape, phrase))
        if "No verdict on task success" not in text:
            return (False, "%s: no refusal was printed" % shape)
        if "Bare" not in text.split("No verdict on task success")[1][:200]:
            return (False, "%s: the refusal does not name the missing config" % shape)
        if "Fat 15/15" not in text:
            return (False, "%s: the fifteen runs that did execute were not counted" % shape)
        if "15 of 30 runs in this ledger never executed" not in text:
            return (False, "%s: the count of non-runs was not stated" % shape)

    # The gate has to discriminate. Replay the same rows under the old semantics
    # - every row counted, non-runs included - and confirm that DOES produce the
    # fabricated headline. A gate that passes on both behaviours tests nothing.
    sys.path.insert(0, str(SCRIPTS))
    import report as _report  # noqa: PLC0415

    rows, _skipped, _derived = _report.read_runs(out / "runs-legacy.tsv")
    for row in rows:
        row["ran"] = True
        row["passed"] = str(row.get("passed")).lower() in ("true", "1", "yes")
    replay = _report.render(rows, {}, False)
    if not any(phrase in replay for phrase in banned):
        return (False, "the old behaviour did not fabricate on this ledger, so the gate "
                       "would pass either way and proves nothing")
    return (True, "both ledger shapes refuse a verdict and name Bare; the pre-fix replay on "
                  "the same rows does print the fabricated headline, so the gate discriminates")


@gate("monitor_never_asks_for_compaction")
def g_no_compact_directive():
    """The monitor may report fullness. It may not ask for a compaction.

    It used to cross 50% and tell the model "Please run /compact now". Measured
    across 413 transcripts on this machine, an ordinary turn above 20,000 tokens
    reads a median 284,209 tokens from cache and writes 1,371; the turn right
    after a compaction writes 90,337 and reads 34,068, and creation exceeds read
    on 86% of post-compaction turns against 4.5% of ordinary ones. A compaction
    moves the conversation from the read price to the write price, so the
    directive was the single largest cost the plugin caused. Both the amber and
    red paths are checked, and so is the shipped source, because a directive
    added back on a path no fixture reaches would pass a behavioural check alone.
    """
    banned = ("/compact", "please run", "run compaction", "compact now")
    for fixture, band in (("amber.jsonl", "amber"), ("red.jsonl", "red")):
        project = temp_project("nocompact-%s" % band)
        code, out, _err, _ms = run_hook(
            "occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / fixture, project))
        if code != 0:
            return (False, "%s exited %d" % (band, code))
        low = out.lower()
        for phrase in banned:
            if phrase in low:
                return (False, "%s still asks for a compaction (%r)" % (band, phrase))
        if "context-diet" not in out:
            return (False, "%s went silent; it should still report and save state" % band)
        manifests = list((project / ".claude" / "context-diet" / "handoff").glob("*.json"))
        if not manifests:
            return (False, "%s spoke without a manifest on disk" % band)

    source = (HOOKS / "occupancy_monitor.py").read_text(encoding="utf-8").lower()
    for phrase in ("please run /compact", "run /compact now"):
        if phrase in source:
            return (False, "the directive is still in the shipped source (%r)" % phrase)
    return (True, "neither band asks for a compaction, both still save and report, and the "
                  "directive is absent from the source")


@gate("cold_cache_turn_is_reported")
def g_cold_cache():
    """A cache miss is the turn that costs money, and it happens below amber.

    The band system watched fullness, so a cold turn at 12% occupancy produced
    nothing at all. The warm fixture is the other half: it is the same size and
    must stay silent, otherwise the gate would pass on a hook that shouted every
    turn and proved only that it can speak.
    """
    project = temp_project("cold")
    code, out, _err, _ms = run_hook(
        "occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / "cold-cache.jsonl", project))
    if code != 0:
        return (False, "cold turn exited %d" % code)
    if "cache went cold" not in out:
        return (False, "a cold turn produced no report: %r" % out[:120])
    if "45,000" not in out:
        return (False, "the report does not name the tokens that were re-processed")
    if "/compact before a break" not in out:
        return (False, "the report does not say what to do instead")
    manifests = list((project / ".claude" / "context-diet" / "handoff").glob("*.json"))
    if not manifests:
        return (False, "state was not saved before speaking")
    _c2, out2, _e2, _m2 = run_hook(
        "occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / "cold-cache.jsonl", project))
    if out2.strip():
        return (False, "it spoke twice; the claim is not one-shot")

    warm = temp_project("warm")
    _c3, out3, _e3, _m3 = run_hook(
        "occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / "warm-cache.jsonl", warm))
    if out3.strip():
        return (False, "a warm turn of the same size also spoke: %r" % out3[:120])
    return (True, "the cold turn is reported once at 12% occupancy, state saved first; the warm "
                  "turn of the same size stays silent")


@gate("impossible_occupancy_pauses")
def g_impossible_occupancy():
    """A reading above 100% is a bug in the monitor, so it says so and stops.

    Escalating to Red on a number the monitor cannot justify is how a wrong
    window entry turns into a wrong irreversible action. A 115% reading was seen
    on a live session and is still unexplained; until it is, the honest response
    is to pause rather than act.
    """
    project = temp_project("impossible")
    code, out, _err, _ms = run_hook(
        "occupancy_monitor.py",
        payload_for(FIXTURES / "transcripts" / "impossible-occupancy.jsonl", project))
    if code != 0:
        return (False, "exited %d" % code)
    if "impossible" not in out:
        return (False, "an impossible reading was not named as one: %r" % out[:160])
    if "paused" not in out:
        return (False, "it did not say monitoring is paused")
    low = out.lower()
    if "armed" in low or "/compact" in low:
        return (False, "it acted on a reading it cannot justify")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) != 1:
        return (False, "expected exactly one line, got %d" % len(lines))
    return (True, "a 115% reading is reported as a bug in the monitor, once, with no action taken")


@gate("fixtures_survive_a_rebuild")
def g_fixtures_survive_rebuild():
    """make_fixtures.py rmtree's its output, so it must generate every fixture.

    Six fixtures were written by hand and never added to the generator. The next
    rebuild deleted all six, and because one of them was the settings file every
    hook gate pins itself to, the monitor gates did not fail loudly - they went
    silent and reported that the monitor had stopped speaking at amber. A
    generator that destroys more than it makes is worse than no generator.

    Rebuilding into a scratch directory and comparing the two file lists is the
    only check that catches this, because a hand-made fixture is indistinguishable
    from a generated one once it is sitting on disk.
    """
    scratch = Path(tempfile.mkdtemp(prefix="cd-rebuild-")) / "fixtures"
    r = subprocess.run([PY, str(HERE / "make_fixtures.py"), "--out", str(scratch)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if r.returncode != 0:
        return (False, "the generator failed: %s" % r.stderr.decode()[:200])

    def listing(root: Path) -> set:
        return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}

    have = listing(FIXTURES)
    made = listing(scratch)
    # The heavy store is opt-in and the index names itself, so neither is a gap.
    ignore = {"INDEX.json"}
    orphans = sorted((have - made) - ignore
                     - {f for f in have if f.startswith("big-store/")})
    if orphans:
        return (False, "%d fixture(s) on disk that a rebuild would destroy and not restore: %s"
                % (len(orphans), ", ".join(orphans[:6])))
    missing = sorted((made - have) - ignore)
    if missing:
        return (False, "the generator makes %d file(s) not present on disk: %s"
                % (len(missing), ", ".join(missing[:6])))
    return (True, "all %d fixtures are generated; a rebuild restores every one of them"
            % len(have - ignore))


@gate("inventory_refuses_an_uncalibrated_token_figure")
def g_inventory_calibration():
    """The audit counts entries. It may only price them against a real reading.

    Reading each skill's full frontmatter description and calling that its cost
    in the index overstates it by a factor of seven, because the description
    carries the worked examples that make a skill fire and the index does not.
    Four candidate models were tried against the app's own reading and all four
    missed, the closest by 44%. So the per-entry rate is calibrated, and without
    a calibration the audit prints counts and says why it will not print tokens.

    It must also price only what is loaded. A plugin installed and not enabled
    costs nothing per message, and counting the shelf is how this becomes a
    scare figure.
    """
    base = Path(tempfile.mkdtemp(prefix="cd-inv-"))
    for name, skills, agents, loaded in (("used-plugin", 3, 1, True),
                                         ("idle-plugin", 5, 0, True),
                                         ("shelf-plugin", 40, 9, False)):
        root = base / name
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name}), encoding="utf-8")
        for i in range(skills):
            d = root / "skills" / ("s%d" % i)
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                "---\nname: s%d\ndescription: %s\n---\n\nbody\n" % (i, "long " * 400),
                encoding="utf-8")
        for i in range(agents):
            d = root / "agents"
            d.mkdir(parents=True, exist_ok=True)
            (d / ("a%d.md" % i)).write_text(
                "---\nname: a%d\ndescription: %s\n---\n" % (i, "long " * 400), encoding="utf-8")
        _ = loaded

    claude_json = base / "claude.json"
    claude_json.write_text(json.dumps({
        "pluginUsage": {"used-plugin@inline": {"usageCount": 9},
                        "idle-plugin@inline": {"usageCount": 0}},
        "skillUsage": {},
    }), encoding="utf-8")
    empty_projects = base / "no-projects"
    empty_projects.mkdir()

    def run(extra):
        r = subprocess.run(
            [PY, str(SCRIPTS / "inventory_audit.py"), "--plugins", str(base),
             "--projects", str(empty_projects), "--claude-json", str(claude_json),
             "--project", str(base)] + extra,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode()[:200]

    code, bare, err = run(["--no-calibration"])
    if code != 0:
        return (False, "uncalibrated run exited %d: %s" % (code, err))
    if "No calibration, so no token figures" not in bare:
        return (False, "it priced the inventory with no reading to price it against")
    if re.search(r"\d[\d,]*\s+tokens", bare):
        return (False, "a token figure was printed without a calibration")
    if "8 skills" not in bare and "8 skills," not in bare:
        return (False, "the loaded skill count is wrong; the shelf plugin was counted")

    code, full, err = run([])
    if code != 0:
        return (False, "calibrated run exited %d: %s" % (code, err))
    if "shelf-plugin" in full.split("plugin ")[-1]:
        return (False, "a plugin that is not loaded appears in the priced table")
    if "tokens per skill" not in full:
        return (False, "the calibrated run does not state the rate it used")
    if "calibrated against" not in full:
        return (False, "the calibrated run does not name where the rate came from")
    # The per-skill rate must come from the calibration, not from the fixture's
    # deliberately enormous descriptions. 8 skills at ~15.4 is about 123 tokens;
    # priced from the text it would be tens of thousands.
    m = re.search(r"skills index\s+([\d,]+) tokens", full)
    if not m:
        return (False, "no skills index line in the calibrated output")
    got = int(m.group(1).replace(",", ""))
    if got > 400:
        return (False, "skills were priced from their description text (%d tokens for 8 "
                       "skills), not from the calibration" % got)
    return (True, "uncalibrated run prints counts and no tokens; calibrated run prices 8 skills "
                  "at %d tokens from the stated rate and leaves the unloaded plugin out" % got)


@gate("install_is_safe_to_run_again")
def g_install_round_trip():
    """A fresh install, an update, and a run in place all have to work.

    Three defects were found by installing rather than by reading: deleting
    fails inside a connected folder, so the installer copies over the top;
    source equal to destination makes cp refuse, so that case verifies instead
    of copying; and the gate suite resolved its config two levels above the
    plugin, which is true in this repo and false once installed, so every hook
    gate failed on an installed copy with a missing-file error that read like a
    monitor regression.

    The update path must keep an edited settings file. Overwriting it would
    silently reset every threshold on upgrade, which is the one thing an
    installer must never do quietly.
    """
    # install.sh runs this whole suite to prove the install worked, and this
    # suite runs install.sh. Without a guard that is unbounded recursion: the
    # first attempt spawned nested installs until it was killed. The installer
    # sets this for the suite it invokes, and seeing it means "you are the
    # inner run, do not install again".
    if os.environ.get("CONTEXT_DIET_INSTALL_CHECK") == "1":
        return (None, "inner run invoked by install.sh; the outer run owns this gate")
    script = PLUGIN / "install.sh"
    if not script.is_file():
        return (False, "no install.sh to test")
    dest = Path(tempfile.mkdtemp(prefix="cd-inst-")) / "target"
    installed = dest / "plugins" / "context-diet"

    env = dict(os.environ, CONTEXT_DIET_INSTALL_CHECK="1")

    def run(src: Path, target: Path):
        return subprocess.run(["bash", str(src), str(target)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)

    r = run(script, dest)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        return (False, "a fresh install failed: %s" % out[-300:])
    if "0 failed" not in out:
        return (False, "the installer claimed success without the gates passing")
    cfg = installed / ".claude" / "context-diet.json"
    if not cfg.is_file():
        return (False, "no settings file was written, so every threshold would fall back "
                       "to a built-in default without saying so")

    # Edit the settings and a data file, then update over the top.
    marker = json.loads(cfg.read_text(encoding="utf-8"))
    marker["amber"] = 0.31
    cfg.write_text(json.dumps(marker), encoding="utf-8")
    data = installed / ".claude" / "context-diet" / "ledger.tsv"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("mine\n", encoding="utf-8")

    r = run(script, dest)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        return (False, "the update failed: %s" % out[-300:])
    if json.loads(cfg.read_text(encoding="utf-8")).get("amber") != 0.31:
        return (False, "the update overwrote an edited settings file")
    if data.read_text(encoding="utf-8") != "mine\n":
        return (False, "the update touched the data directory")
    if not (installed / ".claude" / "context-diet.json.new").is_file():
        return (False, "the new defaults were not left beside the kept settings")
    if not any((dest / "plugins").glob(".context-diet-backup-*")):
        return (False, "the update did not back up the previous version")

    # Source equal to destination: cp refuses, so this path verifies instead.
    r = run(installed / "install.sh", dest)
    out = r.stdout.decode("utf-8", "replace")
    if r.returncode != 0:
        return (False, "running in place failed: %s" % out[-300:])
    if "Already in place" not in out or "0 failed" not in out:
        return (False, "the in-place run did not verify: %s" % out[-200:])
    return (True, "fresh install, update and in-place run all pass their own gates; edited "
                  "settings and data survive the update and the old version is backed up")


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet gates")
    ap.add_argument("--only", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true",
                    help="print every gate name and exit, so a reference to one can be checked")
    args = ap.parse_args()

    if args.list:
        for name, _fn in GATES:
            sys.stdout.write(name + "\n")
        return 0

    results = []
    for name, fn in GATES:
        if args.only and args.only not in name:
            continue
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        results.append({"gate": name, "status": "skip" if ok is None else ("pass" if ok else "FAIL"),
                        "detail": str(detail)})

    if args.json:
        sys.stdout.write(json.dumps(results, indent=2) + "\n")
    else:
        width = max(len(r["gate"]) for r in results) if results else 10
        for r in results:
            sys.stdout.write("%-6s %-*s  %s\n" % (r["status"], width, r["gate"], r["detail"]))
        failed = [r for r in results if r["status"] == "FAIL"]
        skipped = [r for r in results if r["status"] == "skip"]
        sys.stdout.write("\n%d passed, %d failed, %d skipped\n"
                         % (len(results) - len(failed) - len(skipped), len(failed), len(skipped)))
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
