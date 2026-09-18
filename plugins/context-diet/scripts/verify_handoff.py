#!/usr/bin/env python3
"""verify_handoff.py - stat every path, match every checksum, resolve the commit.

A thin entry point over handoff.verify_manifest so the verification gate is one
command a reviewer can run. Exits 1 when a field failed, and names it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from handoff import load_armed, verify_manifest  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet verify handoff")
    ap.add_argument("--project", default=".")
    ap.add_argument("--file", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.file:
        target = Path(args.file)
        if not target.is_file():
            sys.stderr.write("no such manifest: %s\n" % target)
            return 1
        data = json.loads(target.read_text(encoding="utf-8"))
    else:
        data, target = load_armed(args.project)
        if not data:
            sys.stdout.write("no armed handoff for this project\n")
            return 0

    kept, problems = verify_manifest(data, args.project)
    if args.json:
        sys.stdout.write(json.dumps({"manifest": str(target), "problems": problems}, indent=2) + "\n")
    else:
        sys.stdout.write("manifest: %s\n" % target)
        sys.stdout.write("paths checked: %d touched, %d read\n"
                         % (len(kept.get("files", {}).get("touched", [])),
                            len(kept.get("files", {}).get("read", []))))
        if problems:
            sys.stdout.write("dropped %d field(s), none repaired:\n" % len(problems))
            for p in problems:
                sys.stdout.write("  %s\n" % p)
        else:
            sys.stdout.write("every field verified against disk\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
