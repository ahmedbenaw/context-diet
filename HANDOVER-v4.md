# Handover to Claude Code — v4 corrections for the context-diet project

Written 19 Sep 2026 by the cloud session that produced the original plan.
Read this before touching `plugins/context-diet/`.

The v4 source sits in `incoming-v4/` at this project root. It is a git repo,
80/80 checks passing on this machine. **Do not copy it over the top of
`plugins/context-diet/`.** Your build is ahead of it in several places. Take
the four things named in §6 and leave the rest.

---

## 1. What you already have that the plan did not know about

The cloud session wrote §5 of `CONTEXT-DIET-PLAN-v4.md` saying the census was
blocked. It was not blocked — you ran it. `out/census.tsv` (331 rows, 311
transcripts) and `out/census-summary.md` exist and are the best evidence in
this project. Also ahead of the plan: the `fixture/` TypeScript service, 33
gates in `out/gates.json`, 161 rows in `decisions.tsv`, the skill's
`references/` split, and `compactMetadata` as a measured source.

Anything below that contradicts your measurements loses. Measurements beat
plans, including this one.

## 2. The correction that matters most: delete the compact directive

`hooks/occupancy_monitor.py` crosses Amber at 50% and tells the model
"Please run /compact now". **Remove that.** It is the plan's worst idea and
it makes cost worse, not better.

Why. Cache reads cost 0.1x base input. A 5-minute cache write costs 1.25x.
Same tokens, **12.5x apart**. A compaction pass re-processes the whole
conversation as a *write*, not a read — and claude-code issue #70459 shows
that pass re-creating ~196k tokens instead of reading them, with the fix
gated behind a disabled flag. So every extra compaction is a full-price
re-read of everything. Compacting *more often* spends more.

What replaces it: `incoming-v4/hooks/cache_monitor.py`. It detects a cold
turn (`cache_creation > cache_read` on a record with ≥20k total), writes the
manifest, says so once, and **never emits a directive**. It logs occupancy
but never acts on it.

Keep everything else about the monitor: the write-manifest-before-speaking
order, the speak-once rule, silence below threshold, the unknown-model pause,
exit-0-always. Those were right and your gates already prove them.

## 3. Where your evidence beats the plan — the window denominator

The plan's §1 lists "read `autoCompactWindow` as the model's capacity" as
mistake #2 and has `resolve_window` refuse to consult settings at all, with a
hard-coded 1,000,000 for Opus 5 and Fable 5.1.

**Your `decisions.tsv` row 127 is better evidence than that.** You read
`compactMetadata` directly: auto-compaction fired at 1,000,499 / 1,033,177 /
1,316,455 through 11 Sep, then at 467,778 from 14 Sep, and twice on 19 Sep at
467,295 and 470,467. The setting is live. The operational ceiling on these
models today is ~467k, not 1M.

Both facts are true and they answer different questions:

| Question | Denominator |
|---|---|
| How full is the model? | model capacity (~1M, measured peaks 933k–999k) |
| How close am I to compaction? | the live trigger, measured from `compactMetadata` |

The second is the one a monitor cares about. **Keep your measured value.
Do not take `incoming-v4/lib/cdlib.py`'s `resolve_window` wholesale.** Take
only its `cache_state()` and `hit_ratio()` helpers.

Correction to the plan, for the record: v4 §1 mistake #2 is itself wrong to
the extent it says settings must never be consulted. The right rule is
narrower — *never consult a setting whose meaning has not been verified
against observed behaviour*. You verified it. The plan did not.

## 4. One open question, honestly open

The cloud session saw a 115% occupancy reading on a live session and blamed
the denominator. With a ~467k ceiling that explanation does not hold: a
session should not reach 575k real tokens if the compactor fires at 467k.
[Likely] the numerator is also wrong — `input + cache_creation + cache_read`
double-counts when a turn both re-creates and reads. **Do not close this by
adjusting the denominator.** Reproduce it against `census.tsv`, find which
term inflates, and record the answer in `decisions.tsv`.

The "impossible reading pauses instead of screaming RED" guard in
`incoming-v4` is worth taking regardless — a monitor that reports >100% is
reporting a bug in itself and should say so rather than act.

## 5. What is genuinely new: the inventory, and the 91.6%

The largest fixed cost in every session is the plugin inventory, not
instruction prose. Claude Code lists every installed skill (one line, ~13
tokens) and every agent description on **every message**, and names every MCP
tool. It already defers MCP tool *schemas* via ToolSearch; the skills index
and agent list are not deferred. Cost is linear in plugin count.

Measured on the 200 plugins visible to the cloud session: ~38,500 tokens of
inventory per message, ~37,800 of it from plugins with **zero invocations**.
Worst offenders: small-business 2,252 tok/turn, posthog 2,210 (164 skills),
claude-for-financial-advisors 1,912, brand-voice 1,864, sales 1,572. This
machine has 387 plugins, so the real figure is higher.

`incoming-v4/scripts/inventory_audit.py` prices every plugin and counts its
invocations across every transcript. Cost x zero use = disable candidate. It
writes nothing; disabling is a settings change Ben applies and can undo.

**Run it and put the output in `out/`.** This is the one number this project
does not yet have, and it is the biggest one Ben controls.

Note on scope: price only the root `.mcp.json` per plugin. Globbing
recursively picks up nested configs that are never loaded — that bug made
small-business report 70 MCP servers.

## 6. The merge, concretely

Take these four, and nothing else, from `incoming-v4/`:

1. `hooks/cache_monitor.py` → replaces `hooks/occupancy_monitor.py`.
   Update `hooks/hooks.json` to point at it. Same events.
2. `scripts/inventory_audit.py` → new. Then run it into `out/`.
3. From `lib/cdlib.py`: `cache_state()` and `hit_ratio()` only. Port them
   into your `scripts/cdlib.py`. **Not** `resolve_window` — see §3.
4. `install.sh` + `CHANGELOG.md` → a safe update path. Three defects were
   found by installing it on this Mac and are already fixed in it:
   `rm -rf` fails inside a connected folder; `SRC == PLUGIN` when unzipped in
   place makes `cp` refuse; shipped fixtures with absolute paths do not stat
   on another machine.

Then add H5 to the census — `H5_cache_hit_ratio`, `H5_cold_starts`,
`H5_cold_start_tokens`, one verdict line per model — and re-run it. Hit ratio
under 90% is the signal. This is the hypothesis the whole v4 rests on and
your census does not measure it yet.

Do **not** take: `incoming-v4/tests/run_tests.py` (your `run_gates.py` is
better and gate-shaped), the `.claude/context-diet.json` window table (§3),
`README.md`, or `RUN-ON-YOUR-MAC.md` (written for a blocker that did not
exist).

## 7. Things to record before closing

- `decisions.tsv` rows 120, 126, 128 and 159 are still marked open.
  159 says the plan states a hook budget that cannot hold — H3 in your own
  census agrees: median hook wall-clock is **438,386 ms** per session on
  Fable 5.1, 218,272 ms on Opus 4.8, 171,399 ms on Fable 5, against **360 ms**
  on Opus 5. That is a 1,200x spread across models on the same machine. It is
  not a budget problem, it is a defect somewhere in the hook chain on the
  non-Opus-5 models, and nothing in the plan predicted it. Chase it.
- H1 is **refuted on every model** — 0% to 2.7% of Read calls touch an
  instruction file. The Reddit post that started this project is wrong, and
  you have the data to say so in the report. Say it plainly.

## 8. Three things Ben can do with no code

1. `/autocompact auto` — his is pinned at 500,000.
2. `/compact` before a break, not after. Compacting cold pays full price.
3. `/usage` — read the cache hit rate. Under 90% is the signal.
