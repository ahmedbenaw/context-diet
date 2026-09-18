"""cdlib - shared helpers for context-diet.

Standard library only. Nothing here imports tiktoken or mlflow at module level,
so a hook that imports this module never pays for an optional dependency.

Every value that a threshold is compared against comes from config, and the
config's effective values are printable, because a silent constant is how a
wrong threshold survives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STATE_DIRNAME = ".claude/context-diet"

DEFAULTS = {
    "ceiling_tokens": 4000,
    "repeat_read_threshold": 3,
    "guard_mode": "warn",
    "tokenizer": "auto",
    "context_windows": {},
    "unknown_model_policy": "pause",
    "amber": 0.50,
    "red": 0.70,
    "mlflow_tracking_uri": "file://./.claude/context-diet/mlruns",
    # One key per signal row. The first run prints this table once.
    "static_prefix_tokens": 60000,
    "hook_latency_ms_per_tool_call": 2000,
    "hook_injected_bytes_per_session": 4000,
    "plan_window_fraction": 0.80,
    "transcript_store_bytes": 1_000_000_000,
    "transcript_store_files": 1000,
    "backups_store_bytes": 100_000_000,
    "mlflow_store_bytes": 100_000_000,
    "stale_handoff_days": 7,
    "ledger_bytes": 10_000_000,
    "storage_signal_ttl_seconds": 86400,
    "session_start_budget_ms": 200,
    "monitor_budget_ms": 50,
    "duplicate_hook_registrations": 1,
}


def project_root(cwd: str | None = None) -> Path:
    """The directory whose .claude/ holds this project's context-diet state."""
    return Path(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def state_dir(cwd: str | None = None) -> Path:
    return project_root(cwd) / STATE_DIRNAME


def config_path(cwd: str | None = None) -> Path:
    return project_root(cwd) / ".claude" / "context-diet.json"


def load_config(cwd: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    path = config_path(cwd)
    try:
        if path.is_file():
            user = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
    except (OSError, ValueError):
        # A malformed config must not take a session down. Defaults win and the
        # command layer reports it; a hook stays silent.
        pass
    return cfg


def effective_table(cfg: dict) -> str:
    lines = ["key\tvalue\tsource"]
    for key in sorted(cfg):
        source = "default" if cfg.get(key) == DEFAULTS.get(key) else "config"
        lines.append("%s\t%s\t%s" % (key, json.dumps(cfg[key]), source))
    return "\n".join(lines)


def disabled(hook_name: str, session_id: str = "", cwd: str | None = None) -> bool:
    """Both kill switches. Checked first by every hook, before any work."""
    if os.environ.get("CONTEXT_DIET_OFF") == "1":
        return True
    if not session_id:
        return False
    flag = state_dir(cwd) / "disabled" / ("%s.%s" % (hook_name, session_id))
    try:
        return flag.exists()
    except OSError:
        return False


def disable_hook(hook_name: str, session_id: str, cwd: str | None = None) -> Path:
    """Write the per-hook, per-session file flag. Used by hook_latency_budget."""
    d = state_dir(cwd) / "disabled"
    d.mkdir(parents=True, exist_ok=True)
    flag = d / ("%s.%s" % (hook_name, session_id))
    flag.write_text("disabled by context-diet\n", encoding="utf-8")
    return flag


TOKENIZER_CACHE = PLUGIN_ROOT / ".tiktoken-cache"


class Tokenizer:
    """A token count whose provenance is always stated.

    Falls back to characters divided by four when the real tokeniser is not
    usable offline. The name is carried with the number so a reader can never
    mistake an estimate for a measurement.

    tiktoken downloads its encoding on first use. The plugin makes no network
    call by default, so the encoding is used only when it is already cached on
    disk. Without that check a first run blocks for 30 seconds on a network
    timeout and then silently reports an estimate anyway.
    """

    def __init__(self, mode: str = "auto", allow_download: bool = False):
        self.name = "chars/4"
        self.note = ""
        self._enc = None
        if mode not in ("auto", "tiktoken"):
            self.note = "tokenizer pinned to chars/4 by config"
            return
        cache = os.environ.get("TIKTOKEN_CACHE_DIR") or str(TOKENIZER_CACHE)
        os.environ["TIKTOKEN_CACHE_DIR"] = cache
        cached = False
        try:
            cached = any(Path(cache).iterdir())
        except OSError:
            cached = False
        if not cached and not allow_download:
            self.note = (
                "tiktoken encoding not cached; run scripts/preflight.py --fetch-tokenizer once "
                "to use the real tokeniser offline"
            )
            return
        try:
            import tiktoken  # noqa: PLC0415

            self._enc = tiktoken.get_encoding("cl100k_base")
            self.name = "tiktoken:cl100k_base"
        except Exception as exc:
            self._enc = None
            self.note = "tiktoken unavailable (%s)" % type(exc).__name__

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            try:
                return len(self._enc.encode(text, disallowed_special=()))
            except Exception:
                pass
        return (len(text) + 3) // 4


def append_ledger(row: dict, cwd: str | None = None, cfg: dict | None = None) -> None:
    """Append one TSV row. Rotates by size so the ledger cannot grow unbounded.

    Nothing is ever discarded: rotation renames, it does not truncate.
    """
    cfg = cfg or load_config(cwd)
    d = state_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    ledger = d / "ledger.tsv"
    limit = int(cfg.get("ledger_bytes") or DEFAULTS["ledger_bytes"])
    try:
        if ledger.exists() and ledger.stat().st_size > limit:
            import datetime

            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            ledger.rename(d / ("ledger-%s.tsv" % stamp))
    except OSError:
        pass
    header = list(row.keys())
    write_header = not ledger.exists()
    try:
        with ledger.open("a", encoding="utf-8") as fh:
            if write_header:
                fh.write("\t".join(header) + "\n")
            fh.write(
                "\t".join(str(row[k]).replace("\t", " ").replace("\n", " ") for k in header) + "\n"
            )
    except OSError:
        pass


def append_decision(kind: str, subject: str, value: str, note: str = "") -> None:
    """Append to the plugin's decision trail. Best effort, never fatal."""
    import datetime

    path = PLUGIN_ROOT / "decisions.tsv"
    line = "\t".join(
        [
            datetime.datetime.now().isoformat(timespec="seconds"),
            kind,
            subject,
            str(value),
            note.replace("\t", " ").replace("\n", " ")[:400],
        ]
    )
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def read_stdin_json() -> dict:
    """Hook stdin. Any failure yields an empty dict, never an exception."""
    import sys

    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def last_usage(transcript_path: str) -> tuple:
    """Return (usage_dict, model) from the last assistant record.

    Reads the tail of the file only. A transcript is append-only and live, so it
    is opened read-only and a truncated final line is skipped.
    """
    p = Path(transcript_path or "")
    if not p.is_file():
        return ({}, "")
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            window = min(size, 512 * 1024)
            fh.seek(size - window)
            chunk = fh.read(window)
    except OSError:
        return ({}, "")
    lines = chunk.split(b"\n")
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        model = msg.get("model") or ""
        if model == "<synthetic>":
            continue
        usage = msg.get("usage") or {}
        if usage:
            return (usage, model)
    return ({}, "")


def occupancy(usage: dict, window: int) -> float:
    if not window:
        return 0.0
    total = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )
    return total / float(window)


def claude_settings() -> dict:
    """The user's settings. CONTEXT_DIET_SETTINGS overrides the path for tests.

    Without that seam the unknown-model path is untestable on any machine that
    sets autoCompactWindow, because the lookup finds a window before it ever asks
    the per-model table.
    """
    override = os.environ.get("CONTEXT_DIET_SETTINGS")
    path = Path(override) if override else (Path.home() / ".claude" / "settings.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def window_for(model: str, cfg: dict) -> tuple:
    """(window, source). A zero window means 'unknown model' and pauses the monitor.

    Lookup order is settings, then the per-model table, then nothing. It is never
    a silent constant, because a wrong denominator makes every threshold wrong.
    """
    settings = claude_settings()
    acw = settings.get("autoCompactWindow")
    if isinstance(acw, int) and acw > 0:
        return (acw, "settings.autoCompactWindow")
    table = cfg.get("context_windows") or {}
    entry = table.get(model)
    if isinstance(entry, dict):
        value = entry.get("window")
        if isinstance(value, int) and value > 0:
            return (value, "context_windows[%s] (%s)" % (model, entry.get("source", "no source")))
    if isinstance(entry, int) and entry > 0:
        return (entry, "context_windows[%s]" % model)
    if cfg.get("unknown_model_policy") == "conservative":
        known = []
        for v in table.values():
            if isinstance(v, dict) and isinstance(v.get("window"), int):
                known.append(v["window"])
            elif isinstance(v, int):
                known.append(v)
        if known:
            return (min(known), "smallest known window (unknown_model_policy=conservative)")
    return (0, "unknown")
