#!/usr/bin/env python3
"""extract.py - split instruction files into addressable sections with stable IDs.

The audit skill refers to "CLAUDE.md#3.2" instead of re-quoting the file back
into context. A tool that pulls the whole file into the window to decide the file
is too big has already lost the argument.

IDs are stable across runs for unchanged text: the id is the heading path plus a
short hash of the heading, so inserting a section above does not renumber the
ones below.

Usage:
    extract.py PATH [PATH ...] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import Tokenizer, load_config  # noqa: E402


def heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    hashes = len(stripped) - len(stripped.lstrip("#"))
    return hashes if stripped[hashes : hashes + 1] == " " else 0


def section_id(path: Path, crumb: list, heading: str) -> str:
    digest = hashlib.sha256(("/".join(crumb) + "|" + heading).encode("utf-8")).hexdigest()[:8]
    return "%s#%s" % (path.name, digest)


def extract(path: Path, tok: Tokenizer) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"path": str(path), "error": str(exc), "sections": []}
    lines = text.splitlines()
    sections = []
    crumb: list = []
    current = None

    def close(end_line: int) -> None:
        if current is None:
            return
        body = "\n".join(current["lines"])
        current["end_line"] = end_line
        current["tokens"] = tok.count(current["heading"] + "\n" + body)
        current["bytes"] = len(body.encode("utf-8"))
        current["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        current["preview"] = " ".join(body.split())[:160]
        del current["lines"]
        sections.append(current)

    for i, line in enumerate(lines, start=1):
        level = heading_level(line)
        if level:
            close(i - 1)
            crumb = crumb[: level - 1]
            crumb.append(line.lstrip("#").strip())
            current = {
                "id": section_id(path, crumb, line.strip()),
                "heading": line.strip(),
                "level": level,
                "path": "/".join(crumb),
                "start_line": i,
                "lines": [],
            }
        else:
            if current is None:
                current = {
                    "id": section_id(path, ["(preamble)"], "(preamble)"),
                    "heading": "(preamble)",
                    "level": 0,
                    "path": "(preamble)",
                    "start_line": i,
                    "lines": [],
                }
            current["lines"].append(line)
    close(len(lines))
    return {"path": str(path), "tokens": tok.count(text), "sections": sections}


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet extract")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(".")
    tok = Tokenizer(cfg.get("tokenizer", "auto"))
    out = [extract(Path(p), tok) for p in args.paths]

    if args.json:
        sys.stdout.write(json.dumps({"tokenizer": tok.name, "files": out}, indent=2) + "\n")
        return 0
    for doc in out:
        if doc.get("error"):
            sys.stdout.write("%s: %s\n" % (doc["path"], doc["error"]))
            continue
        sys.stdout.write("%s  (%s tokens, tokeniser %s)\n"
                         % (doc["path"], format(doc["tokens"], ","), tok.name))
        for s in doc["sections"]:
            sys.stdout.write("  %-24s L%-5d %6s tok  %s\n"
                             % (s["id"], s["start_line"], format(s["tokens"], ","),
                                s["heading"][:60]))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
