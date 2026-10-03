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
from cdlib import Tokenizer, fence_mask, heading_level, load_config  # noqa: E402


def section_id(path: Path, crumb: list, heading: str, occurrence: int) -> str:
    """A stable id that is unique even when two sections share a name.

    The digest used to cover the crumb and the heading only, and the crumb
    already ends with that heading, so two identically named sections under one
    parent produced the same id and /apply cut whichever came first. The
    occurrence ordinal distinguishes them, and unlike a line number it does not
    churn when unrelated text above is edited.
    """
    key = "/".join(crumb) + "|" + heading + "|#" + str(occurrence)
    return "%s#%s" % (path.name, hashlib.sha256(key.encode("utf-8")).hexdigest()[:8])


def extract(path: Path, tok: Tokenizer) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"path": str(path), "error": str(exc), "sections": []}
    lines = text.splitlines()
    # A hash at the start of a line inside a bash block is a shell comment, not
    # a heading. Without this the section boundaries here disagreed with the
    # ones patch.py computed, so the id in a plan pointed at a different span of
    # text than the one /apply would cut.
    fenced = fence_mask(lines)
    sections = []
    crumb: list = []
    seen: dict = {}
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
        level = 0 if fenced[i - 1] else heading_level(line.lstrip())
        if level:
            close(i - 1)
            crumb = crumb[: level - 1]
            crumb.append(line.lstrip("#").strip())
            key = ("/".join(crumb), line.strip())
            seen[key] = seen.get(key, 0) + 1
            current = {
                "id": section_id(path, crumb, line.strip(), seen[key]),
                "heading": line.strip(),
                # Which occurrence of this exact heading line this is, 1-based.
                # patch.py refuses to cut an ambiguous heading without it.
                "occurrence": seen[key],
                "level": level,
                "path": "/".join(crumb),
                "start_line": i,
                "lines": [],
            }
        else:
            if current is None:
                current = {
                    "id": section_id(path, ["(preamble)"], "(preamble)", 1),
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
