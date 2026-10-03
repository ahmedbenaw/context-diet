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
    # SQLite, not a file store. MLflow 3.16 put the filesystem store into
    # maintenance mode and refuses a file:// URI, so a file:// default sends the
    # MCP server to an empty store while every script logs somewhere else.
    "mlflow_tracking_uri": "sqlite:///./.claude/context-diet/mlflow.db",
    # One key per signal row. The first run prints this table once.
    # Prefix tokens above which a turn whose cache_creation exceeds its
    # cache_read counts as a cache miss. Below it a turn is too small to say.
    "cold_turn_min_tokens": 20000,
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
    """The directory whose .claude/ holds this project's context-diet state.

    Anchored on the enclosing project, not on wherever the shell happens to be.
    This used to take the given path as-is, so a `cd` into a subdirectory moved
    the whole state tree with it: a handoff written from `tests/fixtures` landed
    under `tests/fixtures/.claude/`, where a session starting at the repo root
    will never look, and the acted marker that suppresses a repeat band landed
    there too. That was found live, by this plugin firing on the session that
    was building it.

    CLAUDE_PROJECT_DIR wins outright when set, because it is an explicit answer.
    Otherwise walk up for a `.git`, then for an existing `.claude`, and stop at
    the home directory: `~/.claude` is the user's global config, and treating it
    as a project root would pool every repo's state into one place.
    """
    explicit = os.environ.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return Path(explicit)
    start = Path(cwd or os.getcwd())
    try:
        start = start.resolve()
        home = Path.home().resolve()
    except OSError:
        return start
    for marker in (".git", ".claude"):
        for cand in (start, *start.parents):
            if cand == home or cand == cand.parent:
                break
            if (cand / marker).exists():
                return cand
    return start


def state_dir(cwd: str | None = None) -> Path:
    """The project's own state directory, which keeps itself out of git.

    Everything here is machine-local: handoff manifests naming absolute paths,
    a ledger, an MLflow database. This repo happens to be safe only because its
    own top-level .gitignore covers the path, which is not a property the
    plugin can carry into anyone else's repo. The directory ignores itself
    instead, so a manifest cannot be committed by an unrelated `git add -A`.
    """
    path = project_root(cwd) / STATE_DIRNAME
    try:
        if not path.is_dir():
            return path
        marker = path / ".gitignore"
        if not marker.exists():
            marker.write_text(
                "# Written by context-diet. Everything in here is local state:\n"
                "# handoff manifests, the ledger, the MLflow store, backups.\n"
                "*\n", encoding="utf-8")
    except OSError:
        pass
    return path


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
    """Every threshold with its value and whether it came from config."""
    lines = ["key\tvalue\tsource"]
    for key in sorted(cfg):
        if key.startswith("_"):
            continue
        source = "default" if cfg.get(key) == DEFAULTS.get(key) else "config"
        lines.append("%s\t%s\t%s" % (key, json.dumps(cfg[key]), source))
    return "\n".join(lines)


def effective_table_once(cfg: dict, project: str | None = None) -> str:
    """The table on the first run for this project, then "" for every later one.

    Two files promised this and nothing called it, so a wrong threshold stayed
    invisible until it misfired. The marker records the fact rather than a
    timestamp, so the promise is checkable: delete it and the table comes back.
    """
    marker = state_dir(project) / "effective-table-shown"
    try:
        if marker.exists():
            return ""
        marker.parent.mkdir(parents=True, exist_ok=True)
        state_dir(project)  # writes the ignore marker now the directory exists
        marker.write_text("shown once; delete this file to see the table again\n",
                          encoding="utf-8")
    except OSError:
        # Unwritable state is not a reason to hide the table. Print it.
        return effective_table(cfg)
    return effective_table(cfg)


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
    """Write the per-hook, per-session file flag.

    Used by the self-heal action behind the three gates that bound hook cost:
    monitor_budget_ms, session_start_budget_ms and stop_hook_timing_bound. This
    line used to name `hook_latency_budget`, which has never been a gate; the
    same stale name in selfheal's action table made `--only hook_latency_budget`
    report zero passed and exit zero, which reads like success.
    """
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


def append_decision(kind: str, subject: str, value: str, note: str = "",
                    project: str | None = None) -> None:
    """Append to this project's decision trail. Best effort, never fatal.

    The trail used to be written into the plugin's own distribution directory,
    so every project on the machine appended to the installed package's file:
    one shared, unrotated log mixing unrelated projects, in a directory a
    reinstall replaces. The rows belong to the project that produced them, and
    the plugin's committed decisions.tsv stays the curated trail it was.
    """
    import datetime

    path = state_dir(project) / "decisions.tsv"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state_dir(project)  # writes the ignore marker now the directory exists
    except OSError:
        return
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


# Start small because the answer is almost always in the last few records, and
# stop at a bound so a pathological file cannot stall a 50 ms hook.
TAIL_FIRST = 512 * 1024
TAIL_MAX = 16 * 1024 * 1024


def last_usage(transcript_path: str) -> tuple:
    """Return (usage_dict, model) from the last assistant record.

    Reads the tail of the file only. A transcript is append-only and live, so it
    is opened read-only and a truncated final line is skipped.

    The tail grows until a usage record is found. It used to be a single fixed
    512 KB read, so one large trailing record - a big tool result, a long
    assistant turn - pushed the last usage out of the window and the monitor
    returned nothing. That failure is silent and it lands exactly when a session
    is at its largest, which is when occupancy matters most.
    """
    p = Path(transcript_path or "")
    if not p.is_file():
        return ({}, "")
    try:
        size = p.stat().st_size
    except OSError:
        return ({}, "")

    window = min(size, TAIL_FIRST)
    while True:
        try:
            with p.open("rb") as fh:
                fh.seek(size - window)
                chunk = fh.read(window)
        except OSError:
            return ({}, "")
        found = _usage_in(chunk)
        if found is not None:
            return found
        if window >= size or window >= TAIL_MAX:
            return ({}, "")
        window = min(size, window * 8, TAIL_MAX)


def _usage_in(chunk: bytes):
    """(usage, model) from the last assistant record in this chunk, or None."""
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
    # None, not an empty pair: the caller distinguishes "not in this window, try
    # a bigger one" from "found nothing anywhere". Returning ({}, "") here made
    # the window never grow, which is the bug this function was split out to fix.
    return None


def context_tokens(usage: dict) -> int:
    """How many tokens were in the window for one model call.

    One assistant turn can hold several inference calls, and then the record
    carries an `iterations` list and the top-level fields are inflated relative
    to any single iteration. A three-iteration turn whose iterations were
    652,189, 656,615 and 654,052 reports 1,306,241 at the top level. How that
    total is arrived at is not documented and is not guessed at here. What is
    measured is that across the 34,617 usage records on this machine, 519 carry
    more than one iteration, and the worst inflation over the last iteration is
    exactly 2.00x. The monitor acts on whichever record it lands on, so landing
    on one of those fires Amber at half the real occupancy and spends an
    unnecessary compaction, which the plan's own limits call a guaranteed
    unrecoverable loss of transcript detail.

    The last iteration is the prompt the turn ended on, so that is the reading.
    """
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and iterations:
        last = iterations[-1]
        if isinstance(last, dict):
            usage = last
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
    )


def occupancy(usage: dict, window: int) -> float:
    if not window:
        return 0.0
    return context_tokens(usage) / float(window)


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


def model_window(model: str, cfg: dict) -> tuple:
    """The per-model window from the config table, or (0, "")."""
    table = cfg.get("context_windows") or {}
    entry = table.get(model)
    if isinstance(entry, dict):
        value = entry.get("window")
        if isinstance(value, int) and value > 0:
            return (value, "context_windows[%s] (%s)" % (model, entry.get("source", "no source")))
    if isinstance(entry, int) and entry > 0:
        return (entry, "context_windows[%s]" % model)
    return (0, "")


def window_for(model: str, cfg: dict) -> tuple:
    """(window, source). A zero window means 'unknown model' and pauses the monitor.

    Two numbers can bound a session and they are not the same measurement.
    autoCompactWindow is one global setting that says where Claude Code's own
    compactor fires; the per-model table says how much window the model actually
    has. Taking the setting first, as a single global number, measures every
    model against one denominator: with autoCompactWindow at 500,000, a
    200,000-token model reaches Amber at 250,000 real tokens, which is past the
    point that session can still exist. The monitor would never speak on it.

    So when both are known, the smaller one wins. That keeps Amber strictly
    below the built-in compactor, which is the rule, and never above the
    model's real ceiling, which is physics. It is never a silent constant.
    """
    settings = claude_settings()
    acw = settings.get("autoCompactWindow")
    acw = acw if isinstance(acw, int) and acw > 0 else 0
    mw, msource = model_window(model, cfg)
    if acw and mw:
        if mw <= acw:
            return (mw, "%s, below settings.autoCompactWindow %s" % (msource, format(acw, ",")))
        return (acw, "settings.autoCompactWindow (below %s)" % msource)
    if acw:
        return (acw, "settings.autoCompactWindow")
    if mw:
        return (mw, msource)
    table = cfg.get("context_windows") or {}
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


def median(values: list) -> float:
    """The real median: the average of the two middle values on an even count.

    The census carried its own version that returned the upper middle value and
    called it a median. On [1, 100] that is 100, not 50.5. It disagreed with
    report.py, which had the correct one, so the same word meant two things in
    one plugin. One definition here, imported by both.
    """
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def spread(values: list) -> float:
    """Observed range. The denominator of every "is this a finding" question."""
    return float(max(values) - min(values)) if values else 0.0


def heading_level(stripped: str) -> int:
    """Depth of an ATX markdown heading, or 0 when the line is not one.

    A run of hashes only opens a heading when a space follows it, so `#hashtag`
    and a bare line of `###` are not headings.
    """
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if hashes == 0 or hashes > 6:
        return 0
    rest = stripped[hashes:]
    if rest and not rest[0].isspace():
        return 0
    return hashes


def fence_mask(lines: list) -> list:
    """True for every line that sits inside a fenced code block.

    A shell comment is a hash at the start of a line, so without this a
    `# install deps` inside a bash block reads as a level-1 heading. patch.py
    learned that the hard way and grew this function; extract.py and budget.py
    did not have it, so the same file parsed into different sections depending
    on which script read it. One copy here, used by all three.
    """
    inside = False
    marker = ""
    mask = []
    for line in lines:
        stripped = line.lstrip()
        opener = ""
        for candidate in ("```", "~~~"):
            if stripped.startswith(candidate):
                opener = candidate
                break
        if opener and not inside:
            inside = True
            marker = opener
            mask.append(True)
            continue
        if opener and inside and opener == marker:
            mask.append(True)
            inside = False
            marker = ""
            continue
        mask.append(inside)
    return mask


# A run that never executed produces no evidence about the task. measure.py
# writes one of these prefixes as the note and sets ran=false. Ledgers written
# before the `ran` column existed carry only the note, so report.py reads the
# same list to recover the fact rather than counting a usage limit as a failed
# task. Both files import this so the writer and the reader cannot drift.
NON_RUN_NOTES = (
    "agent run failed:",
    "agent output was not parseable json",
)


def note_means_it_did_not_run(note: str) -> bool:
    """Did this note come from a run that never executed?"""
    text = (note or "").strip()
    return any(text.startswith(marker) for marker in NON_RUN_NOTES)


def cache_state(usage: dict, min_context: int = 20000) -> tuple:
    """Classify one assistant record's cache behaviour: warm, cold, or small.

    Cold means most of the prefix was re-created rather than read. A read is
    billed at a fraction of a write, so a cold turn is the event that actually
    costs money; an occupancy percentage is not.

    The threshold and the created-versus-read test were checked against this
    machine's 413 transcripts before being adopted. Over 34,966 ordinary turns
    above 20,000 tokens, only 1,563 (4.5%) had creation exceeding read. Over the
    44 turns immediately following a compaction, 38 did (86%). It separates the
    two populations cleanly, which is what a signal has to do to be worth acting
    on.
    """
    created = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    if created + read < min_context:
        return ("small", created, read)
    return (("cold" if created > read else "warm"), created, read)


def hit_ratio(created_total: int, read_total: int):
    """Share of prefix tokens served from cache, or None when there were none.

    None is not zero. A session that processed nothing has no hit ratio, and
    reporting 0.0 for it would put it beside a session that genuinely missed
    every time.
    """
    total = created_total + read_total
    return (read_total / float(total)) if total else None
