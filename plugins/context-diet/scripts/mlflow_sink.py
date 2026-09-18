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

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdlib import load_config  # noqa: E402

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
    return uri


def _mlflow(cfg: dict, project: str = "."):
    """Return a configured mlflow module, or None. Never raises."""
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


def log_session(row: dict, cfg: dict | None = None) -> bool:
    """One MLflow run per session, with the model as a tag and signals as metrics."""
    cfg = cfg or load_config(".")
    mlflow = _mlflow(cfg)
    if mlflow is None:
        return False
    try:
        mlflow.set_experiment("context-diet/sessions")
        with mlflow.start_run(run_name=str(row.get("session_id", ""))[:40]):
            mlflow.set_tag("model", row.get("model") or "unknown")
            mlflow.set_tag("cwd", str(row.get("cwd", ""))[:250])
            for key in NUMERIC_FIELDS:
                try:
                    mlflow.log_metric(key, float(row.get(key) or 0))
                except Exception:
                    continue
        return True
    except Exception:
        return False


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


def log_ab_run(record: dict, cfg: dict | None = None) -> bool:
    """One MLflow run per A/B trial. Model and config are tags, never pooled."""
    cfg = cfg or load_config(".")
    mlflow = _mlflow(cfg)
    if mlflow is None:
        return False
    try:
        mlflow.set_experiment("context-diet/ab")
        with mlflow.start_run(run_name="%s-%s-%s" % (record.get("model"), record.get("config"),
                                                     record.get("task"))):
            for tag in ("model", "config", "task", "trial"):
                mlflow.set_tag(tag, str(record.get(tag, "")))
            for key in ("fresh_input", "cache_creation", "cache_read", "output",
                        "wall_ms", "tool_calls", "file_reads"):
                try:
                    mlflow.log_metric(key, float(record.get(key) or 0))
                except Exception:
                    continue
            mlflow.log_metric("passed", 1.0 if record.get("passed") else 0.0)
        return True
    except Exception:
        return False


def main() -> int:
    cfg = load_config(".")
    mlflow = _mlflow(cfg)
    if mlflow is None:
        sys.stdout.write("mlflow: not installed. Every scorer still runs and writes to the "
                         "ledger.\n")
        return 0
    sys.stdout.write("mlflow: %s, tracking %s\n"
                     % (mlflow.__version__, mlflow.get_tracking_uri()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
