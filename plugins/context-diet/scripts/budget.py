#!/usr/bin/env python3
"""budget.py - what the context layer costs, per file and per section.

Counting is arithmetic, so it happens here and not in a model. Every number
carries the name of the tokeniser that produced it.

Walks: the global CLAUDE.md, every project CLAUDE.md in scope, the skills index,
the agents directory, MCP server schemas as configured, and the plugin's own
files when asked. Emits a table and JSON.

Usage:
    budget.py [--project DIR] [--json] [--path P ...] [--max-lines N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import Tokenizer, load_config  # noqa: E402

SECTION_PREFIXES = ("#", "##", "###", "####")


def split_sections(text: str) -> list:
    """Split markdown into addressable sections by heading.

    Text before the first heading is its own section so nothing is uncounted.
    """
    lines = text.splitlines()
    sections = []
    current = {"heading": "(preamble)", "line": 1, "body": []}
    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("#") and stripped.lstrip("#").startswith(" "):
            if current["body"] or current["heading"] != "(preamble)":
                sections.append(current)
            current = {"heading": stripped.rstrip(), "line": i, "body": []}
        else:
            current["body"].append(line)
    sections.append(current)
    return [s for s in sections if s["body"] or s["heading"] != "(preamble)"]


def measure_file(path: Path, tok: Tokenizer, index_only: bool = False) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"path": str(path), "error": str(exc), "tokens": 0, "bytes": 0, "lines": 0,
                "sections": []}
    full_lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    if index_only:
        head = frontmatter(text)
        return {
            "path": str(path),
            "tokens": tok.count(head),
            "bytes": len(head.encode("utf-8")),
            "lines": full_lines,
            "body_tokens": tok.count(text) - tok.count(head),
            "sections": [],
        }
    sections = []
    if path.suffix.lower() in (".md", ".markdown"):
        for s in split_sections(text):
            body = "\n".join(s["body"])
            sections.append(
                {
                    "heading": s["heading"],
                    "line": s["line"],
                    "tokens": tok.count(s["heading"] + "\n" + body),
                    "bytes": len(body.encode("utf-8")),
                }
            )
    return {
        "path": str(path),
        "tokens": tok.count(text),
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "sections": sections,
    }


ALWAYS_ON = ("instruction prose", "skills index", "agents", "commands", "output styles")


def frontmatter(text: str) -> str:
    """The YAML block a skill or agent advertises itself with.

    This is what the client loads into every turn. The body below it loads only
    when the skill is invoked. Counting bodies as always-on context overstates
    the standing cost by an order of magnitude, which is the exact error this
    plugin exists to catch.
    """
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[: end + 4] if end != -1 else ""


def discover(project: Path) -> dict:
    home = Path.home() / ".claude"
    groups: dict = {
        "instruction prose": [],
        "skills index": [],
        "skill bodies (on demand)": [],
        "agents": [],
        "commands": [],
        "output styles": [],
    }
    for p in (home / "CLAUDE.md", project / "CLAUDE.md", project / "AGENTS.md"):
        if p.is_file():
            groups["instruction prose"].append(p)
    if (home / "skills").is_dir():
        for p in sorted((home / "skills").rglob("*.md")):
            if p.name == "SKILL.md":
                groups["skills index"].append(p)
            else:
                groups["skill bodies (on demand)"].append(p)
    for base, key in (
        (home / "agents", "agents"),
        (home / "commands", "commands"),
        (home / "output-styles", "output styles"),
    ):
        if base.is_dir():
            groups[key].extend(sorted(p for p in base.rglob("*.md") if p.is_file()))
    return groups


def measured_prefix(projects: Path, sample: int = 12) -> dict:
    """The static prefix as the client actually built it, from recent transcripts.

    Turn 1's cache_creation_input_tokens is the whole prefix: system prompt,
    instruction prose, skills index, agents, and every MCP tool schema. Anything
    not accountable to a file on disk is inventory the client assembled, and the
    largest part of that is MCP tool schemas. Naming the residue is how the
    invisible cost stops being invisible.
    """
    import json as _json

    if not projects.is_dir():
        return {}
    files = sorted(projects.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:sample]
    per_model: dict = {}
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    model = msg.get("model") or ""
                    if not model or model == "<synthetic>":
                        continue
                    cc = int((msg.get("usage") or {}).get("cache_creation_input_tokens") or 0)
                    if cc:
                        per_model.setdefault(model, []).append(cc)
                    break
        except OSError:
            continue
    return {m: sorted(v)[len(v) // 2] for m, v in per_model.items() if v}


def mcp_schema_cost(tok: Tokenizer) -> list:
    """Per-server schema cost, measured from the configured server list.

    The tool schemas themselves are assembled by the client, not stored on disk,
    so what is measured here is the configuration that causes them. The row says
    so rather than presenting a guess as a measurement.
    """
    from cdlib import claude_settings

    servers = (claude_settings().get("mcpServers") or {})
    rows = []
    for name, spec in sorted(servers.items()):
        blob = json.dumps({name: spec}, indent=2)
        rows.append({"server": name, "config_tokens": tok.count(blob)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet budget")
    ap.add_argument("--project", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--max-lines", type=int, default=0,
                    help="exit 1 if any measured file exceeds this many lines")
    ap.add_argument("--with-bodies", action="store_true",
                    help="also tokenise skill bodies, which load on demand rather than every turn")
    args = ap.parse_args()

    cfg = load_config(args.project)
    tok = Tokenizer(cfg.get("tokenizer", "auto"))
    project = Path(args.project).resolve()

    if args.path:
        groups = {"requested": []}
        for raw in args.path:
            p = Path(raw)
            if p.is_dir():
                groups["requested"].extend(sorted(q for q in p.rglob("*") if q.is_file()
                                                  and q.suffix in (".md", ".py", ".json", ".sh")))
            elif p.is_file():
                groups["requested"].append(p)
    else:
        groups = discover(project)
        if not args.with_bodies:
            # Skill bodies load only when a skill is invoked. Tokenising 289 of
            # them costs most of the runtime and measures something that is not
            # in the window. The count is still reported, as a file count.
            deferred = groups.pop("skill bodies (on demand)", [])
            report_deferred = len(deferred)
        else:
            report_deferred = 0

    report = {"tokenizer": tok.name, "project": str(project), "groups": {}, "mcp": []}
    for group, paths in groups.items():
        index_only = group == "skills index"
        measured = [measure_file(p, tok, index_only=index_only) for p in paths]
        report["groups"][group] = {
            "always_on": group in ALWAYS_ON,
            "files": measured,
            "tokens": sum(m["tokens"] for m in measured),
            "bytes": sum(m["bytes"] for m in measured),
            "count": len(measured),
        }
    if not args.path:
        report["mcp"] = mcp_schema_cost(tok)
        report["measured_prefix"] = measured_prefix(Path.home() / ".claude" / "projects")

    if args.json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write("context-diet budget  (tokeniser: %s)\n" % tok.name)
        if tok.note:
            sys.stdout.write("note: %s\n" % tok.note)
        sys.stdout.write("\n%-26s %10s %10s %7s\n" % ("category", "tokens", "bytes", "files"))
        sys.stdout.write("-" * 56 + "\n")
        always_on = 0
        on_demand = 0
        for group, data in report["groups"].items():
            if not data["count"]:
                continue
            if data["always_on"]:
                always_on += data["tokens"]
            else:
                on_demand += data["tokens"]
            sys.stdout.write("%-26s %10s %10s %7d\n"
                             % (group, format(data["tokens"], ","), format(data["bytes"], ","),
                                data["count"]))
        sys.stdout.write("-" * 56 + "\n")
        sys.stdout.write("%-26s %10s\n" % ("always on, every turn", format(always_on, ",")))
        if on_demand:
            sys.stdout.write("%-26s %10s\n" % ("on demand, when invoked", format(on_demand, ",")))
        elif not args.path and locals().get("report_deferred"):
            sys.stdout.write("%-26s %10s files, not counted (use --with-bodies)\n"
                             % ("skill bodies, on demand", report_deferred))
        sys.stdout.write("\n")
        for group, data in report["groups"].items():
            biggest = sorted(data["files"], key=lambda m: -m["tokens"])[:5]
            if not biggest:
                continue
            sys.stdout.write("largest in %s:\n" % group)
            for m in biggest:
                sys.stdout.write("  %8s  %s\n" % (format(m["tokens"], ","), m["path"]))
            sys.stdout.write("\n")
        prefix = report.get("measured_prefix") or {}
        if prefix:
            sys.stdout.write("measured static prefix, turn 1 of recent sessions:\n")
            sys.stdout.write("%-26s %10s %12s\n" % ("model", "prefix", "vs on disk"))
            for model in sorted(prefix):
                residue = prefix[model] - always_on
                if residue > 0:
                    verdict = "+%s inventory" % format(residue, ",")
                else:
                    verdict = "loaded less than the full tree"
                sys.stdout.write("%-26s %10s  %s\n"
                                 % (model[:26], format(prefix[model], ","), verdict))
            sys.stdout.write(
                "\nOn disk, always-on totals %s tokens, but a session loads only the plugins, "
                "skills and agents it has enabled, so the comparison is a floor and not a "
                "subtraction. Where the prefix exceeds it, the excess is inventory the client "
                "assembled and never wrote down, most of it MCP tool schemas. %d MCP servers "
                "are configured.\n\n" % (format(always_on, ","), len(report["mcp"])))
        if report["mcp"]:
            sys.stdout.write("largest MCP server configurations:\n")
            for row in sorted(report["mcp"], key=lambda r: -r["config_tokens"])[:5]:
                sys.stdout.write("  %8s  %s\n" % (format(row["config_tokens"], ","), row["server"]))
            sys.stdout.write("\nTo price one server's schemas, disable it and re-run: the drop in "
                             "the prefix column is that server's real cost.\n")

    if args.max_lines:
        over = [m for data in report["groups"].values() for m in data["files"]
                if m.get("lines", 0) > args.max_lines]
        for m in over:
            sys.stderr.write("over %d lines: %s (%d)\n" % (args.max_lines, m["path"], m["lines"]))
        if over:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
