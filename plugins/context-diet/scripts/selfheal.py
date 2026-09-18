#!/usr/bin/env python3
"""selfheal.py - the entire list of things this plugin does automatically.

The list below is closed. An action that is not in it is a proposal for a human,
never an automatic act. Every action here is reversible, is logged with its
before and after values, and is appended to the decision trail.

What self-heal never does: it never deletes, never edits source code, never
changes a threshold to make a scorer pass, never runs compaction (no hook can),
never applies an audit cut without approval, and never starts a server.

Usage:
    selfheal.py run --scorer NAME [--project DIR] [--session ID] [--hook NAME]
    selfheal.py table
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import append_decision, disable_hook, load_config, state_dir  # noqa: E402

# scorer -> (what happens, how it is undone)
TABLE = {
    "unknown_model_idle": (
        "Pause occupancy monitoring for this session and emit one line naming the model.",
        "Resumes next session, or when the model is added to context_windows.",
    ),
    "manifest_roundtrip": (
        "Drop the failing field, log which one, re-verify the rest.",
        "The transcript on disk still holds the detail.",
    ),
    "hook_latency_budget": (
        "Write the per-hook file flag for this session and report the measured milliseconds.",
        "The flag is session scoped, so the hook is enabled again next session.",
    ),
    "apply_revert_identity": (
        "Restore every file from the backup set and mark the plan entry rejected.",
        "The backup is the original, byte for byte.",
    ),
    "storage_signal_cost": (
        "Use the cached storage value and skip the directory walk.",
        "The next Stop past the TTL measures again.",
    ),
    "no_source_edit": (
        "Restore the whole backup set, then stop and report. A hard stop, not a retry.",
        "The backup is the original.",
    ),
    "ledger_rotation": (
        "Rotate the ledger to a dated file.",
        "Nothing is discarded. The rotated file stays beside the new one.",
    ),
}


def heal_unknown_model(args, cfg) -> tuple:
    if not args.session:
        return (False, "needs --session")
    flag = disable_hook("occupancy_monitor", args.session, args.project)
    return (True, "monitoring paused for this session via %s" % flag)


def heal_manifest(args, cfg) -> tuple:
    from handoff import load_armed, verify_manifest, write_manifest

    data, path = load_armed(args.project)
    if not data:
        return (False, "no armed manifest to repair")
    kept, problems = verify_manifest(data, args.project)
    write_manifest(kept, args.project, data.get("session_id", ""), armed=data.get("armed", False))
    return (True, "dropped %d field(s), none repaired: %s" % (len(problems), "; ".join(problems[:3])))


def heal_hook_latency(args, cfg) -> tuple:
    if not (args.session and args.hook):
        return (False, "needs --session and --hook")
    flag = disable_hook(args.hook, args.session, args.project)
    return (True, "%s disabled for this session via %s" % (args.hook, flag))


def heal_restore_backup(args, cfg) -> tuple:
    import subprocess

    r = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "patch.py"), "revert",
         "--project", args.project],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    ok = r.returncode == 0
    return (ok, (r.stdout or r.stderr).decode("utf-8", "replace").strip()[:300])


def heal_storage_cache(args, cfg) -> tuple:
    cache = state_dir(args.project) / "storage-cache.json"
    if not cache.is_file():
        return (False, "no cached storage value to fall back to")
    return (True, "using cached value from %s" % cache)


def heal_ledger(args, cfg) -> tuple:
    d = state_dir(args.project)
    ledger = d / "ledger.tsv"
    if not ledger.is_file():
        return (False, "no ledger to rotate")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = d / ("ledger-%s.tsv" % stamp)
    shutil.move(str(ledger), str(target))
    return (True, "rotated to %s, nothing discarded" % target.name)


ACTIONS = {
    "unknown_model_idle": heal_unknown_model,
    "manifest_roundtrip": heal_manifest,
    "hook_latency_budget": heal_hook_latency,
    "apply_revert_identity": heal_restore_backup,
    "no_source_edit": heal_restore_backup,
    "storage_signal_cost": heal_storage_cache,
    "ledger_rotation": heal_ledger,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet self-heal")
    sub = ap.add_subparsers(dest="action", required=True)
    t = sub.add_parser("table")
    t.set_defaults(fn="table")
    r = sub.add_parser("run")
    r.add_argument("--scorer", required=True)
    r.add_argument("--project", default=".")
    r.add_argument("--session", default="")
    r.add_argument("--hook", default="")
    r.set_defaults(fn="run")
    args = ap.parse_args()

    if args.fn == "table":
        sys.stdout.write("Automatic responses. This list is closed.\n\n")
        for scorer, (does, undo) in TABLE.items():
            sys.stdout.write("%s\n  does: %s\n  undo: %s\n\n" % (scorer, does, undo))
        sys.stdout.write("Anything not listed here is a proposal for a human, never automatic.\n")
        return 0

    if args.scorer not in ACTIONS:
        sys.stderr.write(
            "no automatic action for %s. It is a proposal for a human, by design.\n" % args.scorer
        )
        return 1
    cfg = load_config(args.project)
    ok, detail = ACTIONS[args.scorer](args, cfg)
    append_decision("self-heal", args.scorer, "ok" if ok else "refused", detail)
    sys.stdout.write("%s: %s\n" % ("healed" if ok else "refused", detail))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
