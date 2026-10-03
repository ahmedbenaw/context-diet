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

It never runs compaction, and it never asks for one either. No hook can invoke
/compact, and asking the model to was worse than useless: measured across 413
transcripts on this machine, an ordinary turn above 20,000 tokens reads a median
284,209 tokens from cache and writes 1,371, while the turn right after a
compaction writes 90,337 and reads 34,068. Creation exceeds read on 86% of
post-compaction turns against 4.5% of ordinary ones. A compaction moves the
conversation from the read price to the write price, so asking for extra ones
spends more. Fullness is written down and never acted on.

What it watches instead is the cache going cold, which is the event tied to
money. It saves your place and says so once.
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
            cache_state,
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

        # The cache signal comes first and does not depend on a window. A cold
        # turn at 10% occupancy costs real money; the band system would have
        # been silent for it, because it was watching the wrong quantity.
        cold_spoke = cold_turn(usage, cfg, payload, cwd, session_id, event, state_dir,
                               cache_state)

        window, source = window_for(model, cfg)
        if not window:
            return emit_nothing() if cold_spoke else unknown_model(
                model, session_id, cwd, state_dir, event)

        ratio = occupancy(usage, window)
        if ratio > 1.0:
            # A reading above 100% is the monitor reporting a bug in itself. It
            # says so instead of escalating to Red, because acting on a number
            # it cannot justify is how a wrong window number becomes a wrong
            # irreversible action.
            return impossible(model, ratio, window, session_id, cwd, event, state_dir)
        amber = float(cfg.get("amber") or 0.50)
        red = float(cfg.get("red") or 0.70)
        if ratio < amber:
            return emit_nothing()

        band = "red" if ratio >= red else "amber"
        if not claim_band(cwd, session_id, band, state_dir):
            return emit_nothing()

        # The claim is already held, so the expensive work happens at most once
        # per band even under a parallel tool batch, and a run killed by the
        # hook timeout cannot start the whole scan again on the next call.
        wrote, verified, problems = write_and_verify_manifest(payload, cwd, session_id, band)
        if not wrote or not verified:
            # Never ask for compaction when there is no state on disk. Asking
            # anyway told the model to discard transcript detail and then, with
            # the band consumed, stayed silent forever. Give the claim back so
            # the next call retries, and let PreCompact cover the gap.
            release_band(cwd, session_id, band, state_dir)
            return emit_nothing()
        return speak(event, band, ratio, window, source, wrote, verified, problems)
    except Exception:
        # A monitor bug must never trap a working session.
        return emit_nothing()


def claim_once(cwd: str, session_id: str, kind: str, state_dir, payload: str = "") -> bool:
    """Take a one-shot claim for this session, atomically. False means it is taken.

    Every notice in this hook goes through a claim. PostToolUse fires once per
    tool call and a parallel batch fires several at once, so a check followed by
    a separate write lets the same notice print three times.
    """
    try:
        folder = state_dir(cwd) / kind
        folder.mkdir(parents=True, exist_ok=True)
        stamp = folder / ("%s.said" % (session_id or "nosession"))
        fd = os.open(str(stamp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False


def cold_turn(usage: dict, cfg: dict, payload: dict, cwd: str, session_id: str,
              event: str, state_dir, cache_state) -> bool:
    """Say once when the cache went cold, because that is the turn that cost money.

    A cold turn re-creates the prefix instead of reading it. Reads are billed at
    a fraction of writes, so this, and not a fullness percentage, is the event
    with a price attached. It saves state and reports; it never asks for an
    action that would cause another one.
    """
    floor = int(cfg.get("cold_turn_min_tokens") or 20000)
    state, created, read = cache_state(usage, floor)
    if state != "cold":
        return False
    if not claim_once(cwd, session_id, "cold-cache", state_dir, str(created)):
        return False
    wrote, _verified, _problems = write_and_verify_manifest(payload, cwd, session_id, "amber")
    emit(event, (
        "context-diet: the cache went cold on this turn - %s tokens were re-processed at the "
        "write price instead of being read back at the read price, against %s that were read. "
        "Your place is saved at %s. Compacting now would do the same thing again, so the useful "
        "habit is /compact before a break rather than after one."
        % (format(created, ","), format(read, ","), wrote or "(not written)")
    ))
    return True


def impossible(model: str, ratio: float, window: int, session_id: str, cwd: str,
               event: str, state_dir) -> int:
    """An occupancy above 100% means the window is wrong for this session; say so and stop.

    It was first read as a bug in the monitor. Measured on 2026-10-04 it has two
    causes. A multi-pass turn's top-level usage sums its passes, about 2x, which
    context_tokens() already corrects by reading the last pass. And a session
    keeps the compaction trigger it started with: one that began on 13 Sep under
    the old ~1M trigger compacted at 660,956 on 14 Sep, hours after another had
    compacted at 467,778 under the lowered setting. Read against the current
    setting, such a session is genuinely past 100%. Escalating to Red on a
    window that is not this session's would turn it into a wrong irreversible
    action, so it is reported once, then silence for the session.
    """
    if not claim_once(cwd, session_id, "impossible", state_dir, "%.4f" % ratio):
        return 0
    emit(event, (
        'context-diet: this session reads as %.0f%% of a %s-token window for "%s", so that '
        "window is not this session's. Usually the session started before autoCompactWindow "
        "was lowered and still compacts at its old size; otherwise .claude/context-diet.json "
        "has a wrong entry. Occupancy monitoring is paused for this session. Cache-miss "
        "detection still runs."
        % (ratio * 100.0, format(window, ","), model or "unknown")
    ))
    return 0


def unknown_model(model: str, session_id: str, cwd: str, state_dir, event: str = "") -> int:
    """Fail loud and idle, never loud and lossy.

    Guessing the smallest known window would compact a 500k session at 20% real
    occupancy, on every session, until someone edits the table. One unrecoverable
    loss of transcript detail per session is worse than no monitoring.
    """
    try:
        # Claim before speaking, exclusively, for the same reason the band does:
        # a check followed by a separate write lets a parallel tool batch print
        # the same notice several times.
        marker = state_dir(cwd) / "unknown-model"
        marker.mkdir(parents=True, exist_ok=True)
        stamp = marker / ("%s.%s" % (session_id or "nosession", "said"))
        fd = os.open(str(stamp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(model or "unknown")
    except FileExistsError:
        return 0
    except Exception:
        return 0
    line = (
        'context-diet: unknown model "%s", window not configured; monitoring paused this '
        "session. Add it to .claude/context-diet.json." % (model or "unknown")
    )
    # Same delivery rule as the band directive: on the two events that accept it,
    # the structured form is what actually reaches the model. A bare line here
    # meant the pause notice was visible or invisible depending on which event
    # happened to hit the unknown model first.
    emit(event, line)
    return 0


def band_marker(cwd: str, session_id: str, band: str, state_dir) -> Path:
    return state_dir(cwd) / "acted" / ("%s.%s" % (session_id, band))


def claim_band(cwd: str, session_id: str, band: str, state_dir) -> bool:
    """Take the one-shot claim for this band, atomically. False means someone else has it.

    PostToolUse fires once per tool call and a parallel batch fires several at
    once. A check followed by a separate write let three of four concurrent
    monitors through, each doing a full manifest build and each speaking. An
    exclusive create is the whole guard.
    """
    try:
        marker = band_marker(cwd, session_id, band, state_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # A marker that cannot be written must not make the monitor act twice,
        # and must not trap the session either.
        return False


def release_band(cwd: str, session_id: str, band: str, state_dir) -> None:
    """Give the claim back so a later call can retry after a failed attempt."""
    try:
        band_marker(cwd, session_id, band, state_dir).unlink()
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
            "context-diet: this session is at %.0f%% of its %s-token window (%s). State is "
            "saved to %s, so nothing important lives only in the transcript. No action is "
            "needed: compacting now would re-process the conversation at the write price "
            "rather than the read price, which costs more than carrying it."
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

    emit(event, line)
    return 0


def emit(event: str, line: str) -> None:
    """One delivery rule for everything this hook says.

    The band marker is one-shot per session, so whichever event reaches the
    threshold first is the only one that ever speaks. When that was PostToolUse
    it wrote a bare line while only the UserPromptSubmit branch produced a
    directive the model is given, so the same crossing either delivered a
    directive or did not depending on which event fired first.
    """
    if event in ("UserPromptSubmit", "PostToolUse"):
        sys.stdout.write(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": event, "additionalContext": line}}
            )
            + "\n"
        )
    else:
        sys.stdout.write(line + "\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        raise SystemExit(0)
