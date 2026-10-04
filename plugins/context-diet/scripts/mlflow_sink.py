#!/usr/bin/env python3
"""mlflow_sink.py - log runs to MLflow when it is installed, and to the ledger always.

The core of this plugin imports nothing optional. This module is the only place
that touches MLflow, and every entry point here is safe to call when MLflow is
absent: it returns False and the ledger still has the row.

Nothing in a per-tool-call hook calls into this module. Logging from inside a
50 ms monitor would break the monitor's own latency gate, which is the kind of
self-contradiction this plugin exists to catch.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import load_config, state_dir  # noqa: E402

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

REMOTE_SCHEMES = ("http", "https", "ftp", "ftps", "postgresql", "postgres",
                  "mysql", "mssql", "databricks", "s3", "gs", "azure", "wasbs")

NUMERIC_FIELDS = (
    "turns",
    "fresh_input",
    "cache_creation",
    "cache_read",
    "output",
    "tool_calls",
    "hook_ms",
    "static_prefix",
    "injected_bytes",
    "ledger_ms",
)


def resolve_uri(uri: str, project: str = ".") -> str:
    """Turn a relative local URI into an absolute one, and prefer SQLite.

    A config default has to be portable, so it is written relative. MLflow
    rejects a relative URI, and the rejection surfaces as a silent failure to log
    anything at all, so it is resolved here rather than discovered later as
    missing runs.

    MLflow 3.16 put the filesystem tracking store into maintenance mode and
    raises rather than using it. SQLite is still a local file, still needs no
    server and makes no network call, so a file:// default is rewritten to a
    SQLite database beside it. Set MLFLOW_ALLOW_FILE_STORE=true to keep the old
    behaviour.
    """
    if not uri:
        return uri
    if uri.startswith("sqlite:///"):
        tail = uri[len("sqlite:///") :]
        if not tail.startswith("/"):
            if tail.startswith("./"):
                tail = tail[2:]
            return "sqlite:///" + str((Path(project).resolve() / tail).resolve())
        return uri
    if uri.startswith("file://"):
        path = uri[len("file://") :]
        # lstrip("./") would strip every leading dot and slash, turning
        # "./.claude/..." into "claude/...". Strip the prefix, not a char set.
        if path.startswith("./"):
            path = path[2:]
        if not path.startswith("/"):
            path = str((Path(project).resolve() / path).resolve())
        if os.environ.get("MLFLOW_ALLOW_FILE_STORE", "").lower() in ("1", "true", "yes"):
            return "file://" + path
        db = Path(path).parent / "mlflow.db"
        try:
            db.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return "sqlite:///" + str(db)

    # Anything else is a network destination. The plugin's stated non-goal is
    # that it starts no server and makes no network call by default, and a
    # tracking URI lives in a config file that travels with a repo, so a cloned
    # repo carrying `http://…` would quietly post this machine's session
    # metadata off-box. A remote store stays possible, but as the user's
    # explicit choice in their own environment rather than a repo's.
    scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
    if scheme in REMOTE_SCHEMES:
        if os.environ.get("CONTEXT_DIET_ALLOW_REMOTE_MLFLOW", "").lower() in ("1", "true", "yes"):
            return uri
        sys.stderr.write(
            "context-diet: refusing the remote MLflow tracking URI %r from configuration. "
            "Session metadata would leave this machine. Set "
            "CONTEXT_DIET_ALLOW_REMOTE_MLFLOW=1 if that is what you want; logging to the "
            "local store until then.\n" % uri[:120]
        )
        return ""
    return uri


def _mlflow(cfg: dict, project: str = "."):
    """Return a configured mlflow module, or None. Never raises."""
    # MLflow 3.16 prints an agent hint to stderr on import. It would land in
    # every report and census run, so it is switched off before the import.
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    # Telemetry off, MLflow on: the plugin makes no network call by default.
    os.environ.setdefault("MLFLOW_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("DO_NOT_TRACK", "true")
    try:
        import mlflow  # noqa: PLC0415
    except Exception:
        return None
    try:
        uri = os.environ.get("MLFLOW_TRACKING_URI") or cfg.get("mlflow_tracking_uri")
        uri = resolve_uri(uri, project)
        if uri:
            mlflow.set_tracking_uri(uri)
        return mlflow
    except Exception:
        return None


def available(cfg: dict | None = None) -> bool:
    return _mlflow(cfg or load_config(".")) is not None


def drain_pending(cfg: dict | None = None, project: str = ".") -> int:
    """Log the Stop hook's queued session rows, then clear the queue.

    The Stop hook cannot import MLflow: doing so cost about two seconds per
    session end and printed to stderr, breaking its stdlib-only contract. It
    queues rows instead and this runs from the command layer. Returns how many
    rows were logged, and logs nothing when MLflow is absent so the queue
    survives until it is installed.
    """
    cfg = cfg or load_config(project)
    pending = state_dir(project) / "mlflow-pending.jsonl"
    if not pending.is_file():
        return 0
    if _mlflow(cfg, project) is None:
        return 0
    try:
        rows = [json.loads(line) for line in pending.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, ValueError):
        return 0
    try:
        out = log_sessions(rows, cfg, project)
    except Exception:  # noqa: BLE001 - the queue survives for the next drain
        return 0
    if out.get("available") and out.get("logged", 0) + out.get("skipped", 0) == len(rows):
        try:
            pending.unlink()
        except OSError:
            pass
    return out.get("logged", 0)


def session_key(row: dict) -> str:
    return "%s|%s" % (row.get("session_id", ""), row.get("ts", ""))


def log_sessions(rows: list, cfg: dict | None = None, project: str = ".") -> dict:
    """One MLflow run per session row, skipping rows already in the store.

    The Stop hook queues a row per session end, and a session that ends twice
    (resume, then exit) queues twice; the key is the session and the moment.
    """
    cfg = cfg or load_config(project)
    mlflow = _mlflow(cfg, project)
    if mlflow is None:
        return {"available": False, "logged": 0, "skipped": 0}
    exp = mlflow.set_experiment("context-diet/sessions")
    have = {r.data.tags.get("session_key") for r in mlflow.search_runs(
        [exp.experiment_id], output_format="list", filter_string="tags.session_key != ''")}
    logged = skipped = 0
    for row in rows:
        key = session_key(row)
        if key in have:
            skipped += 1
            continue
        with mlflow.start_run(experiment_id=exp.experiment_id,
                              run_name=str(row.get("session_id", ""))[:40]):
            mlflow.set_tags({"model": row.get("model") or "unknown",
                             "cwd": str(row.get("cwd", ""))[:250],
                             "session_key": key, "category": "session"})
            for name in NUMERIC_FIELDS:
                try:
                    mlflow.log_metric(name, float(row.get(name) or 0))
                except (TypeError, ValueError):
                    continue
        have.add(key)
        logged += 1
    return {"available": True, "logged": logged, "skipped": skipped}


def log_session(row: dict, cfg: dict | None = None, project: str = ".") -> bool:
    return bool(log_sessions([row], cfg, project).get("available"))


def adopt_legacy(ab_records: list, session_rows: list, cfg: dict | None = None,
                 project: str = ".", batch: str = "") -> dict:
    """Label runs logged before keys existed, in place. Nothing is deleted.

    The 19 Sep store holds runs with no ab_key or session_key, so a backfill
    alone would log those trials a second time beside the originals. Each
    unkeyed run is matched to a ledger row on what it measured; a match gets
    the key, a second run matching the same row is labelled a duplicate, a run
    whose model is not a Claude model is a test artifact, and anything left is
    labelled as having no ledger row rather than guessed at.
    """
    cfg = cfg or load_config(project)
    mlflow = _mlflow(cfg, project)
    if mlflow is None:
        return {"available": False}
    from mlflow.tracking import MlflowClient  # noqa: PLC0415

    client = MlflowClient()
    counts = {"adopted": 0, "duplicate": 0, "test-artifact": 0, "no-ledger": 0}

    def sig(model, config, task, trial, cache_read, wall_ms):
        return (str(model), str(config), str(task), str(trial),
                float(cache_read or 0), float(wall_ms or 0))

    exp = mlflow.get_experiment_by_name("context-diet/ab")
    if exp is not None:
        wanted = {}
        for rec in ab_records:
            wanted.setdefault(sig(rec.get("model"), rec.get("config"), rec.get("task"),
                                  rec.get("trial"), rec.get("cache_read"), rec.get("wall_ms")),
                              rec)
        taken = {r.data.tags.get("ab_key") for r in mlflow.search_runs(
            [exp.experiment_id], output_format="list", filter_string="tags.ab_key != ''")}
        legacy = [r for r in mlflow.search_runs([exp.experiment_id], output_format="list",
                                                order_by=["attributes.start_time ASC"])
                  if not r.data.tags.get("ab_key") and not r.data.tags.get("category")]
        for run in legacy:
            t, m = run.data.tags, run.data.metrics
            rid = run.info.run_id
            rec = wanted.get(sig(t.get("model"), t.get("config"), t.get("task"), t.get("trial"),
                                 m.get("cache_read"), m.get("wall_ms")))
            if rec is not None:
                key = ab_key(rec)
                if key in taken:
                    client.set_tag(rid, "category", "duplicate")
                    client.set_tag(rid, "duplicate_of", key)
                    counts["duplicate"] += 1
                else:
                    for k, v in (("ab_key", key), ("ran", str(ran_of(rec))),
                                 ("category", "ab-trial"),
                                 ("batch", batch or str(rec.get("batch", "")))):
                        client.set_tag(rid, k, v)
                    taken.add(key)
                    counts["adopted"] += 1
            elif not str(t.get("model", "")).startswith("claude-"):
                client.set_tag(rid, "category", "test-artifact")
                counts["test-artifact"] += 1
            else:
                client.set_tag(rid, "category", "no-ledger")
                counts["no-ledger"] += 1

    sexp = mlflow.get_experiment_by_name("context-diet/sessions")
    if sexp is not None and session_rows:
        wanted = {}
        for row in session_rows:
            wanted.setdefault((str(row.get("session_id", ""))[:40],
                               float(row.get("turns") or 0), float(row.get("cache_read") or 0)),
                              row)
        taken = {r.data.tags.get("session_key") for r in mlflow.search_runs(
            [sexp.experiment_id], output_format="list", filter_string="tags.session_key != ''")}
        for run in mlflow.search_runs([sexp.experiment_id], output_format="list",
                                      order_by=["attributes.start_time ASC"]):
            t, m = run.data.tags, run.data.metrics
            if t.get("session_key") or t.get("category"):
                continue
            row = wanted.get((t.get("mlflow.runName", ""), m.get("turns"), m.get("cache_read")))
            rid = run.info.run_id
            if row is not None and session_key(row) not in taken:
                client.set_tag(rid, "session_key", session_key(row))
                client.set_tag(rid, "category", "session")
                taken.add(session_key(row))
                counts["adopted"] += 1
            elif row is not None:
                client.set_tag(rid, "category", "duplicate")
                counts["duplicate"] += 1
            elif not UUID_RE.match(t.get("mlflow.runName", "")):
                # A real session id is a UUID. "s0", "big" and the like are test
                # fixtures that a 19 Sep gate logged into the real store.
                client.set_tag(rid, "category", "test-artifact")
                counts["test-artifact"] += 1
            else:
                client.set_tag(rid, "category", "no-ledger")
                counts["no-ledger"] += 1
    counts["available"] = True
    return counts


def log_scores(results: list, cfg: dict | None = None, run_name: str = "gates") -> bool:
    """One MLflow run holding every scorer's pass or fail, with its measured value."""
    cfg = cfg or load_config(".")
    mlflow = _mlflow(cfg)
    if mlflow is None:
        return False
    try:
        mlflow.set_experiment("context-diet/scorers")
        with mlflow.start_run(run_name=run_name):
            passed = 0
            for r in results:
                status = r.get("status")
                if status == "skip":
                    continue
                value = 1.0 if status == "pass" else 0.0
                passed += int(value)
                try:
                    mlflow.log_metric(r["gate"][:60], value)
                except Exception:
                    continue
                mlflow.set_tag(r["gate"][:60], str(r.get("detail", ""))[:250])
            mlflow.log_metric("scorers_passed", passed)
            mlflow.log_metric("scorers_total", len([r for r in results if r.get("status") != "skip"]))
        return True
    except Exception:
        return False




AB_METRICS = ("fresh_input", "cache_creation", "cache_read", "output",
              "wall_ms", "tool_calls", "file_reads", "num_turns")


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes")


def ran_of(record: dict) -> bool:
    """Whether a trial executed. Older ledgers have no `ran` column; their note says."""
    if str(record.get("ran", "")).strip():
        return _truthy(record.get("ran"))
    from cdlib import note_means_it_did_not_run  # noqa: PLC0415

    return not note_means_it_did_not_run(record.get("note", "") or "")


def ab_key(record: dict) -> str:
    """One A/B trial's identity: when it started, and which cell of the matrix.

    Without it a backfill run twice doubles the store, and a resumed batch
    cannot tell a carried row it already logged from one it never did.
    """
    return "|".join(str(record.get(k, "")) for k in ("ts", "model", "config", "task", "trial"))


def _existing_keys(mlflow, experiment_id: str) -> dict:
    """Map ab_key to that run's logged values, for every keyed run."""
    found = {}
    for run in mlflow.search_runs([experiment_id], output_format="list",
                                  filter_string="tags.ab_key != ''"):
        tags, metrics = run.data.tags, run.data.metrics
        found[tags.get("ab_key", "")] = {"ran": tags.get("ran", ""),
                                         "passed": metrics.get("passed"),
                                         "cache_read": metrics.get("cache_read")}
    return found


def log_ab_rows(records: list, cfg: dict | None = None, project: str = ".",
                batch: str = "") -> dict:
    """Log A/B trials, one run each, skipping any trial already in the store.

    Model and config are tags, never pooled. A trial that never ran is tagged
    ran=False and gets no `passed` metric, because a zero there would say it ran
    and failed, which is R1 again inside MLflow.
    """
    cfg = cfg or load_config(project)
    mlflow = _mlflow(cfg, project)
    if mlflow is None:
        return {"available": False, "logged": 0, "skipped": 0}
    exp = mlflow.set_experiment("context-diet/ab")
    have = _existing_keys(mlflow, exp.experiment_id)
    logged = skipped = 0
    for rec in records:
        key = ab_key(rec)
        if key in have:
            skipped += 1
            continue
        ran = ran_of(rec)
        with mlflow.start_run(experiment_id=exp.experiment_id,
                              run_name="%s-%s-%s" % (rec.get("model"), rec.get("config"),
                                                     rec.get("task"))):
            mlflow.set_tags({"model": str(rec.get("model", "")),
                             "config": str(rec.get("config", "")),
                             "task": str(rec.get("task", "")),
                             "trial": str(rec.get("trial", "")),
                             "ab_key": key, "ran": str(ran), "category": "ab-trial",
                             "batch": batch or str(rec.get("batch", ""))})
            for name in AB_METRICS:
                try:
                    mlflow.log_metric(name, float(rec.get(name) or 0))
                except (TypeError, ValueError):
                    continue
            if ran:
                mlflow.log_metric("passed", 1.0 if _truthy(rec.get("passed")) else 0.0)
        have[key] = {}
        logged += 1
    return {"available": True, "logged": logged, "skipped": skipped}


def log_ab_run(record: dict, cfg: dict | None = None, project: str = ".") -> bool:
    """One trial. Kept for callers that log as each run finishes."""
    return bool(log_ab_rows([record], cfg, project).get("available"))


def cross_check_ab(records: list, cfg: dict | None = None, project: str = ".") -> str:
    """Compare a ledger to MLflow trial by trial. The ledger is the source.

    It used to compare run counts across the whole experiment. Every model and
    every batch share that experiment, so a Fable-only ledger beside an Opus
    batch read as a divergence that was not there, and two wrong stores of the
    right size would have agreed. Matching on ab_key compares the same trials.
    """
    cfg = cfg or load_config(project)
    mlflow = _mlflow(cfg, project)
    if mlflow is None:
        return "MLflow is not installed, so the ledger is the only record. Nothing to cross-check."
    exp = mlflow.get_experiment_by_name("context-diet/ab")
    if exp is None:
        return "MLflow has no context-diet/ab experiment yet, so there is nothing to compare."
    have = _existing_keys(mlflow, exp.experiment_id)
    missing, differ = [], []
    for rec in records:
        got = have.get(ab_key(rec))
        if got is None:
            missing.append(rec)
            continue
        ran = ran_of(rec)
        want_cache = float(rec.get("cache_read") or 0)
        # `passed` is compared only for trials that ran. Runs adopted from the
        # 19 Sep store carry a 0.0 written before R1, and a never-ran trial has
        # no pass or fail to agree on.
        wrong_pass = ran and got["passed"] != (1.0 if _truthy(rec.get("passed")) else 0.0)
        if got["ran"] != str(ran) or wrong_pass or got["cache_read"] != want_cache:
            differ.append(rec)
    n = len(records)
    if not missing and not differ:
        return ("MLflow holds all %d of this ledger's runs, and every one agrees on ran, passed "
                "and cache read." % n)
    return ("Divergence: of %d ledger runs, %d are missing from MLflow and %d disagree with it. "
            "The ledger is the source of truth. This is reported, not reconciled."
            % (n, len(missing), len(differ)))


def log_census(tsv_path: str, cfg: dict | None = None, project: str = ".") -> dict:
    """One run per model per census, with one metric per H column summed over its sessions."""
    import csv  # noqa: PLC0415
    import hashlib  # noqa: PLC0415

    cfg = cfg or load_config(project)
    mlflow = _mlflow(cfg, project)
    if mlflow is None:
        return {"available": False, "runs": 0}
    raw = Path(tsv_path).read_bytes()
    # The same census file logged twice is one measurement, not two.
    census_key = hashlib.sha256(raw).hexdigest()[:16]
    exp = mlflow.set_experiment("context-diet/census")
    if mlflow.search_runs([exp.experiment_id], output_format="list", max_results=1,
                          filter_string="tags.census_key = '%s'" % census_key):
        return {"available": True, "runs": 0, "skipped": True}
    rows = list(csv.DictReader(raw.decode("utf-8").splitlines(), delimiter="\t"))
    hcols = [c for c in (rows[0].keys() if rows else []) if c[:1] == "h" and c[1:2].isdigit()]
    by_model: dict = {}
    for r in rows:
        by_model.setdefault(r.get("model") or "unknown", []).append(r)
    runs = 0
    for model, group in sorted(by_model.items()):
        with mlflow.start_run(experiment_id=exp.experiment_id, run_name="census-%s" % model[:40]):
            mlflow.set_tags({"model": model, "census_key": census_key, "category": "census"})
            mlflow.log_metric("sessions", float(len(group)))
            for col in hcols:
                total, numeric = 0.0, False
                for r in group:
                    try:
                        total += float(r.get(col) or 0)
                        numeric = True
                    except ValueError:
                        continue
                if numeric:
                    mlflow.log_metric(col, total)
        runs += 1
    return {"available": True, "runs": runs}


def delegate(command: str, payload, project: str = ".", timeout: int = 600,
             env: dict | None = None) -> dict:
    """Run one sink command where MLflow can be imported, and return its JSON reply.

    The scripts that call this run under python3, which has no MLflow. They hand
    the payload to this module under the interpreter cdlib.mlflow_python finds
    (the project's .venv on this machine). Never called from a hook: importing
    MLflow takes seconds.
    """
    import subprocess  # noqa: PLC0415

    from cdlib import mlflow_python  # noqa: PLC0415

    python = mlflow_python(project)
    if python is None:
        return {"available": False}
    try:
        proc = subprocess.run(
            [python, str(Path(__file__).resolve()), command, "--project", str(project)],
            input=json.dumps(payload), capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, MLFLOW_DISABLE_AGENT_HINT="1", MLFLOW_DISABLE_TELEMETRY="true",
                     DO_NOT_TRACK="true", **(env or {})))
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 - the ledger stands alone on any failure
        return {"available": False, "error": type(exc).__name__}


def main() -> int:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(description="context-diet MLflow sink")
    ap.add_argument("command", nargs="?", default="status",
                    choices=["status", "log-ab", "cross-check", "log-census", "log-scores",
                             "drain"])
    ap.add_argument("--project", default=".")
    args = ap.parse_args()
    cfg = load_config(args.project)
    payload = None if args.command in ("status", "drain") else json.loads(sys.stdin.read() or "null")
    if args.command == "status":
        mlflow = _mlflow(cfg, args.project)
        if mlflow is None:
            sys.stdout.write("mlflow: not installed. Every scorer still runs and writes to the "
                             "ledger.\n")
        else:
            sys.stdout.write("mlflow: %s, tracking %s\n"
                             % (mlflow.__version__, mlflow.get_tracking_uri()))
        return 0
    if args.command == "log-ab":
        if isinstance(payload, dict):
            out = log_ab_rows(payload.get("records") or [], cfg, args.project,
                              payload.get("batch", ""))
        else:
            out = log_ab_rows(payload or [], cfg, args.project)
    elif args.command == "cross-check":
        out = {"available": True, "text": cross_check_ab(payload, cfg, args.project)}
    elif args.command == "log-census":
        out = log_census(payload, cfg, args.project)
    elif args.command == "log-scores":
        out = {"available": log_scores(payload, cfg)}
    else:
        out = {"available": _mlflow(cfg, args.project) is not None,
               "drained": drain_pending(cfg, args.project)}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
