---
description: Repair one failing scorer, with a bounded loop that stops after three attempts.
---

Takes one scorer name. Command only. A hook records a failure, it never triggers this.

1. Run the scorer alone: `python3 "${CLAUDE_PLUGIN_ROOT}/tests/run_gates.py" --only <scorer> --json`
2. If an automatic response exists, use it: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/selfheal.py" run --scorer <scorer> --project .`
   See the whole closed list with `selfheal.py table`. Anything not on that list is a proposal for the user, never an automatic act.
3. Otherwise repair it by hand, in this loop:
   - Reproduce the failure.
   - State one hypothesis.
   - Make the smallest change that tests it.
   - Re-run that scorer and then the full set.
   - Keep the change only if the scorer passes and nothing else regressed.
4. After three failed attempts on one scorer, stop and report with the evidence. Three failed fixes mean the premise is wrong, not that the fourth fix will work.

Never change a threshold to make a scorer pass. That is not a repair, it is a deletion of the check.
