#!/usr/bin/env python3
"""inventory_audit.py - price the plugin inventory that every message carries.

The census has said since H2 that the largest fixed cost in a session is plugin
inventory, not instruction prose. This puts a number on it per plugin and sets
that number beside how often the plugin was actually used. Cost times zero use
is a disable candidate.

Three rules, because this is the number with the largest blast radius in the
project and the easiest one to overstate.

  Price what is loaded, not what is installed. A plugin on disk that no session
  enables costs nothing per message. The two counts differ by a wide margin on
  this machine and reporting the larger one would be a scare figure.

  Measure the text, do not multiply a constant. The skills index carries each
  skill's name and description, so this reads them and counts them. A flat
  guess per skill cannot be checked against anything.

  Say which usage counter produced a number. The client keeps its own per-plugin
  counter and the transcripts record tool calls. They count different things, so
  both are reported and a disagreement is named rather than resolved.

It writes nothing outside --out. Disabling is a settings change Ben applies and
can undo.

Usage:
    inventory_audit.py [--plugins DIR] [--projects DIR] [--out DIR] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import Tokenizer, load_config  # noqa: E402

# Calibration, not a guess, and not a model of what the index contains.
#
# Reading each skill's full frontmatter description and calling that its cost in
# the index overstates it by a factor of seven: the description carries the
# worked examples that make a skill fire, and the index does not. Four candidate
# models were tried against a real reading and all four missed, the closest by
# 44% and the furthest by 616%. So the per-entry cost is measured instead.
#
# Source: mcp__ccd_session_mgmt__get_usage on a live session, 20 Sept 2026,
# reporting Skills 20,014 tokens and Custom agents 12,320 against an inventory
# counted here at 1,303 skills and 84 agents. That gives 15.4 tokens per skill
# and 146.7 per agent. MCP tools read 20,465 in the same breakdown.
#
# Pass --calibration to replace these with a fresh reading. Without a
# calibration the audit reports counts and shares and refuses to print a token
# figure, because a token figure with no provenance is what this whole project
# exists to argue against.
CALIBRATION = {
    "source": "mcp__ccd_session_mgmt__get_usage, live session, 2026-09-20",
    "skills_index_tokens": 20014,
    "skills_counted": 1303,
    "agents_index_tokens": 12320,
    "agents_counted": 84,
    "mcp_tools_tokens": 20465,
}


def read_head(path: Path, cap: int = 8000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:cap]
    except OSError:
        return ""


def frontmatter_field(text: str, field: str) -> str:
    """One frontmatter value, folded lines included.

    A skill description routinely runs to several wrapped lines and holds the
    worked examples that make it fire. Reading only the first line would price a
    2,000-character description at 60 characters.
    """
    m = re.search(r"^---\s*$(.*?)^---\s*$", text, re.M | re.S)
    block = m.group(1) if m else text[:2000]
    m = re.search(r"^%s:\s*(.*?)(?=^[A-Za-z_][\w-]*:\s|\Z)" % re.escape(field),
                  block, re.M | re.S)
    return (m.group(1) if m else "").strip()


def plugin_roots(base: Path) -> dict:
    """Every plugin directory, found by its own manifest.

    Anchored on .claude-plugin/plugin.json rather than by walking for anything
    that looks like a plugin, because a plugin's own test fixtures can contain a
    nested manifest that no session ever loads.
    """
    found: dict = {}
    for manifest in base.rglob(".claude-plugin/plugin.json"):
        root = manifest.parent.parent
        # A manifest nested inside another plugin's tree is that plugin's
        # fixture, not a second plugin.
        if any(part in ("tests", "fixtures", "examples", "node_modules")
               for part in root.relative_to(base).parts):
            continue
        try:
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        except Exception:
            name = None
        found.setdefault(name or root.name, root)
    return found


def price(root: Path, tok: Tokenizer) -> dict:
    """What this plugin adds to every message, by reading what is carried."""
    skills = sorted(root.glob("skills/*/SKILL.md"))
    agents = sorted(root.glob("agents/*.md"))

    # Root only. Globbing recursively picks up nested configs that are never
    # loaded, which is the bug that made one plugin report seventy MCP servers.
    servers = []
    root_mcp = root / ".mcp.json"
    if root_mcp.is_file():
        try:
            servers = sorted((json.loads(root_mcp.read_text(encoding="utf-8"))
                              .get("mcpServers") or {}))
        except Exception:
            servers = []

    # The counts are the measurement. Tokens are applied afterwards from the
    # calibration, so a wrong rate can be corrected by re-reading one number
    # rather than re-running the audit against a different model of the index.
    return {
        "skills": len(skills),
        "agents": len(agents),
        "mcp_servers": len(servers),
        "skill_names": [s.parent.name for s in skills],
        "agent_names": [a.stem for a in agents],
        "server_names": servers,
    }


def rates(cal: dict) -> tuple:
    """Tokens per skill and per agent, from the calibration, or None."""
    sk = cal.get("skills_counted") or 0
    ag = cal.get("agents_counted") or 0
    return ((cal.get("skills_index_tokens", 0) / sk) if sk else None,
            (cal.get("agents_index_tokens", 0) / ag) if ag else None)


def client_usage(claude_json: Path) -> tuple:
    """The client's own per-plugin and per-skill counters, and the enabled set.

    Enablement does not live in settings.json alone. Plugins synced from the
    account appear here and nowhere in enabledPlugins, so reading only
    settings.json reports six enabled plugins on a machine running two hundred.
    """
    try:
        data = json.loads(claude_json.read_text(encoding="utf-8"))
    except Exception:
        return ({}, {}, set())
    plugins = data.get("pluginUsage") or {}
    skills = data.get("skillUsage") or {}
    enabled = {key.split("@")[0] for key in plugins}
    return (plugins, skills, enabled)


def transcript_usage(projects: Path) -> tuple:
    """Invocations this machine's transcripts actually record.

    A second, independent count. It sees only what a tool_use block names, so it
    is a floor rather than a total, and it is reported beside the client's
    counter rather than instead of it.
    """
    skills: Counter = Counter()
    agents: Counter = Counter()
    servers: Counter = Counter()
    sessions = 0
    for path in projects.rglob("*.jsonl"):
        sessions += 1
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"tool_use"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue          # a live file's truncated last line
                content = ((rec.get("message") or {}).get("content"))
                if not isinstance(content, list):
                    continue
                for blk in content:
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    name = blk.get("name") or ""
                    data = blk.get("input") or {}
                    if name == "Skill":
                        skills[str(data.get("skill") or "").split(":")[0]] += 1
                    elif name in ("Agent", "Task"):
                        agents[str(data.get("subagent_type") or "").split(":")[0]] += 1
                    elif name.startswith("mcp__"):
                        parts = name.split("__")
                        if len(parts) > 1:
                            servers[parts[1]] += 1
    return (skills, agents, servers, sessions)


def build_rows(roots: dict, tok: Tokenizer, pu: dict, su: dict, enabled: set,
               t_skills: Counter, t_agents: Counter, t_servers: Counter,
               cal: dict) -> list:
    per_skill, per_agent = rates(cal)
    rows = []
    for name, root in sorted(roots.items()):
        p = price(root, tok)
        key = next((k for k in pu if k.split("@")[0] == name), None)
        client = int((pu.get(key) or {}).get("usageCount") or 0) if key else 0
        client += sum(int((su.get(s) or {}).get("usageCount") or 0)
                      for s in su if s.split(":")[0] == name)
        seen = (t_agents.get(name, 0) + sum(t_skills.get(n, 0) for n in [name])
                + sum(t_servers.get(s, 0) for s in p["server_names"]))
        rows.append({
            "plugin": name,
            "enabled": name in enabled or key is not None,
            "skills": p["skills"], "agents": p["agents"], "mcp_servers": p["mcp_servers"],
            "skill_tokens": round(p["skills"] * per_skill) if per_skill else None,
            "agent_tokens": round(p["agents"] * per_agent) if per_agent else None,
            "tokens_per_turn": (round(p["skills"] * per_skill + p["agents"] * per_agent)
                                if per_skill and per_agent else None),
            "uses_client": client,
            "uses_transcripts": seen,
        })
    rows.sort(key=lambda r: (-(r["tokens_per_turn"] or 0), -r["skills"], -r["agents"]))
    return rows


def render(rows: list, tok: Tokenizer, sessions: int, disagree: list, cal: dict) -> str:
    live = [r for r in rows if r["enabled"]]
    idle = [r for r in live if r["uses_client"] == 0 and r["uses_transcripts"] == 0]
    per_skill, per_agent = rates(cal)
    priced = per_skill is not None and per_agent is not None

    n_skills = sum(r["skills"] for r in live)
    n_agents = sum(r["agents"] for r in live)
    n_servers = sum(r["mcp_servers"] for r in live)
    idle_skills = sum(r["skills"] for r in idle)
    idle_agents = sum(r["agents"] for r in idle)

    out = []
    w = out.append
    w("context-diet inventory audit")
    w("")
    w("%d plugin(s) on disk, %d of them loaded into every message. The other %d are installed "
      "but not loaded and cost nothing per message, so they are left out of every figure below."
      % (len(rows), len(live), len(rows) - len(live)))
    w("")
    w("Counted, which is the measurement: %s skills, %s agents and %s MCP server(s) across the "
      "loaded plugins." % (format(n_skills, ","), format(n_agents, ","), format(n_servers, ",")))
    w("")

    if not priced:
        w("No calibration, so no token figures. The number of tokens an index entry costs is not "
          "derivable from the file it came from: reading each skill's full description and "
          "calling that its index cost overstates it sevenfold, because the description carries "
          "the examples that make the skill fire and the index does not.")
        w("")
        w("Get one by running mcp__ccd_session_mgmt__get_usage in the desktop app and passing its "
          "Skills and Custom agents readings with --calibration, alongside the counts above.")
    else:
        total = round(n_skills * per_skill + n_agents * per_agent)
        wasted = round(idle_skills * per_skill + idle_agents * per_agent)
        mcp = int(cal.get("mcp_tools_tokens") or 0)
        w("Priced at %.1f tokens per skill and %.1f per agent, calibrated against %s."
          % (per_skill, per_agent, cal.get("source") or "an unnamed reading"))
        w("")
        w("  skills index      %9s tokens" % format(round(n_skills * per_skill), ","))
        w("  agent list        %9s tokens" % format(round(n_agents * per_agent), ","))
        if mcp:
            w("  MCP tool names    %9s tokens  (read directly, not apportioned)" % format(mcp, ","))
        w("  %s" % ("-" * 28))
        w("  every message     %9s tokens" % format(total + mcp, ","))
        w("")
        w("%s of that (%.0f%% of the skill and agent index) comes from %d plugin(s) with no "
          "recorded use at all: %s skills and %s agents that have never fired."
          % (format(wasted, ","), (100.0 * wasted / total) if total else 0.0,
             len(idle), format(idle_skills, ","), format(idle_agents, ",")))
        w("")
        w("This is per message. A hundred-message session pays it a hundred times, and a cold "
          "turn pays it at the write price rather than the read price.")
    w("")

    header = "%-38s %6s %6s %4s %10s %8s %8s" % (
        "plugin", "skills", "agents", "mcp", "tok/msg", "uses", "in logs")
    w(header)
    w("-" * len(header))
    for r in live[:30]:
        if not (r["skills"] or r["agents"]):
            continue
        w("%-38s %6d %6d %4d %10s %8d %8d" % (
            r["plugin"][:38], r["skills"], r["agents"], r["mcp_servers"],
            format(r["tokens_per_turn"], ",") if priced else "-",
            r["uses_client"], r["uses_transcripts"]))
    more = len([x for x in live if x["skills"] or x["agents"]]) - 30
    if more > 0:
        w("... %d more; --json has every row" % more)
    w("")
    w('"uses" is the client\'s own counter. "in logs" is what %d transcript(s) record, which '
      "sees only what a tool_use block names and is therefore a floor." % sessions)
    if disagree:
        w("%d plugin(s) where the two counters disagree, which means they are not counting the "
          "same event: %s. Neither is corrected against the other."
          % (len(disagree), ", ".join(disagree[:5])))
    w("")
    w("This proposes and writes nothing outside the output directory. Disabling a plugin is a "
      "settings change Ben applies, and it is reversible.")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugins", default=str(Path.home() / ".claude" / "plugins"))
    ap.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--claude-json", default=str(Path.home() / ".claude.json"))
    ap.add_argument("--out", default="")
    ap.add_argument("--project", default=os.getcwd())
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--calibration", default="",
                    help="JSON file holding a fresh get_usage reading and the counts it was "
                         "taken against; replaces the built-in one")
    ap.add_argument("--no-calibration", action="store_true",
                    help="report counts only and print no token figure")
    args = ap.parse_args()

    cal = dict(CALIBRATION)
    if args.no_calibration:
        cal = {}
    elif args.calibration:
        try:
            cal = json.loads(Path(args.calibration).read_text(encoding="utf-8"))
        except Exception as exc:
            sys.stderr.write("could not read the calibration: %s\n" % exc)
            return 1

    base = Path(args.plugins)
    if not base.is_dir():
        sys.stderr.write("no plugin directory at %s\n" % base)
        return 1
    cfg = load_config(args.project)
    tok = Tokenizer(cfg.get("tokenizer", "auto"))

    roots = plugin_roots(base)
    pu, su, enabled = client_usage(Path(args.claude_json))
    projects = Path(args.projects)
    t_skills, t_agents, t_servers, sessions = (
        transcript_usage(projects) if projects.is_dir() else (Counter(), Counter(), Counter(), 0))

    rows = build_rows(roots, tok, pu, su, enabled, t_skills, t_agents, t_servers, cal)
    disagree = [r["plugin"] for r in rows
                if r["enabled"] and bool(r["uses_client"]) != bool(r["uses_transcripts"])]

    text = render(rows, tok, sessions, disagree, cal)
    if args.json:
        sys.stdout.write(json.dumps(
            {"calibration": cal, "sessions_read": sessions, "rows": rows,
             "counters_disagree": disagree}, indent=1) + "\n")
    else:
        sys.stdout.write(text + "\n")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "inventory.md").write_text(text + "\n", encoding="utf-8")
        (out / "inventory.json").write_text(json.dumps(
            {"calibration": cal, "sessions_read": sessions, "rows": rows,
             "counters_disagree": disagree}, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
