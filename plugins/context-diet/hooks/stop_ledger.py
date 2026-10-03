#!/usr/bin/env python3
"""stop_ledger.py - one row per session, and the storage signals.

Runs once per session on Stop. Appends token, time and hook totals to a local
TSV, then runs the storage signals behind a 24-hour cache. Injects nothing.

Storage scans live here and nowhere else. Walking a 370 MB transcript store
inside a 50 ms per-tool-call monitor would break the monitor's budget on every
call, so the expensive measurement runs once per session and is cached.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HOOK = "stop_ledger"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def at_least(truncated: bool) -> str:
    """Say so when a figure stopped counting early, rather than stating it as the total."""
    return "at least " if truncated else ""


def dir_size(path: Path, cap_files: int = 20000) -> tuple:
    """(total_bytes, file_count, truncated).

    The walk stops at cap_files so a Stop hook cannot be held open by a huge
    store. It used to return the partial total with nothing to say so, and the
    caller reported it as the size of the directory. A number that stopped
    counting early is not that directory's size, so the caller is told.
    """
    total = 0
    count = 0
    truncated = False
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
                    count += 1
                    if count >= cap_files:
                        truncated = True
                        break
            except OSError:
                continue
    except OSError:
        pass
    return (total, count, truncated)


def storage_signals(cwd: str, cfg: dict, state_dir) -> list:
    """Cached for 24 hours. Reports. Never deletes anything, ever."""
    cache = state_dir(cwd) / "storage-cache.json"
    ttl = int(cfg.get("storage_signal_ttl_seconds") or 86400)
    now = int(time.time())
    try:
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if now - int(data.get("at", 0)) < ttl:
                return data.get("lines", [])
    except (OSError, ValueError):
        pass

    lines = []
    transcripts = Path.home() / ".claude" / "projects"
    size, count, cut = dir_size(transcripts)
    if size >= int(cfg.get("transcript_store_bytes") or 10**9) or count >= int(
        cfg.get("transcript_store_files") or 1000
    ):
        lines.append(
            "transcript store: %s%.1f GB across %d files at %s. Nothing was deleted."
            % (at_least(cut), size / 1e9, count, transcripts)
        )
    backups = state_dir(cwd) / "backups"
    if backups.is_dir():
        s, c, cut = dir_size(backups)
        if s >= int(cfg.get("backups_store_bytes") or 10**8):
            lines.append("backups: %s%.0f MB across %d files. Nothing was deleted."
                         % (at_least(cut), s / 1e6, c))

    # The MLflow store is a SQLite file, not a directory. Measuring only the old
    # mlruns directory reported zero bytes forever while the real store grew
    # unwatched, so both shapes count and a legacy directory still registers.
    store = state_dir(cwd)
    mlflow_bytes, mlflow_files = 0, 0
    db = store / "mlflow.db"
    if db.is_file():
        mlflow_bytes += db.stat().st_size
        mlflow_files += 1
    legacy = store / "mlruns"
    mlflow_cut = False
    if legacy.is_dir():
        s, c, mlflow_cut = dir_size(legacy)
        mlflow_bytes += s
        mlflow_files += c
    if mlflow_files and mlflow_bytes >= int(cfg.get("mlflow_store_bytes") or 10**8):
        lines.append(
            "mlflow store: %s%.0f MB across %d file(s). Nothing was deleted."
            % (at_least(mlflow_cut), mlflow_bytes / 1e6, mlflow_files)
        )
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"at": now, "lines": lines}), encoding="utf-8")
    except OSError:
        pass
    return lines


def session_totals(transcript: str) -> dict:
    totals = {
        "turns": 0,
        "fresh_input": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "output": 0,
        "tool_calls": 0,
        "hook_ms": 0,
        "static_prefix": 0,
        "injected_bytes": 0,
        "model": "",
    }
    p = Path(transcript or "")
    if not p.is_file():
        return totals
    try:
        fh = p.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return totals
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rtype = rec.get("type")
            if rtype == "assistant":
                msg = rec.get("message") or {}
                model = msg.get("model") or ""
                if model == "<synthetic>":
                    continue
                if model:
                    totals["model"] = model
                usage = msg.get("usage") or {}
                totals["turns"] += 1
                cc = int(usage.get("cache_creation_input_tokens") or 0)
                if totals["turns"] == 1:
                    totals["static_prefix"] = cc
                totals["fresh_input"] += int(usage.get("input_tokens") or 0)
                totals["cache_creation"] += cc
                totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                totals["output"] += int(usage.get("output_tokens") or 0)
                for blk in msg.get("content") or []:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        totals["tool_calls"] += 1
            elif rtype == "attachment":
                att = rec.get("attachment") or {}
                atype = att.get("type")
                if atype in ("hook_success", "hook_non_blocking_error"):
                    totals["hook_ms"] += int(att.get("durationMs") or 0)
                elif atype == "hook_additional_context":
                    content = att.get("content")
                    totals["injected_bytes"] += (
                        sum(len(str(c)) for c in content)
                        if isinstance(content, list)
                        else len(str(content or ""))
                    )
    return totals


def main() -> int:
    started = time.time()
    try:
        from cdlib import append_ledger, disabled, load_config, state_dir  # noqa: PLC0415
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
    transcript = payload.get("transcript_path") or ""

    try:
        if disabled(HOOK, session_id, cwd):
            return 0
        cfg = load_config(cwd)
        totals = session_totals(transcript)
        row = {"ts": int(time.time()), "session_id": session_id, "cwd": cwd}
        row.update(totals)
        row["storage_lines"] = "|".join(storage_signals(cwd, cfg, state_dir))
        row["ledger_ms"] = int((time.time() - started) * 1000)
        append_ledger(row, cwd, cfg)

        # The hook layer never imports MLflow. Importing it here cost about two
        # seconds per session end and printed a hint to stderr, which broke both
        # this hook's stdlib-only contract and its promise to stay silent. The
        # row is queued instead, and the command layer drains the queue into
        # MLflow when a report runs.
        try:
            pending = state_dir(cwd) / "mlflow-pending.jsonl"
            pending.parent.mkdir(parents=True, exist_ok=True)
            with pending.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except OSError:
            # A queue that cannot be written is not worth failing a session over.
            pass
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
