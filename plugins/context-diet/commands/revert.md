---
description: Restore instruction files from a backup set, verified by checksum.
---

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/patch.py" list --project .` to show the backup sets.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/patch.py" revert --project .` for the most recent set, or add `--backup <timestamp>` for a specific one.
3. Every restored file is checked against the checksum recorded when it was backed up. If one does not match, the script stops and says so. Report that rather than retrying.

Revert is meant to be boring. It restores bytes, it does not re-edit.
