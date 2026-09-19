# context-diet

A Claude Code plugin that measures what your context layer actually costs, proposes cuts with a number on each, applies them reversibly, watches how full the window is, and saves your state to disk before compaction.

One rule governs all of it. No cut without a measurement, no claim without a before and after. A change with no number attached is not accepted.

## Why it exists

A post in r/ClaudeCode said the model is slow because it re-reads your CLAUDE.md files, and that deleting them fixes it. The symptom is real. The cause and the cure are not.

So the first thing this plugin does is test the claim instead of acting on it. On the machine it was built for, the census over 311 transcripts found this:

| Model | Reads of CLAUDE.md or AGENTS.md | Reads of other files under `.claude/` | Total reads |
|---|---|---|---|
| Opus 4.8 | 0 | 333 | 582 |
| Opus 5 | 9 | 70 | 334 |
| Fable 5 | 0 | 76 | 182 |
| Fable 5.1 | 2 | 45 | 113 |

Instruction files were not being re-read. What did get read under `.claude/` was skill and plugin files, which is a different cost with a different fix. The measured always-on layer came to about 41,000 tokens, of which the global CLAUDE.md was 1,404. The post points at the one item a user can see and ignores the ones they cannot.

The plugin is built so the opposite answer can come back too. If the A/B run says removing the file helped, the report says the post was right.

## What you get

Four slash commands and a skill.

| Command | What it does | What it writes |
|---|---|---|
| `/context-diet:budget` | Token table per file and category, tokeniser named | Nothing |
| `/context-diet:audit` | Classifies every section into one bucket with a cost | `plan.json` only |
| `/context-diet:apply` | Applies the plan after backing every file up | Instruction files, backups |
| `/context-diet:revert` | Restores a backup set, verified by checksum | Instruction files |
| `/context-diet:measure` | The A/B run against the bundled fixture | Ledger, MLflow |
| `/context-diet:heal` | Repairs one failing check, stops after three tries | Plugin code, logged |

And five hooks that run on their own, from the start of a session and throughout it.

| Hook | When | What it does |
|---|---|---|
| Occupancy monitor | After each tool call, and on each prompt | At 50% of the window it saves your state and asks the model to compact. At 70% it arms a handoff. Below that it says nothing at all. |
| Session start | New session | Loads an armed handoff once, or says one line if the last session crossed a threshold, or stays quiet |
| Pre-compact | Before the built-in compactor runs | Saves state if nothing saved it yet |
| Stop ledger | End of session | Appends one row of totals. Checks storage sizes once a day. Injects nothing. |
| Read tracker | After each read | Counts repeat reads of an unchanged file and suggests hoisting a summary once |

## What it will not do

- It never edits your source code. Instruction files, settings, hooks and its own directories, nothing else.
- It never deletes. It backs up, patches, and restores byte-identical originals.
- It starts no server and makes no network call by default.
- It is allowed to conclude your files are fine and the cost is elsewhere. If it could not return that answer it would not be a measurement tool.
- It never changes a threshold to make one of its own checks pass.

## Install

```bash
git clone https://github.com/ahmedbenaw/context-diet.git
cd context-diet
```

Then in Claude Code:

```
/plugin marketplace add /absolute/path/to/context-diet
/plugin install context-diet
```

Enabling it at user level is what makes the hooks run in every session.

Optional extras, neither required:

```bash
python3 plugins/context-diet/scripts/preflight.py --fetch-tokenizer   # real token counts, one network call, then offline
pip install 'mlflow[mcp]>=3.5.1'                                      # logs every check as a tracked run
```

Without `tiktoken` the counts fall back to characters divided by four, and every report says so. Without MLflow every check still runs and writes to the ledger.

## Three things that were asked for and do not exist

The honest substitute ships instead. Nothing here pretends to be the original.

| Asked for | Reality | What ships |
|---|---|---|
| A hook runs compaction automatically | No hook can invoke it | The hook saves state and asks; the model compacts |
| A hook opens a new session in the same window | No hook can drive the client | The handoff is written and armed; you start the session and it loads in one line |
| Compaction with zero loss | Compaction replaces turns with a summary, so it is lossy by definition | State is on disk before compaction. No state is lost. Transcript detail leaves the window but stays on disk and is named in the manifest. |

## The handoff

At 50% the monitor writes a file of checkable pointers, verifies it against disk, and only then asks for compaction. Manifest first, always: compacting first means summarising from a window you already damaged.

Every field is something that can be checked. A commit that must resolve. A path that must stat. A checksum that must match. Prose is prohibited, because prose is where a model smooths over a gap it cannot recall. A field that fails verification is dropped and named. It is never repaired, because a plausible replacement is the exact failure the format exists to prevent.

## Turning it off

```bash
export CONTEXT_DIET_OFF=1
```

That disables everything without uninstalling. To silence one hook for one session, write a file at `.claude/context-diet/disabled/<hook>.<session_id>`. Every hook checks for its own flag before doing any work.

Every hook exits zero on any failure and says nothing. A monitor bug can never trap a working session.

## Checking it yourself

```bash
python3 plugins/context-diet/tests/make_fixtures.py --heavy
python3 plugins/context-diet/tests/run_gates.py
```

Thirty-three checks, each one a command rather than an opinion. They cover the census, the monitor's silence below threshold, its 50 ms budget, the unknown-model pause, the manifest round trip, apply and revert being byte-identical, the refusal to write outside the instruction layer, storage signals deleting nothing, and the report refusing to state a difference smaller than its own spread.

## Running the A/B yourself

The harness drives `claude -p`, so that command has to be signed in. Check it first:

```bash
claude auth status
```

If it reports `"loggedIn": false`, sign in again. Either command works, and the second one is the documented path for unattended runs:

```bash
claude auth login
```

```bash
claude setup-token
```

Then run the matrix and the report:

```bash
python3 plugins/context-diet/scripts/measure.py run --models claude-opus-5,claude-fable-5-1 --trials 3
```

```bash
python3 plugins/context-diet/scripts/report.py --runs .claude/context-diet/ab/runs.tsv
```

That is 45 runs per model. If the runner cannot authenticate, every trial records the failure in its note rather than reporting zero tokens as if the run were cheap. The plumbing is verified separately with `--dry-run`, where every pass check correctly fails because no agent did any work.

## Checking that the hooks fire

Registering a hook is not the same as watching it run. Hooks load when a session starts, so the session that installs the plugin never runs them. Start a fresh session, do any small task, end it, then look for the row the Stop hook appends:

```bash
cat .claude/context-diet/ledger.tsv
```

One row means the Stop hook fired. For the rest, run the census over that session's transcript and read its hook columns:

```bash
python3 plugins/context-diet/scripts/context_census.py --out census.tsv
```

The plugin's own hooks are expected to cost under 200 ms in total for that session. Over that, it fails its own gate.

## Known limits

- Compaction loses transcript detail every time. The manifest bounds what that costs. It does not make the loss zero.
- Hallucination is reduced, not eliminated. Checkable pointers and a 25-word ceiling on free text remove most of the surface, not all of it.
- Starting the next session is one click. No hook can drive the client window.
- A wrong window size makes every threshold wrong, which is why an unknown model pauses monitoring and says so instead of guessing.
- Three trials per configuration is thin, and the report says so rather than dressing noise as a finding.
- MLflow 3.16 put its filesystem store into maintenance mode, so the default is a local SQLite file. Still local, still no server, still no network call.
