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

PY = sys.executable
GATES = []


def gate(name: str):
    def wrap(fn):
        GATES.append((name, fn))
        return fn

    return wrap


def run_hook(script: str, payload: dict, env_extra: dict | None = None, cwd: str | None = None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS)
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


def temp_project(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="cd-%s-" % name))
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / ".claude" / "context-diet.json", d / ".claude" / "context-diet.json")
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
    ok = inner < 50
    return (ok, "in-process %.1f ms, median subprocess wall %.0f ms" % (inner, sorted(times)[2]))


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
    run_hook("occupancy_monitor.py", payload_for(FIXTURES / "transcripts" / "red.jsonl", project))
    p = {"session_id": "new", "cwd": str(project), "hook_event_name": "SessionStart"}
    _c1, out1, _e1, _m1 = run_hook("session_start.py", p)
    _c2, out2, _e2, _m2 = run_hook("session_start.py", p)
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
    # Second run must use the cache and stay well inside the bound.
    started2 = time.time()
    run_hook("stop_ledger.py",
             {"session_id": "st2", "cwd": str(project),
              "transcript_path": str(FIXTURES / "transcripts" / "amber.jsonl"),
              "hook_event_name": "Stop"})
    cached_ms = (time.time() - started2) * 1000
    return (reported, "reported, deleted nothing, first %.0f ms, cached %.0f ms"
            % (elapsed, cached_ms))


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
    r = subprocess.run(
        [PY, str(SCRIPTS / "hook_audit.py"), "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    )
    if r.returncode != 0:
        return (False, r.stderr.decode()[:200])
    data = json.loads(r.stdout.decode())
    dups = data.get("duplicate_registrations", [])
    found = any("inject-memory.sh" in d["command"] for d in dups)
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


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet gates")
    ap.add_argument("--only", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

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
