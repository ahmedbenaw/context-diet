# Lint rules

Run these over the `preview` fields from `extract.py`. Do not re-read the
instruction files to run them.

## Prior art: what claude-md-improver already covers

The skill at
`~/.claude/plugins/marketplaces/claude-plugins-official/plugins/claude-md-management/skills/claude-md-improver/`
audits CLAUDE.md files already. It is read-only prior art. These checks are its
work, not ours, and context-audit does not repeat them:

| Check it covers | Where it says so |
|---|---|
| Build, test, lint and deploy commands are present | quality-criteria.md, Commands/Workflows rubric |
| Architecture and entry points are described | quality-criteria.md, Architecture Clarity rubric |
| Gotchas and non-obvious patterns are captured | quality-criteria.md, Non-Obvious Patterns rubric |
| Verbosity, filler, restating the obvious | quality-criteria.md, Conciseness rubric |
| Stale commands, outdated tech versions | quality-criteria.md, Red Flags |
| Vague instructions that cannot be executed | quality-criteria.md, Actionability rubric |
| Generic best practices that are not project-specific | update-guidelines.md, "What NOT to Add" |
| One-off fixes that will not recur | update-guidelines.md, "What NOT to Add" |
| Referenced files that no longer exist | quality-criteria.md, Red Flags and the Assessment Process |
| The same information in two CLAUDE.md files | quality-criteria.md, Red Flags |
| A discovery pass that finds every CLAUDE.md in the tree | SKILL.md, Phase 1 |

Its "Generic best practices" rule and the Ritual bucket overlap. Where they
agree, cite it rather than restating it.

Two differences run through everything below. Its output is a score out of 100
and a recommendation. Ours is a bucket and a token figure, and a proposal with
no token figure is refused by `patch.py`. Its checks are judgment calls made by
reading. Ours are run as commands, so two runs agree.

It has no notion of a model generation, so nothing there identifies the Legacy
bucket.

## What context-audit adds

### Rule A: standing behaviour written as prose

Not covered by claude-md-improver at all.

Detect these phrasings in a preview:

- "from now on", "going forward", "for the rest of this session"
- "before every", "after each", "at the start of every", "always run", "never run"
- "whenever you", "each time you", "every time you"

These describe a behaviour that should hold on every turn. Prose pays for that
behaviour on every turn whether or not the turn is relevant. A hook pays once,
at the event, and it cannot be forgotten.

Classify as Policy and keep, with the hook event named in the reason. Map the
phrasing to the event with the table in `buckets.md`. If a hook already does it,
the bucket is Duplicate and the prose copy is the cut.

Price: the section's `tokens` figure, which is what the prose costs every turn.
State it beside the hook proposal, because "a hook would be cheaper" without a
number is the kind of claim this skill exists to stop.

False positive: a phrase like "never commit secrets" is a standing behaviour
that no hook can decide reliably. Keep it as prose and say why.

### Rule B: the same rule in two or more layers

claude-md-improver flags duplicate information between CLAUDE.md files. It does
not look outside CLAUDE.md. The cross-layer case is the addition.

Compare, for every rule:

| Layer | Where to read it |
|---|---|
| instruction prose | `extract.py` previews for each CLAUDE.md and AGENTS.md |
| skills | the frontmatter `description` fields in `budget.py`'s skills index group |
| hooks | `hook_audit.py --json`, which lists every registered command by event, from settings and from every enabled plugin |

Match on the instruction, not the wording. "Run the tests before you stop" in
prose and a Stop hook that runs the tests are one rule in two layers.

Classify as Duplicate. Collapse to one home using the order in `buckets.md`.
Price every copy separately, and report the total saved as the sum of the copies
cut, never the total of all copies including the survivor.

False positive: prose that explains why a hook exists is not a duplicate of the
hook, as long as it is not also an instruction. A sentence that does both is a
Duplicate, and the explanation belongs in a reference file.

### Rule C: referenced paths that do not exist

claude-md-improver covers this by reading and judging. Its Assessment Process
says to check whether referenced files exist, Currency asks whether file
references are accurate, Actionability asks whether paths are real, and the Red
Flags list names references to deleted files. What it does not do is run the
check or price the result.

Run it:

1. Pull every path-looking string out of the previews, meaning anything with a
   `/` or a known file extension, and anything inside backticks that looks like
   a file.
2. Resolve each one against the project root, then against the repository root.
3. Test each for existence. Report the paths that fail, with the section id.

Classify a section whose only content is dead paths as Legacy, dated, with the
missing path named in the reason. If the section is still mostly true, keep it
and report the dead path as a correction for the user to make, not as a cut. A
correction is not a deletion and does not belong in `cuts`.

Price: the section's `tokens` figure when it is cut. When it is a correction,
report zero tokens saved and say so. A wrong path is a cost paid in wasted tool
calls, not in tokens, and inflating it into a token saving is a false claim.

False positive: a path inside an example command, a glob, a path on a remote
host, or a path that only exists after a build step. Check the surrounding
preview before calling it dead.

## Output of the lint pass

One row per finding: rule, section id, file, bucket, tokens, one sentence. Every
finding then appears in `cuts` or `keep` in plan.json. A finding that appears in
neither has been dropped silently, which is the failure this list exists to
prevent.
