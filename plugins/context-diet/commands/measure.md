---
description: Run the A/B harness against the committed fixture and report before and after, per model.
---

This is the part that makes the original claim testable.

1. Check the fixture is intact: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/measure.py" tasks`
2. Run it: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/measure.py" run --models claude-opus-5,claude-fable-5-1 --trials 3 --project .`
   That is 45 runs per model. Tell the user the cost before starting, and run the two primary models first. Other models are a separate opt-in batch.
3. Render it: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --runs .claude/context-diet/ab/runs.tsv --project .`
4. Read the report out as it is written. It refuses to state any difference smaller than the observed spread, and it never pools models. Do not restate an inconclusive result as a finding.
5. If Bare beats Lean beats Fat across the tasks, say the post was right. That answer has to be reachable or this is not a measurement.

The harness only ever runs against the committed fixture. It refuses any other path.
