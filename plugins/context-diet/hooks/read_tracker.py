#!/usr/bin/env python3
"""read_tracker.py - repeat reads of a file that has not changed.

Reading one unchanged file three times in a session pays for it three times. The
tracker counts, and at the threshold says one sentence, once per path.

Ships enabled only where the census showed repeat reads are a real cost. It is
silent until the threshold and never speaks twice about the same path.
"""

from __future__ import annotations

import hashlib
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
            st = Path(target).stat()
            # Whole-second mtime cannot tell an edit from a re-read inside the
            # same second, so an edit-then-read was counted as an unchanged
            # repeat. Nanoseconds plus size is what actually distinguishes them,
            # and the gate no longer has to sleep 1.1 s to hide it.
            stamp = [st.st_mtime_ns, st.st_size]
        except OSError:
            return 0

        store = state_dir(cwd) / "reads"
        store.mkdir(parents=True, exist_ok=True)

        # Counting by read-modify-write loses reads. Hooks run in parallel, and
        # even one file per path lost 4 of 50 concurrent reads of that path:
        # every process read the same number and the last writer won. So no
        # process ever writes a count. Each read appends one line to a file
        # whose name already carries the file's identity, and the count is the
        # number of lines. An O_APPEND write of a few bytes does not interleave,
        # so nothing is lost and nothing has to be locked.
        #
        # The version stamp is part of the name, so an edit starts a new file
        # and the old count is simply not consulted again.
        ident = "%s\x00%s\x00%s" % (target, stamp[0], stamp[1])
        digest = hashlib.sha256(ident.encode("utf-8", "replace")).hexdigest()[:16]
        tally = store / ("%s.%s.reads" % (session_id or "nosession", digest))
        try:
            fd = os.open(str(tally), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, b".")
            finally:
                os.close(fd)
            count = tally.stat().st_size
        except OSError:
            return 0

        if count < threshold:
            return 0

        # Exactly one process may speak, whoever creates the marker first.
        said = tally.with_suffix(".said")
        try:
            os.close(os.open(str(said), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return 0
        except OSError:
            return 0

        sys.stdout.write(
            "context-diet: %s has been read %d times unchanged this session. Hoisting a "
            "short summary of it would stop paying for the whole file each time.\n"
            % (target, count)
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
