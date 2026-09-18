#!/usr/bin/env python3
"""hook_audit.py - what the hook chain actually is, and what it costs.

Parses the user's settings and every enabled plugin's hooks.json, then joins the
census H3 column so worst-case timeouts sit beside measured wall-clock. Reports
duplicate registrations and hookify rules that can never load.

Read-only. Standard library only.

Usage:
    hook_audit.py [--census census.tsv] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import claude_settings, load_config  # noqa: E402

HOME = Path.home() / ".claude"


def expand(command: str) -> str:
    """Normalise a hook command so two spellings of one path compare equal.

    A hook registered once by absolute path and once through $HOME is registered
    twice and runs twice. Only normalisation makes that visible.
    """
    text = os.path.expandvars(command or "")
    text = text.replace(str(Path.home()), "~")
    return " ".join(text.split())


def collect_settings_hooks() -> list:
    rows = []
    settings = claude_settings()
    for event, matchers in (settings.get("hooks") or {}).items():
        for matcher in matchers or []:
            pattern = matcher.get("matcher", "")
            for hook in matcher.get("hooks") or []:
                rows.append(
                    {
                        "source": "~/.claude/settings.json",
                        "event": event,
                        "matcher": pattern,
                        "command": hook.get("command", ""),
                        "normalized": expand(hook.get("command", "")),
                        "timeout": hook.get("timeout"),
                    }
                )
    return rows


def enabled_plugin_names() -> list:
    settings = claude_settings()
    enabled = settings.get("enabledPlugins") or {}
    if isinstance(enabled, dict):
        return sorted(k for k, v in enabled.items() if v)
    if isinstance(enabled, list):
        return sorted(enabled)
    return []


def short(command: str, width: int = 88) -> str:
    """A hook command can be a 2 KB PATH export. Show enough to identify it."""
    text = " ".join((command or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def collect_plugin_hooks() -> list:
    """Hooks from installed plugins.

    Only the marketplace tree is scanned. The cache tree holds several extracted
    versions of the same plugin, and counting those as duplicate registrations
    would report a packaging detail as a live cost.
    """
    rows = []
    roots = [HOME / "plugins" / "marketplaces"]
    seen = set()

    # A marketplace added from a local path is never copied into the cache, so a
    # scan of the cache alone misses every plugin installed that way. Its
    # plugins are read through its manifest, never by walking the directory: a
    # local marketplace often sits inside a working repo, and walking it would
    # report a hooks.json belonging to some library in a virtualenv as if it
    # were a registered hook.
    local_plugin_dirs = []
    for entry in (claude_settings().get("extraKnownMarketplaces") or {}).values():
        source = entry.get("source") if isinstance(entry, dict) else None
        path = source.get("path") if isinstance(source, dict) else source
        if not isinstance(path, str) or not path:
            continue
        base = Path(path).expanduser()
        manifest = base / ".claude-plugin" / "marketplace.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for plugin in data.get("plugins") or []:
            src = plugin.get("source") if isinstance(plugin, dict) else None
            if isinstance(src, str):
                candidate = (base / src).resolve()
                if candidate.is_dir():
                    local_plugin_dirs.append(candidate)

    for plugin_dir in local_plugin_dirs:
        hooks_json = plugin_dir / "hooks" / "hooks.json"
        if hooks_json.is_file():
            rows.extend(_read_hooks_file(hooks_json, seen))
    for root in roots:
        if not root.is_dir():
            continue
        for hooks_json in root.rglob("hooks/hooks.json"):
            rows.extend(_read_hooks_file(hooks_json, seen))
    return rows


def _read_hooks_file(hooks_json: Path, seen: set) -> list:
    """Parse one hooks.json into rows, resolving the plugin root variable."""
    rows = []
    key = str(hooks_json.resolve())
    if key in seen:
        return rows
    seen.add(key)
    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rows
    events = data.get("hooks") if isinstance(data.get("hooks"), dict) else data
    if not isinstance(events, dict):
        return rows
    plugin_root = hooks_json.parent.parent
    for event, matchers in events.items():
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            for hook in (matcher or {}).get("hooks") or []:
                cmd = hook.get("command", "")
                # ${CLAUDE_PLUGIN_ROOT} is literal text in the file. Two plugins
                # shipping the same command text are not running the same
                # script, so the variable is resolved before comparison.
                resolved = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                resolved = resolved.replace("$CLAUDE_PLUGIN_ROOT", str(plugin_root))
                rows.append(
                    {
                        "source": str(hooks_json),
                        "event": event,
                        "matcher": (matcher or {}).get("matcher", ""),
                        "command": cmd,
                        "normalized": expand(resolved),
                        "timeout": hook.get("timeout"),
                    }
                )
    return rows


def inert_hookify_rules() -> list:
    """Rules that can never load, because hookify globs the cwd only.

    A rule in the home directory loads only when the session's cwd is the home
    directory. Everywhere else it is a file that looks active and is not.
    """
    findings = []
    for candidate in sorted(Path.home().glob(".claude/hookify.*.local.md")):
        findings.append(
            {
                "rule": str(candidate),
                "loads_only_when_cwd_is": str(Path.home()),
                "why": "config_loader globs .claude/hookify.*.local.md relative to cwd",
            }
        )
    for candidate in sorted(Path.home().glob("hookify.*.local.md")):
        findings.append(
            {
                "rule": str(candidate),
                "loads_only_when_cwd_is": str(Path.home()),
                "why": "not under a project .claude directory",
            }
        )
    return findings


def census_h3(census: Path) -> dict:
    """Measured hook wall-clock per session, keyed by top event."""
    if not census.is_file():
        return {}
    by_event: dict = defaultdict(list)
    try:
        with census.open(encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            idx = {name: i for i, name in enumerate(header)}
            for line in fh:
                cells = line.rstrip("\n").split("\t")
                if len(cells) != len(header):
                    continue
                event = cells[idx.get("h3_hook_ms_top_event", -1)] if "h3_hook_ms_top_event" in idx else ""
                try:
                    value = int(cells[idx["h3_hook_ms_top_value"]])
                except (KeyError, ValueError):
                    continue
                if event:
                    by_event[event].append(value)
    except OSError:
        return {}
    return {e: sorted(v)[len(v) // 2] for e, v in by_event.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet hook audit")
    ap.add_argument("--census", default="census.tsv")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(".")
    rows = collect_settings_hooks() + collect_plugin_hooks()
    measured = census_h3(Path(args.census))

    by_event: dict = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    duplicates = []
    counts: dict = defaultdict(list)
    for r in rows:
        counts[(r["event"], r["matcher"], r["normalized"])].append(r)
    limit = int(cfg.get("duplicate_hook_registrations") or 1)
    for key, group in counts.items():
        if len(group) > limit:
            duplicates.append(
                {
                    "event": key[0],
                    "command": key[2],
                    "count": len(group),
                    "paths": [g["command"] for g in group],
                    "sources": sorted({g["source"] for g in group}),
                }
            )

    inert = inert_hookify_rules()
    report = {
        "events": {
            event: {
                "count": len(items),
                "worst_case_timeout_s": sum(int(i["timeout"] or 60) for i in items),
                "measured_median_ms": measured.get(event),
                "hooks": [
                    {"command": i["command"], "timeout": i["timeout"], "source": i["source"],
                     "matcher": i["matcher"]}
                    for i in items
                ],
            }
            for event, items in sorted(by_event.items())
        },
        "duplicate_registrations": duplicates,
        "inert_hookify_rules": inert,
    }

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0

    sys.stdout.write("context-diet hook audit\n\n")
    sys.stdout.write("%-18s %6s %14s %16s\n"
                     % ("event", "hooks", "worst case s", "measured ms"))
    sys.stdout.write("-" * 58 + "\n")
    for event, data in report["events"].items():
        m = data["measured_median_ms"]
        sys.stdout.write("%-18s %6d %14d %16s\n"
                         % (event, data["count"], data["worst_case_timeout_s"],
                            format(m, ",") if m is not None else "not measured"))
    sys.stdout.write("\n")
    if duplicates:
        sys.stdout.write("duplicate registrations (each one runs every time):\n")
        for d in duplicates:
            sys.stdout.write("  %s x%d on %s\n" % (short(d["command"]), d["count"], d["event"]))
            for p in d["paths"]:
                sys.stdout.write("      %s\n" % short(p))
        sys.stdout.write("\n")
    else:
        sys.stdout.write("duplicate registrations: none\n\n")
    if inert:
        sys.stdout.write("hookify rules that never load:\n")
        for i in inert:
            sys.stdout.write("  %s\n      loads only when cwd is %s (%s)\n"
                             % (i["rule"], i["loads_only_when_cwd_is"], i["why"]))
        sys.stdout.write("\n")
    else:
        sys.stdout.write("hookify rules that never load: none\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
