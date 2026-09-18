---
description: Classify every section of your instruction files into one bucket each, with a token cost attached, and write a proposal.
---

Use the `context-audit` skill. It does the classification. This command only drives it.

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/budget.py" --json --project .` for the costs.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/extract.py" <each instruction file> --json` for the sections.
3. Load the `context-audit` skill and follow it. Every section lands in exactly one bucket: Fact, Policy, Ritual, Legacy, Duplicate, Unverifiable.
4. Write the proposal to `plan.json` in the project root. Write nothing else. This command changes no instruction file.
5. Every proposed cut carries a token figure. A proposal without one is rejected by the apply step, so do not write one.
6. You are allowed to conclude the instruction files are fine and the cost is elsewhere. If the numbers say that, say it.

Finish by reporting the token total by bucket and what applying the plan would save.
