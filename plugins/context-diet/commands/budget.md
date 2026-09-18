---
description: Show what your context layer costs, per file and per category, with the tokeniser named.
---

Run the token budget and report it plainly.

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/budget.py" --project .`
2. If the output says the tokeniser is `chars/4`, say so in your first sentence. An estimate must never be presented as a measurement.
3. Report the always-on total first, then the largest single contributor by category.
4. Rank the categories with the invisible ones beside the visible one. Tool schemas, the skills index, agents and hook injections are invisible to the user. Instruction prose is the one they can see. Naming only the visible one is the error this plugin exists to correct.
5. Add the plan-window row:
   - Inside the desktop app, call the session usage tool if it is available, and report the window name and its reset time.
   - Outside it, write "plan window: unavailable here". Never guess a number.
6. If `.mcp.json` or user-level MCP servers are configured, name the count and point at the prefix column. To price one server, disable it and re-run: the drop in the prefix is that server's real cost.

Report the numbers. Do not propose any cut from this command.
