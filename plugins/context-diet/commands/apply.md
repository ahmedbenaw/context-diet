---
description: Apply an approved plan.json, backing up every file first so the change is reversible.
---

1. Show the user the plan first: each cut, its bucket, its heading and its token figure.
2. Anything in the Fact bucket needs the user to say yes explicitly. Losing a build command means the agent rediscovers it by grepping in every session from now on.
3. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/patch.py" apply --plan plan.json --project .`
   Add `--confirm-facts` only after the user has confirmed those specific cuts.
4. Report the backup directory it printed. That path is how the change is undone.
5. Suggest `/context-diet:measure` to check the change did what the plan said.

The patch script refuses to write anything outside instruction files, settings, hooks and the plugin's own directories. If it refuses, report the refusal as it is. Do not work around it.
