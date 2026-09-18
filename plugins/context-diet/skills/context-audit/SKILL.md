---
name: context-audit
description: Classify every section of the instruction layer into one bucket with a token cost attached, then write a plan.json proposal. Use when asked to audit, price, shrink or explain the cost of CLAUDE.md, AGENTS.md, or a project's always-on context.
---

# Context audit

## What this skill does

One job: classification.

Read a token budget and a section extract produced by the scripts. Put every
section of every instruction file into exactly one bucket. Attach its token
cost. Write a plan.json proposal.

This skill never edits a file and never runs the apply step. It produces a
proposal that a human approves and a script applies.

## The governing rule

No cut without a measurement. No claim without a before and after.

You may not propose a deletion you cannot price in tokens. A proposal with no
token figure is rejected by the apply step: `patch.py` refuses the whole plan if
any cut is missing its `tokens` key, and applies nothing.

Every number you print carries the name of the tokeniser that produced it. If
you did not read a number out of a script's output, you do not have that number.

## Inputs

Run these. Do not open the instruction files to read them whole. A tool that
pulls a file into the window to decide the file is too big has already lost the
argument.

| Command | What it gives you |
|---|---|
| `scripts/budget.py --project DIR --json` | `tokenizer` name, `groups` of per-file and per-section token counts, `mcp` config rows, `measured_prefix` per model |
| `scripts/extract.py ABSPATH --json` | `sections` with stable `id`, `heading`, `level`, `start_line`, `end_line`, `tokens`, `sha256`, and a 160 character `preview` |
| `scripts/patch.py apply --plan plan.json` | Applies an approved plan. You never call this. Print it for the user. |
| `scripts/patch.py revert` | Restores the last backup. You never call this either. |

Pass absolute paths to `extract.py`, because plan.json needs absolute paths.

## Procedure

1. Run `budget.py --json` from the project root. Record the `tokenizer` value
   verbatim. It goes in plan.json.
2. Take the instruction file list from the `instruction prose` group.
3. Run `extract.py` on each of those files, from the project root, with
   absolute paths, `--json`. If its `tokenizer` differs from `budget.py`'s,
   stop and report it. Both scripts read the tokeniser from config relative to
   the working directory, and two counts made under two tokenisers are not one
   measurement.
4. Put every section in exactly one bucket. Every section, including the title
   heading and any `(preamble)`. The decision test for each bucket is in
   `references/buckets.md`.
5. Run the lint checks in `references/lint-rules.md` over the previews. Classify
   what they find. Do not re-read the files.
6. Price every entry from the extract's `tokens` field. For a heading with child
   sections under it, see "Pricing a parent section".
7. Build the inventory table and the cost ranking described below.
8. Write `plan.json` in the project root, and write nothing else. Print the
   report: the token total per bucket, and what applying the plan would save.
   Stop, and print the apply command for the user to run.

## The buckets

| Bucket | Meaning | Default action |
|---|---|---|
| Fact | Project-specific and non-derivable (build command, test command, deploy target) | Keep. Cutting one requires explicit confirmation. |
| Policy | A behavioural rule that is actually enforceable | Keep; note if a hook could enforce it more cheaply |
| Ritual | Ceremony with no behavioural effect ("always be thorough") | Cut |
| Legacy | A workaround for a model no longer in use | Cut, dated and logged |
| Duplicate | Stated in two or more of CLAUDE.md, a skill, a hook | Collapse to one home |
| Unverifiable | An opinion with no test attached | Demote to a reference file, out of the always-on layer |

Do not invent a seventh bucket. If a section resists the table, pick the bucket
whose default action you would defend and say why in the `reason`.

## Pricing a parent section

`patch.py` cuts from the heading line to the next heading of the same or higher
level, so cutting `## Conventions` also removes every `###` beneath it.
`extract.py` counts a section up to the next heading of any level, so a parent's
own count excludes its children.

Propose cuts at leaf sections. If you do propose a parent, `tokens` is the
parent plus every descendant, and the `reason` names the descendant ids. A
parent priced at its own count is a mispriced cut. A worked case is in
`references/worked-examples.md`.

## What the apply step will and will not accept

- `section_heading` must be the extract's `heading` field verbatim, including
  the `#` characters. `patch.py` matches on `line.strip() == heading`.
- The `(preamble)` pseudo-section has no heading line, so `patch.py` cannot cut
  it. Classify it and keep it. If it should go, say so in the report in prose
  and leave it to the user.
- A cut whose bucket is `Fact` needs `--confirm-facts`. Print that flag beside
  any Fact cut you propose.
- `patch.py` writes only inside the instruction layer. A plan naming a source
  file is refused whole and nothing is applied.

## How six buckets fit a two-list contract

The contract has `cuts` and `keep` only. This is the mapping this skill uses.
It is an interpretation, stated here so a reader can overrule it.

- Duplicate: the copy outside the chosen home goes in `cuts`, the home goes in
  `keep`, and each `reason` names the other section's id.
- Unverifiable: emit as a cut whose `reason` names the reference file it should
  move to. The text survives in the timestamped backup `patch.py` takes, so the
  relocation is a copy out of the backup, not a retype.
- Legacy: the `reason` carries the date and the model name it was written for.
  `patch.py` logs the cut to `decisions.tsv` when it applies.
- Policy: a `keep` whose `reason` names the hook event that could enforce it
  more cheaply, when one could.
- Fact and Ritual: `keep` and `cuts` respectively, as the table says.

## The plan.json contract

```json
{"version": 1,
 "tokenizer": "<name from budget.py>",
 "cuts": [{"file": "<abs path>", "section_heading": "<exact heading line>", "bucket": "Ritual",
           "tokens": 123, "reason": "<one sentence>"}],
 "keep": [{"file": "...", "section_heading": "...", "bucket": "Fact", "tokens": 45,
           "reason": "..."}]}
```

- `version` is always 1.
- `tokenizer` is copied from `budget.py`, not guessed.
- `file` is absolute.
- Every entry in both lists has a `tokens` integer.
- `reason` is one sentence.
- Every section in every audited file appears in exactly one of the two lists,
  or is named in the `reason` of a parent cut that prices it.
  The sum of both lists reconciles to the file totals `extract.py` reported.
  Print that reconciliation.

## Inventory

The audit also prices inventory, so a plugin can be disabled by evidence rather
than by feel. For each plugin, report:

| Column | Where it comes from |
|---|---|
| skills index tokens | `budget.py` `groups["skills index"]`, which counts frontmatter only, because that is what loads every turn |
| skill count | the file count in the same group |
| agent count | `groups["agents"]` |
| command count | `groups["commands"]` |

`budget.py`'s discovery walks `~/.claude/skills`, `agents`, `commands` and
`output-styles`, and reports every file path, so group those paths by the plugin
directory they sit in. For a plugin directory outside that tree, run
`budget.py --path <plugin>/skills --json`. Label that figure a ceiling: `--path`
measures whole files, so it includes skill bodies, which load on demand and are
not in the prefix.

For a skill whose frontmatter cost `budget.py` does not report, copy the block
between the two `---` lines into a scratch file and run `budget.py --path` on
that. That is the always-on figure. The rest of the file is body.

The price of a plugin is the drop in `measured_prefix` when it is disabled and
`budget.py` is run again. That is the same before-and-after `budget.py` already
prescribes for MCP servers. Nothing else in this section is a measurement of the prefix,
and the report must not present it as one.

## Reporting rule

Every report ranks the cost categories, with the invisible ones beside the
visible one. Naming only instruction prose is the exact error this plugin exists
to correct.

| Category | Source | Is it a measurement |
|---|---|---|
| instruction prose | `budget.py` groups | Yes, tokenised from disk |
| skills index | `budget.py`, frontmatter only | Yes |
| agents, commands, output styles | `budget.py` groups | Yes |
| hook injections | `hook_audit.py --json` for registrations and worst-case timeouts, `context_census.py` H4 for injected bytes per session | Yes |
| MCP tool schemas | `measured_prefix` minus the always-on total | No, it is a residue. The `mcp` rows are config tokens, not schema tokens. Price one server by disabling it and re-running. |

Rank them by tokens, largest first. If instruction prose is not the largest,
say so in the first line of the report.

This ranking is a snapshot of one tree. Before and after across measured
sessions is `report.py`'s job, from runs recorded by `measure.py`. Do not
rebuild it here.

## The honest answer

This skill is allowed to conclude that the instruction files are fine and the
cost is elsewhere.

When that is what the numbers say, `cuts` is `[]`, every section is in `keep`,
and the report names the larger category with its figure. A run that cannot
return that answer is not a measurement tool, it is a tool for deleting things.
The worked case is in `references/worked-examples.md`: 1,404 tokens of
instruction prose against a 71,706 token measured prefix.

## What this skill never does

- It never edits an instruction file. `patch.py` does the writing.
- It never runs `patch.py apply` on its own initiative. It prints the command.
- It never invents a token figure, and never reports one without the tokeniser
  name beside it.
- It never proposes a cut it cannot price.

## Reference files

- `references/buckets.md`: the decision test for each bucket, and the tie-breaks.
- `references/worked-examples.md`: real runs with real numbers, including a
  parent-section pricing case and the honest-answer case.
- `references/lint-rules.md`: the checks to run, and which of them
  `claude-md-improver` already covers.
