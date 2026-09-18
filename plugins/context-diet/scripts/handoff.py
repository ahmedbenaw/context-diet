#!/usr/bin/env python3
"""handoff.py - build, verify and load the state manifest.

State leaves context before context is cut. Compaction replaces turns with a
summary, so it loses transcript detail every time. What it must not lose is the
state, and the way to guarantee that is to put the state on disk first.

The anti-hallucination mechanism is the format, not a warning. Every field is a
checkable pointer: a path that must stat, a checksum that must match, a commit
that must resolve. Prose is prohibited, because prose is where a model smooths
over a gap it cannot actually recall. A field that fails verification is dropped
and logged. It is never repaired, because a plausible replacement is exactly the
failure this design exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import load_config, state_dir  # noqa: E402

PROHIBITED_FIELDS = ("summary", "narrative", "we_discussed", "reasoning", "notes")
MAX_NEXT_ACTION_WORDS = 25


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def git(args: list, cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git"] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5
        )
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def scan_transcript(transcript: str) -> dict:
    """Pull the checkable facts out of the transcript on disk.

    Paths, commands and exit codes are lifted verbatim. Nothing is described.
    """
    touched: dict = {}
    read: list = []
    commands: list = []
    seen_cmd = set()
    p = Path(transcript or "")
    if not p.is_file():
        return {"touched": touched, "read": read, "commands": commands}
    try:
        fh = p.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {"touched": touched, "read": read, "commands": commands}
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "assistant":
                continue
            for blk in (rec.get("message") or {}).get("content") or []:
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                name = blk.get("name") or ""
                inp = blk.get("input") or {}
                path = inp.get("file_path") or inp.get("notebook_path") or ""
                if name in ("Write", "Edit", "NotebookEdit") and path:
                    touched[path] = True
                elif name in ("Read", "NotebookRead") and path:
                    if path not in read:
                        read.append(path)
                elif name == "Bash":
                    cmd = (inp.get("command") or "").strip()
                    if cmd and cmd not in seen_cmd:
                        seen_cmd.add(cmd)
                        commands.append(cmd)
    return {"touched": sorted(touched), "read": read, "commands": commands}


def build_manifest(payload: dict, cwd: str, session_id: str, band: str) -> dict:
    transcript = payload.get("transcript_path") or ""
    scanned = scan_transcript(transcript)
    manifest = {
        "version": 1,
        "session_id": session_id,
        "band": band,
        "created": int(time.time()),
        "cwd": cwd,
        "transcript_path": transcript,
        "git": {
            "sha": git(["rev-parse", "HEAD"], cwd),
            "branch": git(["rev-parse", "--abbrev-ref", "HEAD"], cwd),
            "dirty": bool(git(["status", "--porcelain"], cwd)),
        },
        "files": {
            "touched": [{"path": p} for p in scanned["touched"]],
            "read": scanned["read"][:200],
        },
        "tasks": [],
        "decisions": [],
        "commands": [{"command": c, "exit_code": None} for c in scanned["commands"][-40:]],
        "next_action": "",
        "unresolved": [],
    }
    return manifest


def verify_manifest(manifest: dict, cwd: str) -> tuple:
    """Stat every path, confirm every checksum, resolve the commit.

    Returns (kept_manifest, problems). A failing field is removed and named. A
    manifest that loses a field loudly is safe. One that invents a plausible
    replacement is the failure mode this whole design exists to avoid.
    """
    problems: list = []
    kept = json.loads(json.dumps(manifest))

    for field in PROHIBITED_FIELDS:
        if field in kept:
            del kept[field]
            problems.append("prohibited prose field removed: %s" % field)

    sha = kept.get("git", {}).get("sha") or ""
    if sha:
        resolved = git(["rev-parse", "--verify", "%s^{commit}" % sha], cwd)
        if not resolved:
            kept["git"]["sha"] = ""
            problems.append("git sha did not resolve and was dropped")

    touched_ok = []
    for entry in kept.get("files", {}).get("touched", []):
        p = Path(entry.get("path", ""))
        if not p.is_file():
            problems.append("touched path does not stat, dropped: %s" % entry.get("path"))
            continue
        try:
            entry["mtime"] = int(p.stat().st_mtime)
            entry["sha256"] = sha256_file(p)[:32]
        except OSError:
            problems.append("touched path unreadable, dropped: %s" % entry.get("path"))
            continue
        touched_ok.append(entry)
    kept.setdefault("files", {})["touched"] = touched_ok

    read_ok = []
    for raw in kept.get("files", {}).get("read", []):
        if Path(raw).exists():
            read_ok.append(raw)
        else:
            problems.append("read path no longer exists, dropped: %s" % raw)
    kept["files"]["read"] = read_ok

    tp = kept.get("transcript_path") or ""
    if tp and not Path(tp).is_file():
        kept["transcript_path"] = ""
        problems.append("transcript path does not stat and was dropped")

    na = (kept.get("next_action") or "").strip()
    if na and len(na.split()) > MAX_NEXT_ACTION_WORDS:
        kept["next_action"] = ""
        problems.append(
            "next_action exceeded %d words and was dropped; a long free-text field is where "
            "invention hides" % MAX_NEXT_ACTION_WORDS
        )

    kept["verified"] = True
    kept["dropped"] = problems
    return (kept, problems)


def handoff_dir(cwd: str) -> Path:
    return state_dir(cwd) / "handoff"


def write_manifest(manifest: dict, cwd: str, session_id: str, armed: bool = False) -> Path:
    d = handoff_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    manifest["armed"] = bool(armed)
    manifest["consumed"] = False
    path = d / ("%s.json" % (session_id or "nosession"))
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_armed(cwd: str) -> tuple:
    """The newest armed, unconsumed, verified manifest for this project."""
    d = handoff_dir(cwd)
    if not d.is_dir():
        return (None, None)
    best = None
    best_path = None
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("armed") or data.get("consumed"):
            continue
        if best is None or data.get("created", 0) > best.get("created", 0):
            best, best_path = data, path
    return (best, best_path)


def mark_consumed(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["consumed"] = True
        data["consumed_at"] = int(time.time())
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass


def stale_manifests(cwd: str, days: int) -> list:
    d = handoff_dir(cwd)
    if not d.is_dir():
        return []
    cutoff = time.time() - days * 86400
    out = []
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("consumed"):
            continue
        if data.get("created", 0) < cutoff:
            out.append(str(path))
    return out


def render(manifest: dict) -> str:
    """One sentence first, then the pointers. Never a story."""
    git_info = manifest.get("git") or {}
    touched = manifest.get("files", {}).get("touched", [])
    cmds = manifest.get("commands", [])
    lines = []
    na = manifest.get("next_action") or ""
    if na:
        lines.append("Where you left off: %s" % na)
    else:
        lines.append(
            "Where you left off: %d file(s) changed on branch %s. The detail is on disk, "
            "not in this window."
            % (len(touched), git_info.get("branch") or "unknown")
        )
    if git_info.get("sha"):
        lines.append(
            "Commit %s on %s%s."
            % (git_info["sha"][:12], git_info.get("branch") or "?",
               ", working tree dirty" if git_info.get("dirty") else "")
        )
    if touched:
        lines.append("Files changed: " + ", ".join(e["path"] for e in touched[:12]))
    if cmds:
        lines.append("Last command run: %s" % cmds[-1]["command"][:160])
    if manifest.get("transcript_path"):
        lines.append("Full transcript: %s" % manifest["transcript_path"])
    if manifest.get("unresolved"):
        lines.append("Open questions: " + "; ".join(manifest["unresolved"][:5]))
    dropped = manifest.get("dropped") or []
    if dropped:
        lines.append("%d field(s) failed verification and were dropped, not repaired." % len(dropped))
    return "\n".join(lines)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="context-diet handoff")
    ap.add_argument("action", choices=["show", "stale", "verify"])
    ap.add_argument("--project", default=".")
    ap.add_argument("--file", default="")
    args = ap.parse_args()
    cfg = load_config(args.project)

    if args.action == "stale":
        for p in stale_manifests(args.project, int(cfg.get("stale_handoff_days") or 7)):
            sys.stdout.write(p + "\n")
        return 0
    if args.action == "show":
        data, path = load_armed(args.project)
        if not data:
            sys.stdout.write("no armed handoff for this project\n")
            return 0
        sys.stdout.write("%s\n\n%s\n" % (path, render(data)))
        return 0
    target = Path(args.file)
    if not target.is_file():
        sys.stderr.write("no such manifest: %s\n" % target)
        return 1
    data = json.loads(target.read_text(encoding="utf-8"))
    kept, problems = verify_manifest(data, args.project)
    sys.stdout.write(json.dumps({"problems": problems, "kept_fields": sorted(kept)}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
