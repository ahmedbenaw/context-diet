# Measure first, then build — the brief for Claude Code

Ben's decision, 4 Oct: **measure before building any per-project plugin loader.**
Build nothing until the data says which plugins each project actually needs.

## Why not just build the loader

A loader that enables and disables plugins *during* a session cannot pay for
itself, and the project's own measurements say so.

The skills index sits in the cached prefix. Change the enabled set and that
prefix changes; change the prefix and the cache is void. The next turn then
re-creates the whole conversation at the write price instead of reading it at
the read price — the 12.5x gap this project exists to document.

    one toggle, conversation at 200,000 tokens   +230,000 tokens
    saved per warm message by dropping 15,305     -1,530 tokens
    break-even                                    ~150 messages

So the enabled set has to be decided **before the session's first message** and
then left alone. Per-project profiles: yes. Mid-session hot-swapping: no.

That makes the first question "what does each project actually use", which is a
measurement, not a design.

## What is already written and tested

`incoming-v4/scripts/project_inventory.py` — one pass over every transcript,
answering both open questions at once because walking 433 files twice to ask two
questions about the same files is waste.

1. **Per-project usage.** Splits plugin/skill/agent/MCP invocations by project
   directory, and prices each project's used set against a prior
   `inventory_audit.py --json` run. Output: what a project carries versus what
   it used.
2. **The .claude reads.** The census counts 485 reads of files under a `.claude`
   directory across the five non-Haiku models — a third of all reads — and
   records no filenames, so nobody can say whether that is 485 reads of a
   200-byte file or a 200KB one. This records the basenames and, where the file
   still exists, its size. Instruction files are excluded; those are H1's column.

`incoming-v4/tests/test_project_inventory.py` — 34 checks, passing on this Mac.
Covers: attribution to the right project, prices only enabled rows, never
invents a token figure without an inventory file, a truncated last line, a
malformed inventory, a missing projects directory, a deleted file reporting
"gone" rather than a guessed size, and that it writes nothing outside `--out`.

## What it was written without, and what you should check

It was written in a cloud session that **cannot reach `~/.claude/projects`**, so
every check above runs against fixtures built at test time. Before trusting a
number from it:

- Run it against the real projects directory and sanity-check the session count
  against the census's 433.
- `decode_project_dir()` is the part most likely to be wrong on real data. A
  project path is encoded by replacing separators with dashes, which is not
  reversible when the path itself contains one: `/opt/my-app` and `/opt/my/app`
  encode identically. It resolves this against the filesystem and returns
  `exact=False` whenever the answer is a guess — a moved or deleted project, or
  two equally valid readings. **Check how many of Ben's real projects come back
  inexact.** If it is more than a couple, the report needs to say so prominently
  rather than listing guessed paths.
- The usage count is a floor. A transcript shows only what a `tool_use` block
  names, so a skill the model applied without the Skill tool is invisible. The
  report says this in those words; keep it saying so.

## Run it

```bash
cd ~/Claude/Projects/context-diet/incoming-v4
python3 tests/test_project_inventory.py                       # 34 checks first

python3 scripts/inventory_audit.py --json --out ../out/inv     # if not current
python3 scripts/project_inventory.py \
    --inventory ../out/inv/inventory.json \
    --out ../out --top 40
python3 scripts/project_inventory.py \
    --inventory ../out/inv/inventory.json --out ../out --json
```

Both only read. Neither proposes a disable, and no single project's data should
produce one — a plugin unused in project A may be the whole point of project B.

## Then, and only then

With real per-project profiles in hand, the loader is a small thing: a wrapper
that writes the enabled set for a project and starts the session. Ben has asked
for it to be built from clear results, not ahead of them.

## Also settled on 4 Oct

- **Hook time:** find the culprit first, then cap it. PreToolUse is the event —
  4,939,497 ms across 72 sessions, and context-diet registers no PreToolUse hook,
  so it is another plugin's. Identify which before adding a timeout.
- **The 665x model spread was wrong.** Opus 5 has the highest *total* PreToolUse
  time and the lowest median, because it has 184 sessions to spread it over. The
  hooks are slow on every model; the median hid it. Worth a correction wherever
  that spread is quoted.
- **H5:** derive a line from the data *and* flag on cold-turn count, keeping both.
  The 90% line came from the v4 handover and the census was right to refuse it.
- **decisions.tsv row 120:** approved — when the model limit and the setting
  disagree, take the smaller. Row 159 Ben will reword himself; leave it open.
- **The 115% is closed.** Not a double-count. `fresh_input + cache_creation +
  cache_read` are session totals across every turn, so dividing them by one
  window's size was never an occupancy figure. 214 of 433 rows exceed 1.0 by that
  formula, the worst at 1,926x, which proves the formula rather than a bug.
