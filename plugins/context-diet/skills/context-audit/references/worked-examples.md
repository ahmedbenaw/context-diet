# Worked examples

Every number here came out of a real run of `budget.py` or `extract.py` on this
machine on 2026-09-19. The tokeniser was `tiktoken:cl100k_base` in every case.
Re-running may give different totals on different files. Do not copy a figure
from this page into a report.

## 1. A five-section file, classified end to end

Input file: `plugins/context-diet/tests/fixtures/instructions/CLAUDE.md`.
`extract.py --json` reported 67 tokens for the file and these sections.

| id | heading | tokens | bucket | why |
|---|---|---|---|---|
| CLAUDE.md#e3ef2fa9 | `# Project` | 3 | Fact | file title |
| CLAUDE.md#030e8903 | `## Build` | 21 | Fact | names `npm run build` and `npm test`, not derivable |
| CLAUDE.md#5dfa0517 | `## Style` | 15 | Ritual | "Always be thorough and do your best work at all times" changes nothing observable |
| CLAUDE.md#9bf95774 | `## Legacy` | 16 | Legacy | "When using the old model, repeat the task back" is a workaround for a retired model |
| CLAUDE.md#e9b13cff | `## Duplicate` | 12 | Duplicate | restates the test command already in `## Build` |

Reconciliation: 3 + 21 + 15 + 16 + 12 = 67, which matches the file total
`extract.py` reported. Every section is accounted for.

Before and after: 67 tokens, 43 cut, 24 kept, a 64 percent reduction of this
file. That is the whole claim, and it is two numbers.

The plan:

```json
{"version": 1,
 "tokenizer": "tiktoken:cl100k_base",
 "cuts": [
   {"file": "/abs/path/tests/fixtures/instructions/CLAUDE.md",
    "section_heading": "## Style", "bucket": "Ritual", "tokens": 15,
    "reason": "Ceremony with no behavioural effect, nothing to run and nothing to check."},
   {"file": "/abs/path/tests/fixtures/instructions/CLAUDE.md",
    "section_heading": "## Legacy", "bucket": "Legacy", "tokens": 16,
    "reason": "Workaround for a model no longer in use, dated 2026-09-19."},
   {"file": "/abs/path/tests/fixtures/instructions/CLAUDE.md",
    "section_heading": "## Duplicate", "bucket": "Duplicate", "tokens": 12,
    "reason": "Repeats the test command from CLAUDE.md#030e8903, which is the home."}],
 "keep": [
   {"file": "/abs/path/tests/fixtures/instructions/CLAUDE.md",
    "section_heading": "# Project", "bucket": "Fact", "tokens": 3,
    "reason": "File title."},
   {"file": "/abs/path/tests/fixtures/instructions/CLAUDE.md",
    "section_heading": "## Build", "bucket": "Fact", "tokens": 21,
    "reason": "Build and test commands, not derivable from the code, and the home for CLAUDE.md#e9b13cff."}]}
```

`/abs/path` stands in for the real absolute path. A real plan carries the real
one, because `patch.py` opens exactly what the `file` field says.

## 2. Pricing a parent section

Input file: `plugins/context-diet/fixture/CLAUDE.md`, 683 tokens.

| heading | level | tokens |
|---|---|---|
| `# context-diet-fixture` | 1 | 63 |
| `## Commands` | 2 | 167 |
| `## Conventions` | 2 | 4 |
| `### 1. Money is integer cents, and rounding is half up` | 3 | 100 |
| `### 2. Relative imports carry an explicit .ts extension`, id CLAUDE.md#bb169e79 | 3 | 120 |
| `### 3. Services return result objects and never throw` | 3 | 67 |
| `## Gotchas` | 2 | 162 |

Sum: 63 + 167 + 4 + 100 + 120 + 67 + 162 = 683, matching the file total. The
backticks around `.ts` in the second convention's real heading are dropped in
this table for readability, which is why the row carries its id. A plan carries
the heading exactly as `extract.py` printed it.

`## Conventions` has 4 tokens of its own. A cut on it priced at 4 is wrong.
`patch.py` deletes to the next heading of the same or higher level, which is
`## Gotchas`, so the cut removes 4 + 100 + 120 + 67 = 291 tokens.

Suppose all three conventions were opinions with nothing enforcing them and the
whole block was being moved to a reference file. The correct entry is then:

```json
{"file": "/abs/path/fixture/CLAUDE.md", "section_heading": "## Conventions",
 "bucket": "Unverifiable", "tokens": 291,
 "reason": "Cutting this heading also removes CLAUDE.md#e5f0c9e6, CLAUDE.md#bb169e79 and CLAUDE.md#dc49549a, which are priced into the 291."}
```

Prefer leaf cuts. Three separate entries of 100, 120 and 67 are easier to
approve one at a time and each carries its own reason. In this file the block
should not be cut whole anyway: child 2 is a Policy, priced and kept in example
3 below, so only the other two are candidates.

## 3. A Policy kept, with the hook named

From the same file, `### 2. Relative imports carry an explicit .ts extension`,
120 tokens. It is a Policy and not an opinion: an extensionless relative import
passes the test suite and fails at runtime under Node's type stripping, so it
is breakable in a way you can point at.

```json
{"file": "/abs/path/fixture/CLAUDE.md",
 "section_heading": "### 2. Relative imports carry an explicit `.ts` extension",
 "bucket": "Policy", "tokens": 120,
 "reason": "Enforceable rule, and a PreToolUse hook on Edit and Write could reject an extensionless relative import for a fraction of 120 tokens per turn."}
```

Copy the heading from the extract's `heading` field, including any backticks.
`patch.py` compares the stripped line, so a retyped heading with a straightened
backtick will not match and the cut is skipped.

## 4. An opinion and a standing behaviour

Measured on a two-section scratch file, 62 tokens total.

```
## Design taste

Prefer functional composition over class hierarchies. Deep conditional nesting
is a smell and should be flattened wherever it appears.

## Workflow

From now on, run the full test suite before every commit, and after each task
write a one-line summary of what changed.
```

| heading | tokens | bucket | action |
|---|---|---|---|
| `## Design taste` | 29 | Unverifiable | Cut, reason names the reference file it moves to. Nothing checks "is a smell". |
| `## Workflow` | 30 | Policy | Keep, and name the hook. "From now on", "before every" and "after each" are standing behaviour, which a Stop hook enforces once instead of 30 tokens every turn. |

If a Stop hook already runs the suite, `## Workflow` is a Duplicate, not a
Policy, and the prose copy is the cut. Check `hook_audit.py --json` before
deciding.

## 5. The honest answer

`budget.py --project .` on this machine reported:

| category | tokens | files |
|---|---|---|
| skills index | 21,420 | 182 |
| commands | 15,587 | 91 |
| agents | 2,563 | 6 |
| instruction prose | 1,404 | 1 |
| output styles | 305 | 1 |
| always on, every turn | 41,279 | |

Measured static prefix, turn 1 of recent sessions: 71,706 tokens for
`claude-opus-5`, which is 30,427 tokens more than the always-on total on disk.
The residue is inventory the client assembled, most of it MCP tool schemas.

The report's first line: instruction prose is not the largest category. It is
1,404 of 71,706 tokens, 2 percent of the prefix. Deleting all of it, every
word, would save 2 percent. The skills index is fifteen times larger.

The plan for that run is `"cuts": []` and every section in `keep`. That is a
correct result, not a failed audit.

What to propose instead, in prose, with the cost of each measurement named:

1. Disable one plugin, re-run `budget.py`, and report the drop in
   `measured_prefix`. That is the plugin's real price.
2. Disable one MCP server and do the same. The `mcp` rows are config tokens and
   not schema tokens, so only the A/B prices the schemas.
3. Compare the two prefix rows already in the output. The same tree measured
   141,546 for one model and 71,706 for another, so a prefix figure is only
   comparable within one model and one enabled set.

## 6. An inventory row, and the ceiling trap

`claude-md-management`, an installed plugin, measured with
`budget.py --path <plugin>/skills --path <plugin>/commands`:

| figure | tokens | what it is |
|---|---|---|
| all files under `skills` and `commands` | 4,075 | the ceiling, includes reference files and skill bodies |
| `skills/claude-md-improver/SKILL.md`, whole file | 1,473 | still a ceiling |
| that skill's frontmatter | 101 | the always-on cost, one skill in the index |
| that skill's body | 1,372 | loads only when the skill is invoked |

The 101 came from copying the block between the two `---` lines into a scratch
file and running `budget.py --path` on it, because `budget.py` reports
frontmatter only for skills it discovers under `~/.claude/skills`.

The inventory row is 101 tokens, 1 skill, 0 agents, 1 command. Reporting 4,075
overstates this plugin's standing cost by a factor of forty. Report the ceiling
only if you label it a ceiling, and never subtract one from a prefix figure.
