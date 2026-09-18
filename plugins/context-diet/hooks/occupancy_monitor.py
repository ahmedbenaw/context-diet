#!/usr/bin/env python3
"""occupancy_monitor.py - watch how full the window is, and act once.

Runs on PostToolUse and UserPromptSubmit. Reads the tail of one transcript and
parses one record. Budget: 50 ms.

Three rules this file may never break:
  1. It never blocks on failure. Any error at all exits 0 and says nothing.
  2. It is silent below threshold. No status line, no reassurance, no per-turn
     nudge. A context monitor that spends context is the joke this plugin exists
     to avoid.
  3. Two kill switches. CONTEXT_DIET_OFF=1 turns everything off; a file flag
     turns this hook off for one session.

It never runs compaction. No hook can. It writes the manifest first, then asks
the model to compact, and the model does it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK = "occupancy_monitor"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def emit_nothing() -> int:
    return 0


def main() -> int:
    try:
        from cdlib import (  # noqa: PLC0415
            disabled,
            last_usage,
            load_config,
            occupancy,
            state_dir,
            window_for,
        )
    except Exception:
        return emit_nothing()

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return emit_nothing()
    except Exception:
        return emit_nothing()

    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or os.getcwd()
    event = payload.get("hook_event_name") or ""

    try:
        if disabled(HOOK, session_id, cwd):
            return emit_nothing()
        cfg = load_config(cwd)
        usage, model = last_usage(transcript)
        if not usage:
            return emit_nothing()

        window, source = window_for(model, cfg)
        if not window:
            return unknown_model(model, session_id, cwd, state_dir)

        ratio = occupancy(usage, window)
        amber = float(cfg.get("amber") or 0.50)
        red = float(cfg.get("red") or 0.70)
        if ratio < amber:
            return emit_nothing()

        band = "red" if ratio >= red else "amber"
        if already_acted(cwd, session_id, band, state_dir):
            return emit_nothing()

        wrote, verified, problems = write_and_verify_manifest(payload, cwd, session_id, band)
        mark_acted(cwd, session_id, band, state_dir)
        return speak(event, band, ratio, window, source, wrote, verified, problems)
    except Exception:
        # A monitor bug must never trap a working session.
        return emit_nothing()


def unknown_model(model: str, session_id: str, cwd: str, state_dir) -> int:
    """Fail loud and idle, never loud and lossy.

    Guessing the smallest known window would compact a 500k session at 20% real
    occupancy, on every session, until someone edits the table. One unrecoverable
    loss of transcript detail per session is worse than no monitoring.
    """
    try:
        marker = state_dir(cwd) / "unknown-model"
        marker.mkdir(parents=True, exist_ok=True)
        stamp = marker / ("%s.%s" % (session_id or "nosession", "said"))
        if stamp.exists():
            return 0
        stamp.write_text(model or "unknown", encoding="utf-8")
    except Exception:
        return 0
    sys.stdout.write(
        'context-diet: unknown model "%s", window not configured; monitoring paused this '
        "session. Add it to .claude/context-diet.json.\n" % (model or "unknown")
    )
    return 0


def already_acted(cwd: str, session_id: str, band: str, state_dir) -> bool:
    try:
        return (state_dir(cwd) / "acted" / ("%s.%s" % (session_id, band))).exists()
    except Exception:
        return False


def mark_acted(cwd: str, session_id: str, band: str, state_dir) -> None:
    try:
        d = state_dir(cwd) / "acted"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.%s" % (session_id, band))).write_text("1", encoding="utf-8")
    except Exception:
        pass


def write_and_verify_manifest(payload: dict, cwd: str, session_id: str, band: str) -> tuple:
    """Manifest first, always. Compacting first summarises a damaged window."""
    try:
        from handoff import build_manifest, verify_manifest, write_manifest  # noqa: PLC0415

        manifest = build_manifest(payload, cwd, session_id, band)
        kept, problems = verify_manifest(manifest, cwd)
        path = write_manifest(kept, cwd, session_id, armed=(band == "red"))
        return (str(path), True, problems)
    except Exception as exc:
        return ("", False, ["manifest not written: %s" % type(exc).__name__])


def speak(event: str, band: str, ratio: float, window: int, source: str,
          manifest: str, verified: bool, problems: list) -> int:
    pct = ratio * 100.0
    if band == "amber":
        line = (
            "context-diet: this session is at %.0f%% of its %s-token window (%s). "
            "State is saved to %s. Please run /compact now: the saved file holds the "
            "checkable facts, so nothing important lives only in the transcript."
            % (pct, format(window, ","), source, manifest or "(not written)")
        )
    else:
        line = (
            "context-diet: this session is at %.0f%% of its %s-token window. A handoff is "
            "armed at %s. Start a new session when convenient and it will load from there "
            "in one line."
            % (pct, format(window, ","), manifest or "(not written)")
        )
    if problems:
        line += " %d field(s) failed verification and were dropped, not repaired." % len(problems)

    if event == "UserPromptSubmit":
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": line,
                    }
                }
            )
            + "\n"
        )
    else:
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
