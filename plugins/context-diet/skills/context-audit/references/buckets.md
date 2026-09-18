# Buckets

Six buckets. Every section goes in exactly one. This list is fixed.

Work down the list in order and stop at the first bucket whose test the section
passes. The order matters: a duplicated build command is a Duplicate, not a
Fact, and a ritual written for a retired model is Legacy, not Ritual.

## 1. Duplicate

Test: the same instruction appears in two or more of CLAUDE.md, a skill, and a
hook.

Evidence you must have: the id or path of the other copy. Compare previews from
`extract.py`, skill frontmatter from `budget.py`'s skills index group, and hook
commands from `hook_audit.py --json`. Wording does not have to match. The
instruction has to.

Action: pick one home and cut the rest.

- A rule a hook already enforces belongs in the hook. The prose copy is the cut.
- A rule that only applies inside one task belongs in that skill's body, which
  loads on demand rather than every turn.
- A rule that applies to everything belongs in CLAUDE.md.

Both entries name each other. The cut reason names the surviving home, and the
keep reason names what was collapsed into it.

## 2. Legacy

Test: it is a workaround for a model that is no longer in use, or for a client
behaviour that no longer exists.

Signals in the preview: a model name, "when using the old model", "until the
fix lands", a version number for something that has since shipped,
"repeat the task back before starting".

Action: cut. The reason carries the date and the model or behaviour it was
written for, for example "written for the pre-tool-use behaviour of an older
model, dated 2026-09-19". `patch.py` appends the cut to `decisions.tsv` when it
applies, so the log is mechanical.

If you cannot name the model or the behaviour it was written for, it is not
Legacy. Try Ritual or Unverifiable.

## 3. Ritual

Test: remove the section and no observable behaviour changes.

Signals: "always be thorough", "do your best work", "think carefully",
"be helpful", praise of qualities rather than instructions to act. Nothing to
run, nothing to check, no file named, no command given.

Action: cut. The reason states what behaviour would change, which is none.

The dividing line against Policy: a Policy can be violated in a way you could
point at afterwards. A Ritual cannot be violated, only felt.

## 4. Unverifiable

Test: it is an opinion, and no test, linter, hook or command checks it.

Signals: "prefer", "we like", "is a smell", "wherever possible", taste about
style with nothing enforcing it. It is specific enough to be an instruction,
unlike a Ritual, but nothing decides whether it was followed.

Action: demote. Emit it as a cut whose reason names the reference file it moves
to, for example `docs/style-notes.md`. The text survives in the backup
`patch.py` takes, so moving it is a copy out of the backup. It leaves the
always-on layer, it is not destroyed.

If a test does check it, it is a Policy, not an opinion. Name the test.

## 5. Policy

Test: it is a behavioural rule, and something could tell you it was broken.

Signals: a named test file, a lint rule, a CI check, a hook, or a condition that
a hook could evaluate mechanically.

Action: keep. If a hook could enforce it more cheaply than prose, say so in the
reason and name the event:

| The rule is about | Hook event |
|---|---|
| what may be written or edited | PreToolUse, or PostToolUse to check after the fact |
| what must run before a commit or a stop | Stop |
| what must be true at the start of a session | SessionStart |
| a phrasing or a habit in the user's request | UserPromptSubmit |

A rule that a hook enforces today is a Duplicate, not a Policy. Check the hook
list before you classify.

## 6. Fact

Test: it is project-specific and you could not derive it by reading the code, or
deriving it would cost more than storing it.

Examples: the build command, the test command, the deploy target, the node
version a native feature needs, the reason two commands are the same command.

Action: keep. A Fact cut needs `--confirm-facts` on the apply step, so if you
propose one, print that flag and the consequence beside it: the agent
rediscovers the fact by grepping in every session from now on.

The title heading of a file, and any `(preamble)`, are Facts with the reason
"file title" or "file preamble". They cost a handful of tokens and removing
them tells the reader nothing.

## Tie-breaks

- It is enforceable but nothing enforces it: Policy, with the hook named. Not
  Unverifiable. Unverifiable is for opinions that cannot be enforced.
- It is both duplicated and ritual: Duplicate, and cut both copies, one for each
  reason, each priced separately.
- You cannot decide: pick the bucket whose default action you would defend to
  the user, and say in the reason why the other bucket was rejected.
- The section is empty: whatever the heading claims it is, priced at its own
  token count, usually a few tokens. Do not spend a paragraph on it.
