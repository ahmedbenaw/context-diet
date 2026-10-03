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

def audit_home() -> Path:
    """The home directory being audited. CONTEXT_DIET_HOME overrides it for tests.

    Without the seam the inert-rule scan can only read the developer's own home,
    so the gate had to assert that particular machine's stray rules and went red
    on any clean checkout.
    """
    override = os.environ.get("CONTEXT_DIET_HOME")
    return Path(override) if override else Path.home()


HOME = audit_home() / ".claude"


def expand(command: str) -> str:
    """Normalise a hook command so two spellings of one path compare equal.

    A hook registered once by absolute path and once through $HOME is registered
    twice and runs twice. Only normalisation makes that visible.
    """
    text = os.path.expandvars(command or "")
    text = text.replace(str(audit_home()), "~")
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


def installed_plugin_paths() -> dict:
    """name@marketplace -> install directory, from installed_plugins.json."""
    path = HOME / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, entries in (data.get("plugins") or {}).items():
        for entry in entries if isinstance(entries, list) else [entries]:
            if isinstance(entry, dict) and entry.get("installPath"):
                out.setdefault(key, Path(entry["installPath"]).expanduser())
    return out


def local_marketplace_dirs() -> dict:
    """name@marketplace -> plugin directory, for marketplaces added from a path.

    Read through each marketplace's manifest, never by walking the directory: a
    local marketplace often sits inside a working repo, and walking it would
    report a hooks.json belonging to some library in a virtualenv as if it were
    a registered hook.
    """
    out = {}
    for market, entry in (claude_settings().get("extraKnownMarketplaces") or {}).items():
        source = entry.get("source") if isinstance(entry, dict) else None
        path = source.get("path") if isinstance(source, dict) else source
        if not isinstance(path, str) or not path:
            continue
        base = Path(path).expanduser()
        manifest = base / ".claude-plugin" / "marketplace.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for plugin in data.get("plugins") or []:
            if not isinstance(plugin, dict) or not isinstance(plugin.get("source"), str):
                continue
            candidate = (base / plugin["source"]).resolve()
            if candidate.is_dir() and plugin.get("name"):
                out["%s@%s" % (plugin["name"], market)] = candidate
    return out


SKIPPED: dict = {}


def collect_plugin_hooks() -> list:
    """Hooks from plugins that are installed and enabled, and nothing else.

    This used to walk every hooks.json under the marketplace tree. A marketplace
    is a catalogue: on this machine it held 10 hooks.json files for 4 installed
    plugins, so hooks that never run were counted in the chain and could be
    reported as duplicates. enabled_plugin_names() existed for this and had no
    caller. Each plugin is now read from its own install path, falling back to a
    local marketplace's source directory when the install path is gone.

    Plugins the desktop app syncs from the account are unpacked per session and
    are not visible from here; the report says so rather than implying it saw
    every hook.
    """
    rows = []
    seen = set()
    enabled = set(enabled_plugin_names())
    installed = installed_plugin_paths()
    local = local_marketplace_dirs()
    SKIPPED.clear()
    SKIPPED["installed_not_enabled"] = sorted(k for k in installed if k not in enabled)
    SKIPPED["enabled_not_found"] = []
    for key in sorted(enabled):
        plugin_dir = installed.get(key)
        if plugin_dir is None or not plugin_dir.is_dir():
            plugin_dir = local.get(key)
        if plugin_dir is None or not plugin_dir.is_dir():
            SKIPPED["enabled_not_found"].append(key)
            continue
        hooks_json = plugin_dir / "hooks" / "hooks.json"
        if hooks_json.is_file():
            for row in _read_hooks_file(hooks_json, seen):
                row["plugin"] = key
                rows.append(row)
    catalogue = HOME / "plugins" / "marketplaces"
    read = {r["source"] for r in rows}
    SKIPPED["catalogue_hook_files_not_loaded"] = (
        sum(1 for h in catalogue.rglob("hooks/hooks.json") if str(h) not in read)
        if catalogue.is_dir() else 0)
    return rows


def collect_skill_hooks() -> list:
    """Hooks shipped inside a skill folder under ~/.claude/skills.

    The desktop app loads a skill that carries hooks/hooks.json as a plugin of
    its own. Registering the same scripts again in settings.json then runs them
    twice: on this machine the anti-ai-design-style Stop check ran twice per
    Stop, about 6.8 s each, and nothing reported it because the two command
    strings differ only in the interpreter path.
    """
    rows = []
    seen: set = set()
    skills = HOME / "skills"
    if not skills.is_dir():
        return rows
    for hooks_json in sorted(skills.glob("*/hooks/hooks.json")):
        for row in _read_hooks_file(hooks_json, seen):
            row["plugin"] = "%s (skill folder, loaded as a plugin by the desktop app)" % (
                hooks_json.parent.parent.name)
            rows.append(row)
    return rows


SCRIPT_SUFFIXES = (".py", ".sh", ".js", ".mjs", ".cjs", ".ts", ".rb")


def duplicate_key(row: dict) -> tuple:
    """Two registrations are the same hook if they run the same script.

    Comparing whole command strings missed `python3 x.py` against
    `/opt/homebrew/bin/python3 x.py`. The script path, after the plugin root
    and $HOME are resolved, is what decides whether one run is redundant.
    """
    words = [w.strip("'\"") for w in (row.get("normalized") or "").split()]
    idx = [i for i, w in enumerate(words) if w.endswith(SCRIPT_SUFFIXES)]
    # The arguments after the script stay in the key: one script called as
    # `worker-service.cjs start` and as `worker-service.cjs hook ... context`
    # is two hooks, not one registered twice.
    target = " ".join(words[idx[-1]:]) if idx else row.get("normalized")
    return (row["event"], row["matcher"] or "", target)


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
    for candidate in sorted(audit_home().glob(".claude/hookify.*.local.md")):
        findings.append(
            {
                "rule": str(candidate),
                "loads_only_when_cwd_is": str(audit_home()),
                "why": "config_loader globs .claude/hookify.*.local.md relative to cwd",
            }
        )
    for candidate in sorted(audit_home().glob("hookify.*.local.md")):
        findings.append(
            {
                "rule": str(candidate),
                "loads_only_when_cwd_is": str(audit_home()),
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
    rows = collect_settings_hooks() + collect_plugin_hooks() + collect_skill_hooks()
    measured = census_h3(Path(args.census))

    by_event: dict = defaultdict(list)
    for r in rows:
        by_event[r["event"]].append(r)

    duplicates = []
    counts: dict = defaultdict(list)
    for r in rows:
        counts[duplicate_key(r)].append(r)
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
        "plugins_not_audited": dict(SKIPPED),
        "scope_note": "plugins synced from the account by the desktop app are unpacked per "
                      "session and are not visible to this audit",
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
