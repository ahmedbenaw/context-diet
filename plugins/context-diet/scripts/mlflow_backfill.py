#!/usr/bin/env python3
"""mlflow_backfill.py - put every run already on disk into MLflow, once.

The ledgers are the source of truth and MLflow is the second record. Runs made
while MLflow could not be imported exist only in the ledgers; runs logged on
19 Sep exist in MLflow without the keys that tie them to a ledger row. This
labels the old runs in place (it deletes nothing), then logs every ledger row
the store does not already hold. Run it twice and the second run adds nothing.

    mlflow_backfill.py [--project .] [--ab-ledger FILE ...] [--sessions FILE] [--census TSV]

With no --ab-ledger it takes every runs*.tsv in the A/B folder, oldest first,
so a trial carried into a later batch keeps the batch it first ran in.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import load_config, mlflow_python, state_dir  # noqa: E402


def read_tsv(path: Path) -> list:
    with path.open(encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh, delimiter="\t") if r.get("model")]


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet MLflow backfill")
    ap.add_argument("--project", default=".")
    ap.add_argument("--ab-ledger", action="append", default=[])
    ap.add_argument("--sessions", default="")
    ap.add_argument("--census", default="")
    args = ap.parse_args()

    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    try:
        import mlflow  # noqa: F401, PLC0415
    except ImportError:
        python = mlflow_python(args.project)
        if python is None or os.environ.get("CONTEXT_DIET_BACKFILL_REEXEC"):
            sys.stdout.write("mlflow: not installed, so there is nowhere to backfill to. "
                             "Run mlflow_bootstrap.sh --install first.\n")
            return 1
        os.environ["CONTEXT_DIET_BACKFILL_REEXEC"] = "1"
        os.execv(python, [python, str(Path(__file__).resolve())] + sys.argv[1:])

    from mlflow_sink import (adopt_legacy, drain_pending, log_ab_rows,  # noqa: PLC0415
                             log_census, log_sessions)

    cfg = load_config(args.project)
    sdir = state_dir(args.project)
    ledgers = [Path(p) for p in args.ab_ledger] or sorted(
        (sdir / "ab").glob("runs*.tsv"), key=lambda p: (p.stat().st_mtime, p.name))
    records = []
    for path in ledgers:
        for rec in read_tsv(path):
            rec["batch"] = path.stem
            records.append(rec)
    sessions_path = Path(args.sessions) if args.sessions else sdir / "ledger.tsv"
    sessions = read_tsv(sessions_path) if sessions_path.is_file() else []

    report = {"ledgers": [p.name for p in ledgers], "ledger_rows": len(records),
              "session_rows": len(sessions)}
    report["legacy"] = adopt_legacy(records, sessions, cfg, args.project)
    report["ab"] = log_ab_rows(records, cfg, args.project)
    report["sessions"] = log_sessions(sessions, cfg, args.project)
    report["drained"] = drain_pending(cfg, args.project)
    if args.census:
        report["census"] = log_census(args.census, cfg, args.project)
    sys.stdout.write(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
