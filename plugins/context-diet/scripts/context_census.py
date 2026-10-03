#!/usr/bin/env python3
"""context_census.py - the diagnosis that gates every cut.

Reads Claude Code transcripts and answers four hypotheses per model. Read-only,
standard library only. Never opens a transcript for write. Tolerates a truncated
last line.

Extends the counting approach of analyze-token-usage.py with two more passes per
record: one over `attachment` records (H3, H4) and one over `tool_use` /
`toolUseResult` records (H1, H2 size ranking).

Row granularity is (transcript, model). A transcript that used two models yields
two rows and nothing is ever pooled across models. Every transcript in scope
appears in at least one row.

Usage:
    context_census.py [--projects DIR] [--out DIR] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import cache_state, hit_ratio, load_config, median, spread  # noqa: E402

# A Read whose path ends in one of these, or sits under a .claude directory,
# is an instruction-file read. This is H1's whole definition.
INSTRUCTION_BASENAMES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", "AGENTS.local.md")
READ_TOOLS = ("Read", "NotebookRead")

COLUMNS = [
    "transcript",
    "session_id",
    "model",
    "turns",
    "h1_instruction_reads",
    "h1_md_reads",
    "h1_dot_claude_reads",
    "h1_total_reads",
    "h2_prefix_tokens",
    "h2_cache_read_first",
    "h2_cache_read_last",
    "h2_cache_read_growth",
    "h2_top_tool_result_bytes",
    "h2_top_tool_result_name",
    "h3_hook_ms_total",
    "h3_hook_ms_top_event",
    "h3_hook_ms_top_value",
    "h4_injected_bytes",
    "h4_top_injector",
    "h4_top_injector_bytes",
    # Where the built-in compactor actually fired. Part 5.3 requires Amber to
    # sit strictly below it, and until now that was reasoned about rather than
    # measured, even though every compaction record states it outright.
    # H5: the cache. A read is billed at a fraction of a write, so the share of
    # prefix tokens served from cache is the number tied to money. A session
    # that re-creates its prefix is paying full price for tokens it already had.
    "h5_cache_hit_ratio",
    "h5_cold_starts",
    "h5_cold_start_tokens",
    "compactor_events",
    "compactor_auto_events",
    "compactor_lowest_auto_pre_tokens",
    "compactor_dropped_tokens",
    "fresh_input",
    "cache_creation",
    "cache_read",
    "output",
]


def classify_read(path: str) -> str:
    """Return 'md', 'dot_claude' or '' for one Read target.

    H1's definition is the union of the two: an instruction file by name, or any
    file under a .claude directory. They are counted separately because a read of
    CLAUDE.md is the post's actual claim, while a read of a skill body under
    ~/.claude/plugins is a different cost with a different fix. Reporting only
    the union would hide which one is happening.
    """
    if not path:
        return ""
    p = str(path)
    if os.path.basename(p) in INSTRUCTION_BASENAMES:
        return "md"
    if any(part == ".claude" for part in Path(p).parts):
        return "dot_claude"
    return ""


def iter_records(path: Path):
    """Yield parsed records. A truncated or corrupt line is skipped, not fatal."""
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue


def new_model_bucket() -> dict:
    return {
        "turns": 0,
        "fresh_input": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "output": 0,
        "h1_instruction_reads": 0,
        "h1_md_reads": 0,
        "h1_dot_claude_reads": 0,
        "h1_total_reads": 0,
        "h2_prefix_tokens": 0,
        "h2_cache_read_first": 0,
        "h2_cache_read_last": 0,
        "top_result_bytes": 0,
        "top_result_name": "",
        "h5_cold_starts": 0,
        "h5_cold_start_tokens": 0,
    }


def analyze(path: Path, cold_floor: int = 20000) -> dict:
    """One transcript -> per-model rows plus session-level hook measurements.

    H3 and H4 are session-level: a hook's wall-clock and injected bytes are not
    attributable to a model, so they are reported on the session and repeated on
    each of its model rows, with the session id making the duplication visible.
    """
    models: dict = defaultdict(new_model_bucket)
    hook_ms_by_event: dict = defaultdict(int)
    hook_ms_by_name: dict = defaultdict(int)
    injected_by_hook: dict = defaultdict(int)
    session_id = ""
    compactions = 0
    auto_compactions = 0
    lowest_auto_pre = 0
    dropped = 0
    tool_use_model: dict = {}
    tool_use_name: dict = {}
    current_model = ""

    for rec in iter_records(path):
        if not session_id:
            session_id = rec.get("sessionId", "") or ""
        rtype = rec.get("type")

        if rtype == "assistant":
            msg = rec.get("message") or {}
            model = msg.get("model") or "unknown"
            if model == "<synthetic>":
                continue
            current_model = model
            bucket = models[model]
            bucket["turns"] += 1
            usage = msg.get("usage") or {}
            cc = int(usage.get("cache_creation_input_tokens") or 0)
            cr = int(usage.get("cache_read_input_tokens") or 0)
            bucket["fresh_input"] += int(usage.get("input_tokens") or 0)
            bucket["cache_creation"] += cc
            bucket["cache_read"] += cr
            bucket["output"] += int(usage.get("output_tokens") or 0)
            # H2: the static prefix is turn 1's cache creation for this model.
            if bucket["turns"] == 1:
                bucket["h2_prefix_tokens"] = cc
                bucket["h2_cache_read_first"] = cr
            bucket["h2_cache_read_last"] = cr
            # H5: a turn that re-created more of the prefix than it read back
            # paid the write price for tokens it already had. Turn 1 is excluded
            # because a session's first turn has nothing to read and would count
            # every session as one cold start.
            if bucket["turns"] > 1:
                state, created, _read = cache_state({
                    "cache_creation_input_tokens": cc,
                    "cache_read_input_tokens": cr,
                }, cold_floor)
                if state == "cold":
                    bucket["h5_cold_starts"] += 1
                    bucket["h5_cold_start_tokens"] += created
            for blk in msg.get("content") or []:
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                name = blk.get("name") or ""
                tool_use_model[blk.get("id") or ""] = model
                tool_use_name[blk.get("id") or ""] = name
                if name in READ_TOOLS:
                    bucket["h1_total_reads"] += 1
                    inp = blk.get("input") or {}
                    target = inp.get("file_path") or inp.get("notebook_path") or ""
                    kind = classify_read(target)
                    if kind:
                        bucket["h1_instruction_reads"] += 1
                        bucket["h1_%s_reads" % kind] += 1

        elif rtype == "user":
            # H2 ranking: how big is each tool result, and which tool produced it.
            result = rec.get("toolUseResult")
            if result is None:
                continue
            size = len(json.dumps(result, default=str))
            source_id = rec.get("sourceToolUseID") or ""
            if not source_id:
                # The link back to the call lives on the tool_result content block.
                for item in (rec.get("message") or {}).get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        source_id = item.get("tool_use_id") or ""
                        break
            owner = tool_use_model.get(source_id, current_model)
            if not owner:
                continue
            bucket = models[owner]
            if size > bucket["top_result_bytes"]:
                bucket["top_result_bytes"] = size
                # The result record carries no tool name, so it is resolved from the
                # tool_use block that produced it. Falling back to a literal
                # "(unnamed)" would report a fact the transcript never stated.
                name = tool_use_name.get(source_id, "")
                if not name and isinstance(result, dict):
                    name = result.get("toolName") or result.get("tool") or ""
                bucket["top_result_name"] = name or "(tool not resolvable)"

        cm = rec.get("compactMetadata")
        if isinstance(cm, dict):
            compactions += 1
            pre = cm.get("preTokens")
            if cm.get("trigger") == "auto":
                auto_compactions += 1
                if isinstance(pre, int) and pre > 0:
                    # The smallest window at which the built-in compactor was
                    # seen to fire. Amber has to sit below this, not below a
                    # setting that may not be what the client acts on.
                    lowest_auto_pre = min(lowest_auto_pre or pre, pre)
            got = cm.get("cumulativeDroppedTokens")
            if isinstance(got, int):
                dropped = max(dropped, got)

        if rtype == "attachment":
            att = rec.get("attachment") or {}
            atype = att.get("type")
            if atype in ("hook_success", "hook_non_blocking_error"):
                name = att.get("hookName") or "(unnamed hook)"
                event = att.get("hookEvent") or name.split(":")[0]
                ms = int(att.get("durationMs") or 0)
                hook_ms_by_event[event] += ms
                hook_ms_by_name[name] += ms
            elif atype == "hook_additional_context":
                content = att.get("content")
                if isinstance(content, list):
                    size = sum(len(str(c)) for c in content)
                else:
                    size = len(str(content or ""))
                # Every one of these carries its own hookName: 763 of 763 across
                # this machine's transcripts. Charging the bytes to whichever
                # hook happened to run most recently invented an attribution the
                # record already stated, and mis-stated it whenever a hook
                # injected without having logged a hook_success first.
                injected_by_hook[att.get("hookName") or "(unattributed)"] += size

    top_event = max(hook_ms_by_event.items(), key=lambda kv: kv[1], default=("", 0))
    top_injector = max(injected_by_hook.items(), key=lambda kv: kv[1], default=("", 0))
    return {
        "session_id": session_id,
        "compactor_events": compactions,
        "compactor_auto_events": auto_compactions,
        "compactor_lowest_auto_pre_tokens": lowest_auto_pre,
        "compactor_dropped_tokens": dropped,
        "models": models,
        "h3_total": sum(hook_ms_by_event.values()),
        "h3_top_event": top_event[0],
        "h3_top_value": top_event[1],
        "h3_by_name": dict(hook_ms_by_name),
        "h4_total": sum(injected_by_hook.values()),
        "h4_top": top_injector[0],
        "h4_top_bytes": top_injector[1],
    }


def rows_for(path: Path, result: dict) -> list:
    rows = []
    models = result["models"]
    if not models:
        models = {"(no assistant turns)": new_model_bucket()}
    for model, b in sorted(models.items()):
        rows.append(
            {
                "transcript": str(path),
                "session_id": result["session_id"],
                "model": model,
                "turns": b["turns"],
                "h1_instruction_reads": b["h1_instruction_reads"],
                "h1_md_reads": b["h1_md_reads"],
                "h1_dot_claude_reads": b["h1_dot_claude_reads"],
                "h1_total_reads": b["h1_total_reads"],
                "h2_prefix_tokens": b["h2_prefix_tokens"],
                "h2_cache_read_first": b["h2_cache_read_first"],
                "h2_cache_read_last": b["h2_cache_read_last"],
                "h2_cache_read_growth": b["h2_cache_read_last"] - b["h2_cache_read_first"],
                "h2_top_tool_result_bytes": b["top_result_bytes"],
                "h2_top_tool_result_name": b["top_result_name"],
                "h3_hook_ms_total": result["h3_total"],
                "h3_hook_ms_top_event": result["h3_top_event"],
                "h3_hook_ms_top_value": result["h3_top_value"],
                "h4_injected_bytes": result["h4_total"],
                "h4_top_injector": result["h4_top"],
                "h4_top_injector_bytes": result["h4_top_bytes"],
                "h5_cache_hit_ratio": (
                    round(ratio, 4) if (ratio := hit_ratio(b["cache_creation"], b["cache_read"]))
                    is not None else ""),
                "h5_cold_starts": b["h5_cold_starts"],
                "h5_cold_start_tokens": b["h5_cold_start_tokens"],
                "compactor_events": result["compactor_events"],
                "compactor_auto_events": result["compactor_auto_events"],
                "compactor_lowest_auto_pre_tokens": result["compactor_lowest_auto_pre_tokens"],
                "compactor_dropped_tokens": result["compactor_dropped_tokens"],
                "fresh_input": b["fresh_input"],
                "cache_creation": b["cache_creation"],
                "cache_read": b["cache_read"],
                "output": b["output"],
            }
        )
    return rows


PRIMARY_MODELS = ("opus", "fable")


def model_sort_key(model: str) -> tuple:
    low = model.lower()
    for i, tag in enumerate(PRIMARY_MODELS):
        if tag in low:
            return (0, i, low)
    return (1, 0, low)


def summarize(rows: list) -> str:
    by_model: dict = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    out = ["# Census summary", ""]
    out.append(
        "Transcripts: %d. Rows: %d. Nothing is pooled across models."
        % (len({r["transcript"] for r in rows}), len(rows))
    )
    out.append("")
    out.append(
        "H3 and H4 are session-level measurements. They repeat on each model row of "
        "a multi-model session and are counted once per session in the medians below."
    )
    out.append("")
    for model in sorted(by_model, key=model_sort_key):
        rs = by_model[model]
        turns = sum(r["turns"] for r in rs)
        if turns == 0:
            continue
        ins = sum(r["h1_instruction_reads"] for r in rs)
        md = sum(r["h1_md_reads"] for r in rs)
        dot = sum(r["h1_dot_claude_reads"] for r in rs)
        reads = sum(r["h1_total_reads"] for r in rs)
        share = (md / reads) if reads else 0.0
        median_prefix = median([r["h2_prefix_tokens"] for r in rs if r["h2_prefix_tokens"]])
        median_growth = median([r["h2_cache_read_growth"] for r in rs])
        sessions = {r["session_id"] or r["transcript"]: r for r in rs}
        median_hook_ms = median([r["h3_hook_ms_total"] for r in sessions.values()])
        median_injected = median([r["h4_injected_bytes"] for r in sessions.values()])
        top_injectors: dict = defaultdict(int)
        for r in sessions.values():
            if r["h4_top_injector"]:
                top_injectors[r["h4_top_injector"]] += r["h4_top_injector_bytes"]
        top_injector = max(top_injectors.items(), key=lambda kv: kv[1], default=("none", 0))
        top_results: dict = defaultdict(int)
        for r in rs:
            if r["h2_top_tool_result_name"]:
                top_results[r["h2_top_tool_result_name"]] = max(
                    top_results[r["h2_top_tool_result_name"]], r["h2_top_tool_result_bytes"]
                )
        top_result = max(top_results.items(), key=lambda kv: kv[1], default=("none", 0))

        if reads == 0:
            h1 = "INCONCLUSIVE"
        elif share >= 0.05:
            h1 = "SUPPORTED"
        else:
            h1 = "REFUTED"
        # H2 is decided per session, not by two medians. Each session is
        # compared against itself: did its cache-read growth exceed its own
        # turn-1 prefix? Sessions differ enormously in length, so a spread taken
        # across them measures session length rather than noise, and pooling
        # them called a 43x gap inconclusive. The verdict was previously the
        # literal string "H2 SUPPORTED", which no transcript could contradict.
        paired = [(r["h2_prefix_tokens"], r["h2_cache_read_growth"])
                  for r in rs if r["h2_prefix_tokens"]]
        n_paired = len(paired)
        growth_sessions = sum(1 for pre, gro in paired if gro > pre)
        if n_paired == 0:
            h2 = "INCONCLUSIVE"
            h2_because = "no session recorded a turn-1 prefix, so there is nothing to compare"
            h2_growth_wins = False
        else:
            share_growth = growth_sessions / n_paired
            h2_growth_wins = share_growth >= 0.5
            if share_growth >= 0.6:
                h2 = "SUPPORTED"
                h2_because = (
                    "accumulated output drives cost in %d of %d sessions"
                    % (growth_sessions, n_paired))
            elif share_growth <= 0.4:
                h2 = "REFUTED"
                h2_because = (
                    "the static prefix drives cost in %d of %d sessions"
                    % (n_paired - growth_sessions, n_paired))
            else:
                h2 = "INCONCLUSIVE"
                h2_because = (
                    "sessions split %d to %d between growth and prefix, which is too close "
                    "to call" % (growth_sessions, n_paired - growth_sessions))
        h3 = "SUPPORTED" if median_hook_ms >= 2000 else "REFUTED"
        h4 = "SUPPORTED" if median_injected >= 4000 else "REFUTED"

        out.append("## %s" % model)
        out.append("Sessions: %d. Assistant turns: %d." % (len(sessions), turns))
        out.append("")
        out.append(
            "- **H1 %s** - %d of %d Read calls (%.1f%%) targeted an instruction file by name "
            "(CLAUDE.md or AGENTS.md). A further %d targeted other files under a .claude "
            "directory, which is a different cost with a different fix."
            % (h1, md, reads, share * 100, dot)
        )
        out.append(
            "- **H2 %s, %s** - median static prefix %s tokens, median cache-read growth across "
            "a session %s tokens; largest single tool result %s bytes from %s."
            % (
                h2,
                h2_because,
                format(int(median_prefix), ","),
                format(int(median_growth), ","),
                format(top_result[1], ","),
                top_result[0],
            )
        )
        out.append(
            "- **H3 %s** - median hook wall-clock per session %s ms."
            % (h3, format(int(median_hook_ms), ","))
        )
        # H5: the cache. Read is billed at a fraction of write, so the share of
        # prefix tokens served from cache is the figure with money attached. The
        # 90% line is v4's; it is stated as the threshold being applied, not as
        # something this census independently established.
        ratios = [float(r["h5_cache_hit_ratio"]) for r in rs if r["h5_cache_hit_ratio"] != ""]
        cold = sum(int(r["h5_cold_starts"] or 0) for r in rs)
        cold_tokens = sum(int(r["h5_cold_start_tokens"] or 0) for r in rs)
        if not ratios:
            out.append(
                "- **H5 INCONCLUSIVE** - no session recorded any prefix tokens, so there is no "
                "hit ratio to report.")
        else:
            med_ratio = median(ratios)
            below = sum(1 for r in ratios if r < 0.90)
            spread_ratio = spread(ratios)
            gap = abs(med_ratio - 0.90)
            # The same rule the A/B report is held to. Sessions vary widely in
            # how much of their prefix is cached, and a median two points from
            # the line inside a spread of twenty is not a finding about the
            # cache; it is a finding about which sessions happened to be in the
            # sample. Without this the verdict flipped between two models whose
            # medians differ by 2.4 points.
            if gap <= spread_ratio:
                h5, because = "INCONCLUSIVE", (
                    "the median is %.1f points from the 90%% line and sessions vary by %.1f "
                    "points, so the line does not separate them"
                    % (gap * 100, spread_ratio * 100))
            elif med_ratio < 0.90:
                h5, because = "SUPPORTED", "the median sits below the line by more than sessions vary"
            else:
                h5, because = "REFUTED", "the median sits above the line by more than sessions vary"
            out.append(
                "- **H5 %s, %s** - median cache hit ratio %.1f%% across %d session(s), %d of "
                "them below the 90%% line. %s cold turn(s) re-created %s tokens at the write "
                "price that a warm turn would have read back. The 90%% line is the v4 "
                "handover's threshold, applied here rather than established here."
                % (h5, because, med_ratio * 100, len(ratios), below,
                   format(cold, ","), format(cold_tokens, ",")))
        autos = [r["compactor_lowest_auto_pre_tokens"] for r in rs if r["compactor_lowest_auto_pre_tokens"]]
        if autos:
            out.append(
                "- **Built-in compactor** - fired automatically in %d session(s); the lowest "
                "context it fired at was %s tokens. Amber must sit below that, and the plugin "
                "never runs a second compaction of its own."
                % (sum(1 for r in rs if r["compactor_auto_events"]), format(min(autos), ","))
            )
        else:
            out.append(
                "- **Built-in compactor** - not observed firing automatically for this model, so "
                "there is no measured ceiling for Amber to sit below yet."
            )
        out.append(
            "- **H4 %s** - median injected context %s bytes per session; top injector %s at %s bytes."
            % (h4, format(int(median_injected), ","), top_injector[0], format(top_injector[1], ","))
        )
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet census")
    ap.add_argument("--projects", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--out", default=".")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--project", default=os.getcwd())
    args = ap.parse_args()

    # H5's floor is a config key like every other threshold, so a wrong default
    # is visible in the effective table rather than buried in this file.
    cold_floor = int(load_config(args.project).get("cold_turn_min_tokens") or 20000)

    root = Path(args.projects)
    if not root.exists():
        sys.stderr.write("context-diet: no transcript store at %s\n" % root)
        return 1
    files = sorted(root.rglob("*.jsonl"))
    if args.limit:
        files = files[: args.limit]

    rows = []
    for path in files:
        rows.extend(rows_for(path, analyze(path, cold_floor)))

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    tsv = outdir / "census.tsv"
    with tsv.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(COLUMNS) + "\n")
        for r in rows:
            fh.write(
                "\t".join(str(r[c]).replace("\t", " ").replace("\n", " ") for c in COLUMNS) + "\n"
            )
    (outdir / "census-summary.md").write_text(summarize(rows), encoding="utf-8")
    sys.stdout.write(
        "context-diet census: %d transcripts, %d rows -> %s\n" % (len(files), len(rows), tsv)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
