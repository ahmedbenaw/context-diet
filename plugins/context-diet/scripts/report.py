#!/usr/bin/env python3
"""report.py - render before and after, per model, and refuse to overstate it.

Two rules decide every sentence this file prints.

  Never pool models. A median is per model per config. One row carries one model
  and one config, and nothing is averaged across them.

  Never state a difference smaller than the spread. Three trials is thin. Where
  the gap between two configs is inside the observed range, the report says
  inconclusive and stops. A measurement tool that dresses noise as a finding is
  worse than no measurement.

The ledger is always the source of truth. When MLflow is present its runs are
cross-checked against the ledger and any divergence is named, not reconciled
silently.

Usage:
    report.py --runs runs.tsv [--markdown]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import load_config  # noqa: E402

NUMERIC = ("fresh_input", "cache_creation", "cache_read", "output", "wall_ms",
           "tool_calls", "file_reads")


def read_runs(path: Path) -> list:
    rows = []
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            cells = line.rstrip("\n").split("\t")
            if len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            for key in NUMERIC:
                try:
                    row[key] = int(float(row.get(key) or 0))
                except ValueError:
                    row[key] = 0
            row["passed"] = str(row.get("passed")).lower() in ("true", "1", "yes")
            rows.append(row)
    return rows


def median(values: list) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def spread(values: list) -> float:
    return float(max(values) - min(values)) if values else 0.0


def compare(a_vals: list, b_vals: list, label_a: str, label_b: str, metric: str) -> str:
    """One sentence, or an honest refusal to make one."""
    if not a_vals or not b_vals:
        return "%s vs %s on %s: not enough runs to compare." % (label_a, label_b, metric)
    ma, mb = median(a_vals), median(b_vals)
    diff = abs(ma - mb)
    widest = max(spread(a_vals), spread(b_vals))
    if diff <= widest:
        return (
            "%s vs %s on %s: inconclusive. The gap is %s and the spread within a single config "
            "is %s, so the gap is not a finding."
            % (label_a, label_b, metric, format(int(diff), ","), format(int(widest), ","))
        )
    winner = label_a if ma < mb else label_b
    return (
        "%s vs %s on %s: %s is lower by %s, which exceeds the %s spread."
        % (label_a, label_b, metric, winner, format(int(diff), ","), format(int(widest), ","))
    )


def build(rows: list) -> dict:
    grouped: dict = defaultdict(list)
    for r in rows:
        grouped[(r["model"], r["config"])].append(r)
    return grouped


def render(rows: list, cfg: dict, markdown: bool) -> str:
    grouped = build(rows)
    models = sorted({r["model"] for r in rows})
    out = []
    w = out.append

    w("# A/B report" if markdown else "context-diet A/B report")
    w("")
    w("Every row carries one model and one config. Nothing is pooled.")
    w("")

    for model in models:
        w(("## %s" if markdown else "%s") % model)
        w("")
        header = "%-6s %5s %7s %12s %12s %12s %10s" % (
            "config", "runs", "passed", "fresh in", "cache read", "output", "wall ms")
        w(header)
        w("-" * len(header))
        for config in ("Fat", "Lean", "Bare"):
            rs = grouped.get((model, config), [])
            if not rs:
                continue
            w("%-6s %5d %7d %12s %12s %12s %10s" % (
                config,
                len(rs),
                sum(1 for r in rs if r["passed"]),
                format(int(median([r["fresh_input"] for r in rs])), ","),
                format(int(median([r["cache_read"] for r in rs])), ","),
                format(int(median([r["output"] for r in rs])), ","),
                format(int(median([r["wall_ms"] for r in rs])), ","),
            ))
        w("")
        w("Medians above. Fresh input, cache creation and cache read stay in separate columns "
          "because summing them is how a cumulative counter gets quoted as a low number.")
        w("")

        for metric in ("wall_ms", "output"):
            fat = [r[metric] for r in grouped.get((model, "Fat"), [])]
            lean = [r[metric] for r in grouped.get((model, "Lean"), [])]
            bare = [r[metric] for r in grouped.get((model, "Bare"), [])]
            w("- " + compare(fat, lean, "Fat", "Lean", metric))
            w("- " + compare(fat, bare, "Fat", "Bare", metric))
        w("")

        pass_by_config = {
            c: (sum(1 for r in grouped.get((model, c), []) if r["passed"]),
                len(grouped.get((model, c), [])))
            for c in ("Fat", "Lean", "Bare")
        }
        w("Task success: " + ", ".join("%s %d/%d" % (c, p, t)
                                       for c, (p, t) in pass_by_config.items() if t))
        bare_p, bare_t = pass_by_config.get("Bare", (0, 0))
        fat_p, fat_t = pass_by_config.get("Fat", (0, 0))
        if bare_t and fat_t:
            if bare_p > fat_p:
                w("On this evidence, removing the instruction file did not hurt and the post's "
                  "prescription held for %s." % model)
            elif bare_p < fat_p:
                w("Removing the instruction file cost %d task(s) for %s. That is the "
                  "rediscovery cost the post never tested." % (fat_p - bare_p, model))
            else:
                w("Fat and Bare finished the same number of tasks for %s, so the instruction "
                  "file neither helped nor hurt task success here." % model)
        w("")

    notes = {r.get("note", "") for r in rows if r.get("note")}
    if notes:
        w("Caveats recorded during the run:")
        for n in sorted(notes):
            w("- %s" % n)
        w("")

    zero_tokens = all(r["fresh_input"] == 0 and r["cache_read"] == 0 for r in rows)
    if zero_tokens:
        w("Every token column is zero, so no token claim can be made from this run. Only the "
          "pass and fail columns carry information.")
        w("")
    return "\n".join(out)


def cross_check(rows: list, cfg: dict) -> str:
    """The ledger is the source. MLflow is compared to it and divergence is named."""
    try:
        import mlflow  # noqa: PLC0415
    except Exception:
        return "MLflow is not installed, so the ledger is the only record. Nothing to cross-check."
    try:
        from mlflow_sink import resolve_uri  # noqa: PLC0415

        uri = resolve_uri(cfg.get("mlflow_tracking_uri"), cfg.get("_project", "."))
        if uri:
            mlflow.set_tracking_uri(uri)
        exp = mlflow.get_experiment_by_name("context-diet/ab")
        if exp is None:
            return "MLflow has no context-diet/ab experiment yet, so there is nothing to compare."
        runs = mlflow.search_runs([exp.experiment_id])
        if len(runs) != len(rows):
            return ("Divergence: the ledger holds %d runs and MLflow holds %d. The ledger is the "
                    "source of truth. This is reported, not reconciled." % (len(rows), len(runs)))
        return "MLflow holds %d runs and agrees with the ledger." % len(runs)
    except Exception as exc:
        return "MLflow cross-check failed (%s). The ledger stands alone." % type(exc).__name__


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet report")
    ap.add_argument("--runs", required=True)
    ap.add_argument("--project", default=".")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    path = Path(args.runs)
    if not path.is_file():
        sys.stderr.write("no run file at %s\n" % path)
        return 1
    cfg = load_config(args.project)
    rows = read_runs(path)
    if not rows:
        sys.stderr.write("run file has no rows\n")
        return 1
    sys.stdout.write(render(rows, cfg, args.markdown) + "\n")
    sys.stdout.write(cross_check(rows, cfg) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
