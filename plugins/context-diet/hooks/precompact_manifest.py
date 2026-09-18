#!/usr/bin/env python3
"""precompact_manifest.py - the fallback when the built-in compactor wins the race.

Claude Code compacts on its own at autoCompactWindow. This plugin never competes
with that: its Amber band sits strictly below it, and its action is manifest then
directive, never a second compaction mechanism.

If the built-in compactor fires first, this hook is the last chance to get the
state onto disk. It writes a manifest only when one does not already exist for
this session, so it never overwrites the richer one the monitor wrote earlier.

Budget: 200 ms. Never blocks. Silent always.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOK = "precompact_manifest"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def main() -> int:
    try:
        from cdlib import disabled  # noqa: PLC0415
        from handoff import build_manifest, handoff_dir, verify_manifest, write_manifest
    except Exception:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
    except Exception:
        return 0

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    try:
        if disabled(HOOK, session_id, cwd):
            return 0
        existing = handoff_dir(cwd) / ("%s.json" % (session_id or "nosession"))
        if existing.is_file():
            return 0
        manifest = build_manifest(payload, cwd, session_id, "precompact")
        kept, _problems = verify_manifest(manifest, cwd)
        write_manifest(kept, cwd, session_id, armed=True)
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
