#!/usr/bin/env python3
"""read_tracker.py - repeat reads of a file that has not changed.

Reading one unchanged file three times in a session pays for it three times. The
tracker counts, and at the threshold says one sentence, once per path.

Ships enabled only where the census showed repeat reads are a real cost. It is
silent until the threshold and never speaks twice about the same path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK = "read_tracker"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def main() -> int:
    try:
        from cdlib import disabled, load_config, state_dir  # noqa: PLC0415
    except Exception:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
    except Exception:
        return 0

    tool = payload.get("tool_name") or ""
    if tool not in ("Read", "NotebookRead"):
        return 0
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not target:
        return 0

    try:
        if disabled(HOOK, session_id, cwd):
            return 0
        cfg = load_config(cwd)
        threshold = int(cfg.get("repeat_read_threshold") or 3)
        try:
            mtime = int(Path(target).stat().st_mtime)
        except OSError:
            return 0

        store = state_dir(cwd) / "reads"
        store.mkdir(parents=True, exist_ok=True)
        key = "%s.json" % (session_id or "nosession")
        path = store / key
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}

        entry = data.get(target) or {"count": 0, "mtime": mtime, "said": False}
        if entry.get("mtime") != mtime:
            # The file changed, so re-reading it is not a repeat read.
            entry = {"count": 0, "mtime": mtime, "said": False}
        entry["count"] += 1
        data[target] = entry
        try:
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            return 0

        if entry["count"] >= threshold and not entry["said"]:
            entry["said"] = True
            data[target] = entry
            try:
                path.write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass
            sys.stdout.write(
                "context-diet: %s has been read %d times unchanged this session. Hoisting a "
                "short summary of it would stop paying for the whole file each time.\n"
                % (target, entry["count"])
            )
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
