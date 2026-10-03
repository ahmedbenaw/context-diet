#!/usr/bin/env python3
"""patch.py - apply an approved plan, reversibly, or refuse.

Reversibility is mechanical here, not remembered. Every file is copied to a
timestamped backup before a byte of it changes, and revert restores the backup
whole, so apply then revert is byte-identical by checksum.

Two refusals are absolute:
  1. Nothing outside the instruction layer is ever written. Instruction files,
     settings, hooks and the plugin's own directories. A plan naming a source
     file is rejected and nothing is applied.
  2. A section classified Fact is never cut without explicit confirmation,
     because losing the build command means the agent rediscovers it by grepping
     in every session from now on.

Usage:
    patch.py apply  --plan plan.json [--project DIR] [--confirm-facts]
    patch.py revert [--project DIR] [--backup TIMESTAMP]
    patch.py list   [--project DIR]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import (  # noqa: E402
    PLUGIN_ROOT, append_decision, fence_mask, heading_level, state_dir,
)

INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", "AGENTS.local.md")
# cut_section removes a markdown heading and its body. Nothing else is a
# legitimate target, so the fence refuses every other file type outright.
MARKDOWN_SUFFIXES = (".md", ".markdown")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def in_located_layer(p: Path) -> bool:
    """Is this path inside the directories the plugin is allowed to touch?"""
    if p.name in INSTRUCTION_NAMES:
        return True
    parts = p.parts
    if ".claude" in parts:
        tail = parts[parts.index(".claude") + 1 :]
        if not tail:
            return False
        if tail[0] in ("settings.json", "settings.local.json", "hooks", "context-diet.json"):
            return True
        if p.name.startswith("hookify.") and p.name.endswith(".local.md"):
            return True
    try:
        p.relative_to(PLUGIN_ROOT)
        return True
    except ValueError:
        return False


def in_instruction_layer(path: Path) -> bool:
    """The write fence. Everything outside it is refused, not warned about.

    Location alone is not enough. Allowing anything under the plugin root, or
    anything under a .claude/hooks directory, let a plan name this file, a hook,
    or the gate suite and have a markdown heading cut out of Python source. The
    fence is conjunctive: the path must sit in an allowed directory AND be a
    markdown file, which is the only thing cut_section can meaningfully edit.
    """
    p = path.resolve()
    return in_located_layer(p) and p.suffix.lower() in MARKDOWN_SUFFIXES


def relative_to_project(path: Path, project: str) -> str:
    """A path to log. Absolute paths leak the home directory into a public file.

    decisions.tsv is tracked and published, and it already carried this user's
    home layout because revert logged str(dst).
    """
    try:
        return str(Path(path).resolve().relative_to(Path(project).resolve()))
    except (ValueError, OSError):
        return Path(path).name


def backups_root(project: str) -> Path:
    return state_dir(project) / "backups"


def cut_section(text: str, heading: str, occurrence: int = 0) -> tuple:
    """Remove one section by its exact heading line.

    Returns (new_text, removed, problem). `problem` is "" on success, and names
    the reason on a refusal.

    A heading is not a unique key. Two `### Notes` under two different parents,
    or two identically named sections under one parent, all match the same
    string, and this used to take whichever came first. Silently cutting the
    wrong section is the worst thing an irreversible-feeling edit can do, so an
    ambiguous heading with no occurrence given is refused rather than guessed.
    `occurrence` is 1-based and comes from the section id extract.py assigned.
    """
    target = heading.strip()
    if not target:
        return (text, "", "no heading given")
    lines = text.splitlines(keepends=True)
    fenced = fence_mask(lines)

    matches = []
    for i, line in enumerate(lines):
        if fenced[i]:
            continue
        if line.strip() != target:
            continue
        # The line matched but is not a heading, so there is no section here to
        # cut. Treating it as level 0 made the terminator test unsatisfiable and
        # deleted everything from here to the end of the file. A non-heading
        # match is a miss, not a cut.
        level = heading_level(line.lstrip())
        if level:
            matches.append((i, level))

    if not matches:
        return (text, "", "no section with that heading")
    if len(matches) > 1 and not occurrence:
        return (text, "", "heading appears %d times and the plan did not say which"
                % len(matches))
    index = (occurrence or 1) - 1
    if index < 0 or index >= len(matches):
        return (text, "", "occurrence %d requested but the heading appears %d time(s)"
                % (occurrence, len(matches)))
    start, level = matches[index]
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if fenced[j]:
            continue
        this_level = heading_level(lines[j].lstrip())
        if 0 < this_level <= level:
            end = j
            break
    removed = "".join(lines[start:end])
    return ("".join(lines[:start] + lines[end:]), removed, "")


def do_apply(args) -> int:
    plan_path = Path(args.plan)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write("cannot read plan: %s\n" % exc)
        return 2

    cuts = plan.get("cuts") or []
    if not cuts:
        sys.stdout.write("plan has no cuts; nothing to apply\n")
        return 0

    # Validate the whole plan before touching anything. A plan that is half
    # applied is worse than one that is refused.
    problems = []
    for cut in cuts:
        target = Path(cut.get("file", ""))
        if not target.is_file():
            problems.append("no such file: %s" % target)
            continue
        if not in_instruction_layer(target):
            problems.append("outside the instruction layer, refused: %s" % target)
        if cut.get("bucket") == "Fact" and not args.confirm_facts:
            problems.append(
                "%s is classified Fact; cutting it needs --confirm-facts" % cut.get("file")
            )
        if "tokens" not in cut:
            problems.append("cut has no token figure: %s" % cut.get("section_heading"))
        # Decode every target up front. read_text is strict and used to run
        # inside the mutation loop, so one undecodable byte in a later file left
        # an earlier file already rewritten with no index to revert from.
        try:
            target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append("cannot read as UTF-8, refused: %s (%s)" % (target, type(exc).__name__))
    if problems:
        for p in problems:
            sys.stderr.write("refused: %s\n" % p)
        sys.stderr.write("nothing was applied\n")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = backups_root(args.project) / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    index = {"created": stamp, "files": []}

    # Back every distinct file up once, before anything is modified. Backing up
    # per cut means the second cut to one file copies the already-modified text
    # over the original, and the backup silently stops being a backup.
    targets = []
    for cut in cuts:
        resolved = Path(cut["file"]).resolve()
        if resolved not in targets:
            targets.append(resolved)
    for target in targets:
        safe_name = str(target).replace("/", "_").lstrip("_")
        shutil.copy2(target, backup_dir / safe_name)
        index["files"].append(
            {"original": str(target), "backup": safe_name, "sha256_before": sha256(target)}
        )

    # Write the index before the first mutation, not after the last one. A
    # crash between the two used to leave modified files beside a backup set
    # with no index, which revert reports as "backup set has no index".
    (backup_dir / "INDEX.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    applied = 0
    for cut in cuts:
        target = Path(cut["file"]).resolve()
        text = target.read_text(encoding="utf-8")
        new_text, removed, problem = cut_section(
            text, cut.get("section_heading", ""), int(cut.get("occurrence") or 0))
        if problem or not removed:
            sys.stderr.write(
                "skipped %s in %s: %s\n"
                % (cut.get("section_heading"), target, problem or "nothing was removed"))
            continue
        target.write_text(new_text, encoding="utf-8")
        applied += 1
        append_decision(
            "apply",
            "%s %s" % (target.name, cut.get("section_heading")),
            str(cut.get("tokens")),
            "bucket=%s reason=%s backup=%s" % (cut.get("bucket"), cut.get("reason"), stamp),
        )

    sys.stdout.write(
        "applied %d of %d cut(s). Backup: %s\nRevert with: patch.py revert --project %s\n"
        % (applied, len(cuts), backup_dir, args.project)
    )
    return 0


def do_revert(args) -> int:
    root = backups_root(args.project)
    if not root.is_dir():
        sys.stderr.write("no backups for this project\n")
        return 1
    sets = sorted(p for p in root.iterdir() if p.is_dir())
    if not sets:
        sys.stderr.write("no backup sets\n")
        return 1
    chosen = root / args.backup if args.backup else sets[-1]
    index_path = chosen / "INDEX.json"
    if not index_path.is_file():
        sys.stderr.write("backup set has no index: %s\n" % chosen)
        return 1
    index = json.loads(index_path.read_text(encoding="utf-8"))

    # Validate the whole index before writing a single byte. An INDEX.json is
    # just a file on disk, and a repo can ship one, so its paths are input, not
    # instructions: an unchecked `original` restored to any absolute path on the
    # machine, and the checksum afterwards only compared the written file to the
    # blob that had just been written, which assures nothing. Restore is
    # all-or-nothing, and every entry is checked first.
    planned = []
    problems = []
    for entry in index.get("files") or []:
        name = str(entry.get("backup", ""))
        if not name or Path(name).name != name or name in (".", ".."):
            problems.append("backup name is not a plain filename: %r" % name)
            continue
        src = chosen / name
        if not src.is_file():
            problems.append("missing backup file: %s" % src)
            continue
        dst = Path(str(entry.get("original", ""))).resolve()
        if not in_instruction_layer(dst):
            problems.append("restore target is outside the instruction layer: %s" % dst)
            continue
        recorded = str(entry.get("sha256_before", ""))
        actual = sha256(src)
        if not recorded or actual != recorded:
            problems.append("backup blob does not match its recorded checksum: %s" % src)
            continue
        planned.append((src, dst, recorded))

    if problems:
        for p in problems:
            sys.stderr.write("refused: %s\n" % p)
        sys.stderr.write("nothing was restored\n")
        return 1

    restored = 0
    for src, dst, recorded in planned:
        shutil.copy2(src, dst)
        if sha256(dst) != recorded:
            sys.stderr.write("restored file does not match its recorded checksum: %s\n" % dst)
            return 1
        restored += 1
        append_decision("revert", relative_to_project(dst, args.project), recorded[:12],
                        "from %s" % chosen.name)
    sys.stdout.write("restored %d file(s) from %s, each verified by checksum\n"
                     % (restored, chosen.name))
    return 0


def do_list(args) -> int:
    root = backups_root(args.project)
    if not root.is_dir():
        sys.stdout.write("no backups\n")
        return 0
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        index = d / "INDEX.json"
        count = 0
        if index.is_file():
            try:
                count = len(json.loads(index.read_text(encoding="utf-8")).get("files", []))
            except ValueError:
                count = 0
        sys.stdout.write("%s  %d file(s)\n" % (d.name, count))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet patch")
    sub = ap.add_subparsers(dest="action", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--plan", required=True)
    a.add_argument("--project", default=".")
    a.add_argument("--confirm-facts", action="store_true")
    a.set_defaults(fn=do_apply)
    r = sub.add_parser("revert")
    r.add_argument("--project", default=".")
    r.add_argument("--backup", default="")
    r.set_defaults(fn=do_revert)
    ls = sub.add_parser("list")
    ls.add_argument("--project", default=".")
    ls.set_defaults(fn=do_list)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
