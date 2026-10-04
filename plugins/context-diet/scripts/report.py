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
from cdlib import load_config, median, note_means_it_did_not_run, spread  # noqa: E402

NUMERIC = ("fresh_input", "cache_creation", "cache_read", "output", "wall_ms",
           "tool_calls", "file_reads")


def read_runs(path: Path) -> tuple:
    """Rows, the count of unreadable lines, and how many `ran` values were derived.

    A row whose cell count does not match the header used to be skipped in
    silence. A report built on 40 of 90 runs looked exactly like a report built
    on all 90, which is the one thing a measurement tool must never do.
    """
    rows = []
    skipped = 0
    derived = 0
    with path.open(encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            if not line.strip():
                continue
            cells = line.rstrip("\n").split("\t")
            if len(cells) != len(header):
                skipped += 1
                continue
            row = dict(zip(header, cells))
            for key in NUMERIC:
                try:
                    row[key] = int(float(row.get(key) or 0))
                except ValueError:
                    row[key] = 0
            row["passed"] = str(row.get("passed")).lower() in ("true", "1", "yes")
            if "ran" in header:
                row["ran"] = str(row["ran"]).strip().lower() not in ("false", "0", "no")
            else:
                # A ledger written before the `ran` column existed still records
                # why a run produced nothing, in the note measure.py wrote for
                # itself. Defaulting those to "it ran" is what let 62 runs killed
                # by a usage limit be counted as 62 failed tasks. Recover the
                # fact from the note rather than trusting the absent column.
                row["ran"] = not note_means_it_did_not_run(row.get("note", ""))
                derived += 1
            rows.append(row)
    return rows, skipped, derived


def ran(rows: list) -> list:
    """Only the runs that produced evidence.

    Every median, every pass count and every verdict is computed from these.
    A run that never executed is missing data; counting it as a failure lets a
    usage limit masquerade as a finding about the thing being measured.
    """
    return [r for r in rows if r.get("ran", True)]


def trial_pass_counts(rows: list) -> list:
    """Passes per trial, so task success has a spread of its own to be judged against.

    One number per config cannot be inconclusive, because there is nothing to
    compare it with. Grouping by trial gives the same within-config variation
    the token metrics are already held to.
    """
    by_trial: dict = {}
    for r in rows:
        by_trial.setdefault(r.get("trial"), []).append(bool(r.get("passed")))
    return [sum(1 for v in vals if v) for _t, vals in sorted(by_trial.items(), key=lambda kv: str(kv[0]))]


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
        header = "%-6s %5s %8s %7s %12s %12s %12s %10s" % (
            "config", "runs", "no data", "passed", "fresh in", "cache read", "output", "wall ms")
        w(header)
        w("-" * len(header))
        missing_any = False
        for config in ("Fat", "Lean", "Bare"):
            all_rs = grouped.get((model, config), [])
            if not all_rs:
                continue
            rs = ran(all_rs)
            lost = len(all_rs) - len(rs)
            missing_any = missing_any or bool(lost)
            if not rs:
                w("%-6s %5d %8d %7s %12s %12s %12s %10s" % (
                    config, len(all_rs), lost, "-", "-", "-", "-", "-"))
                continue
            w("%-6s %5d %8d %7d %12s %12s %12s %10s" % (
                config,
                len(all_rs),
                lost,
                sum(1 for r in rs if r["passed"]),
                format(int(median([r["fresh_input"] for r in rs])), ","),
                format(int(median([r["cache_read"] for r in rs])), ","),
                format(int(median([r["output"] for r in rs])), ","),
                format(int(median([r["wall_ms"] for r in rs])), ","),
            ))
        w("")
        if missing_any:
            w("The \"no data\" column counts runs where the agent never executed - a usage "
              "limit, an unparseable reply. Those produced no evidence about the task, so they "
              "are excluded from every number here rather than counted as failures. The reason "
              "for each is in the caveats below.")
            w("")
        w("Medians above. Fresh input, cache creation and cache read stay in separate columns "
          "because summing them is how a cumulative counter gets quoted as a low number.")
        w("")

        for metric in ("wall_ms", "output"):
            fat = [r[metric] for r in ran(grouped.get((model, "Fat"), []))]
            lean = [r[metric] for r in ran(grouped.get((model, "Lean"), []))]
            bare = [r[metric] for r in ran(grouped.get((model, "Bare"), []))]
            w("- " + compare(fat, lean, "Fat", "Lean", metric))
            w("- " + compare(fat, bare, "Fat", "Bare", metric))
        w("")

        pass_by_config = {
            c: (sum(1 for r in ran(grouped.get((model, c), [])) if r["passed"]),
                len(ran(grouped.get((model, c), []))))
            for c in ("Fat", "Lean", "Bare")
        }
        stated = ", ".join(
            "%s %d/%d" % (c, p, t) if t else "%s no runs with data" % c
            for c in ("Fat", "Lean", "Bare")
            if grouped.get((model, c))
            for p, t in [pass_by_config[c]]
        )
        w("Task success: " + stated)
        bare_p, bare_t = pass_by_config.get("Bare", (0, 0))
        fat_p, fat_t = pass_by_config.get("Fat", (0, 0))
        if not bare_t or not fat_t:
            # The comparison the whole harness exists for cannot be made from a
            # config with nothing in it. Naming the gap is the honest output; a
            # verdict here would be a claim about runs that never happened.
            absent = [c for c in ("Fat", "Lean", "Bare")
                      if grouped.get((model, c)) and not pass_by_config[c][1]]
            if absent:
                w("No verdict on task success for %s: %s produced no runs with data, so there "
                  "is nothing to compare Fat against. Re-run those before reading anything into "
                  "the pass counts above." % (model, " and ".join(absent)))
            else:
                w("No verdict on task success for %s: the comparison needs both Fat and Bare."
                  % model)
        if bare_t and fat_t:
            # Pass counts get the same treatment as every other number here.
            # This block used to compare the raw totals and announce that "the
            # post's prescription held" on a one-task difference, which is the
            # exact claim the docstring above forbids and which the token
            # metrics have refused to make since the first version.
            fat_trials = trial_pass_counts(ran(grouped.get((model, "Fat"), [])))
            bare_trials = trial_pass_counts(ran(grouped.get((model, "Bare"), [])))
            widest = max(spread(fat_trials), spread(bare_trials))
            diff = abs(bare_p - fat_p)
            if diff <= widest:
                w("Fat and Bare are inconclusive on task success for %s: the gap is %d task(s) "
                  "and one config's own trials vary by %d, so the gap is not a finding."
                  % (model, diff, widest))
            elif bare_p > fat_p:
                w("On this evidence, removing the instruction file did not hurt for %s: Bare "
                  "finished %d more task(s) than Fat, which exceeds the %d-task spread within a "
                  "config. The post's prescription held here." % (model, diff, widest))
            else:
                w("Removing the instruction file cost %d task(s) for %s, which exceeds the "
                  "%d-task spread within a config. That is the rediscovery cost the post never "
                  "tested." % (diff, model, widest))
        w("")

    notes = {r.get("note", "") for r in rows if r.get("note")}
    if notes:
        w("Caveats recorded during the run:")
        for n in sorted(notes):
            w("- %s" % n)
        w("")

    executed = ran(rows)
    if len(executed) != len(rows):
        w("%d of %d runs in this ledger never executed and are excluded from every number "
          "above." % (len(rows) - len(executed), len(rows)))
        w("")
    if not executed:
        w("No run in this ledger executed, so this report states nothing. Fix the cause named "
          "in the caveats and run it again.")
        return "\n".join(out)

    zero_tokens = all(r["fresh_input"] == 0 and r["cache_read"] == 0 for r in executed)
    if zero_tokens:
        w("Every token column is zero, so no token claim can be made from this run. Only the "
          "pass and fail columns carry information.")
        w("")
    return "\n".join(out)


def cross_check(rows: list, cfg: dict, project: str = ".") -> str:
    """The ledger is the source. MLflow is compared to it trial by trial.

    report.py stays stdlib-only, so the comparison runs in mlflow_sink under the
    interpreter that has MLflow.
    """
    try:
        from mlflow_sink import delegate  # noqa: PLC0415

        out = delegate("cross-check", rows, project)
    except Exception as exc:  # noqa: BLE001 - the ledger stands alone on any failure
        return "MLflow cross-check failed (%s). The ledger stands alone." % type(exc).__name__
    if not out.get("available"):
        if out.get("error"):
            return "MLflow cross-check failed (%s). The ledger stands alone." % out["error"]
        return "MLflow is not installed, so the ledger is the only record. Nothing to cross-check."
    return out.get("text", "")


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
    rows, skipped, derived = read_runs(path)
    if not rows:
        sys.stderr.write("run file has no rows\n")
        return 1
    if skipped:
        sys.stdout.write(
            "%d line(s) in %s did not match the header and were not read. Every number below "
            "is from the %d run(s) that did.\n\n" % (skipped, path.name, len(rows)))
    if derived:
        sys.stdout.write(
            "%s has no `ran` column, so whether each run executed was read from the note the "
            "harness wrote for itself, for all %d row(s). Re-running the batch with the current "
            "measure.py records it outright.\n\n" % (path.name, derived))
    sys.stdout.write(render(rows, cfg, args.markdown) + "\n")
    # MLflow is required: the report above still prints from the ledger, then a
    # missing install ends the command with the install line and exit 1.
    from mlflow_sink import MLflowMissing, delegate, require_python  # noqa: PLC0415

    try:
        require_python(args.project)
    except MLflowMissing as exc:
        sys.stderr.write("%s\n" % exc)
        return 1
    # Drain the Stop hook's queued session rows first. The hook cannot import
    # MLflow itself, so the command layer is where the queue is emptied.
    drained = delegate("drain", None, args.project).get("drained", 0)
    if drained:
        sys.stdout.write("logged %d queued session row(s) to MLflow\n" % drained)
    sys.stdout.write(cross_check(rows, cfg, args.project) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
