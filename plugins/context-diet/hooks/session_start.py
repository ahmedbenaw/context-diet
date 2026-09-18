#!/usr/bin/env python3
"""session_start.py - the first thing a session sees, or nothing at all.

Order of precedence:
  1. An armed, verified handoff for this project: inject it once, mark consumed.
  2. Otherwise, if the previous session crossed a threshold: exactly one line.
  3. Otherwise: nothing.

Budget: 200 ms. Never blocks. Silent unless one of the two cases above applies.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK = "session_start"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def inject(text: str) -> int:
    sys.stdout.write(
        json.dumps(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        )
        + "\n"
    )
    return 0


def previous_session_line(cwd: str, cfg: dict) -> str:
    """One line, only when the last recorded session actually crossed a line."""
    try:
        from cdlib import state_dir  # noqa: PLC0415

        ledger = state_dir(cwd) / "ledger.tsv"
        if not ledger.is_file():
            return ""
        with ledger.open(encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            last = ""
            for line in fh:
                if line.strip():
                    last = line
        if not last:
            return ""
        cells = last.rstrip("\n").split("\t")
        row = dict(zip(header, cells))
    except Exception:
        return ""

    parts = []
    try:
        prefix = int(row.get("static_prefix", 0) or 0)
        if prefix >= int(cfg.get("static_prefix_tokens") or 60000):
            parts.append("a %s-token static prefix" % format(prefix, ","))
    except ValueError:
        pass
    try:
        hook_ms = int(row.get("hook_ms", 0) or 0)
        if hook_ms >= int(cfg.get("hook_latency_ms_per_tool_call") or 2000):
            parts.append("%s ms of hook time" % format(hook_ms, ","))
    except ValueError:
        pass
    if not parts:
        return ""
    return ("context-diet: the previous session carried %s. Run /context-diet:budget to see "
            "where it goes." % " and ".join(parts))


def main() -> int:
    try:
        from cdlib import disabled, load_config, state_dir  # noqa: PLC0415
        from handoff import load_armed, mark_consumed, render, stale_manifests, verify_manifest
    except Exception:
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()

    try:
        if disabled(HOOK, session_id, cwd):
            return 0
        cfg = load_config(cwd)

        data, path = load_armed(cwd)
        if data and path:
            # Verify before injecting. A stale manifest is a session acting on
            # state that no longer exists, which is worse than no manifest.
            kept, problems = verify_manifest(data, cwd)
            mark_consumed(path)
            text = render(kept)
            stale = stale_manifests(cwd, int(cfg.get("stale_handoff_days") or 7))
            if stale:
                text += "\n%d unconsumed handoff(s) older than %d days remain on disk." % (
                    len(stale), int(cfg.get("stale_handoff_days") or 7))
            return inject(text)

        line = previous_session_line(cwd, cfg)
        if line:
            return inject(line)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
