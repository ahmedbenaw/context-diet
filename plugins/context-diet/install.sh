#!/usr/bin/env bash
# Install or update context-diet without losing your settings or your data.
#
#   ./install.sh <destination folder>
#
# Safe to run again. Your .claude/context-diet.json and everything under
# .claude/context-diet/ - the ledger, the backups, the saved places - are never
# touched. Nothing is switched on: enabling the plugin stays your decision.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEST="${1:-}"
if [ -z "$DEST" ]; then
  echo "usage: ./install.sh <destination folder>" >&2
  exit 1
fi
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd -P)"
PLUGIN="$DEST/plugins/context-diet"

# A log per run, in the system temp directory. A fixed path in /tmp collides
# between two runs and is writable by anyone on a shared machine.
LOG="$(mktemp "${TMPDIR:-/tmp}/context-diet-install.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

version_of() {
  python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['version'])" "$1" 2>/dev/null \
    || echo unknown
}

# The starting config lives beside the marketplace in the source repo and
# travels with the plugin once installed. Look in both, or a fresh install ships
# without it and every hook falls back to built-in defaults silently.
TEMPLATE=""
for c in "$SRC/.claude/context-diet.json" "$SRC/../../.claude/context-diet.json"; do
  if [ -f "$c" ]; then TEMPLATE="$c"; break; fi
done

NEW_VER="$(version_of "$SRC/.claude-plugin/plugin.json")"
if [ -f "$PLUGIN/.claude-plugin/plugin.json" ]; then
  OLD_VER="$(version_of "$PLUGIN/.claude-plugin/plugin.json")"
  MODE=update
else
  OLD_VER=none
  MODE=install
fi

echo "context-diet: $MODE  $OLD_VER -> $NEW_VER"
echo "into: $PLUGIN"
echo

# The gates are the only thing allowed to say this worked. run_gates.py prints
# "N passed, M failed, K skipped" and exits non-zero on any failure.
run_checks() {
  if (cd "$1" && python3 tests/run_gates.py) >"$LOG" 2>&1; then
    echo "  $(grep -oE '[0-9]+ passed, [0-9]+ failed' "$LOG" | tail -1)"
    return 0
  fi
  echo "  CHECKS FAILED:" >&2
  tail -25 "$LOG" >&2
  return 1
}

# Unpacked in place? Then there is nothing to copy, and copying a directory over
# itself makes cp refuse. Verify and stop.
if [ "$SRC" = "$PLUGIN" ]; then
  echo "Already in place, so nothing to copy. Verifying instead."
  echo
  if [ -n "$TEMPLATE" ] && [ ! -f "$PLUGIN/.claude/context-diet.json" ]; then
    mkdir -p "$PLUGIN/.claude"
    cp "$TEMPLATE" "$PLUGIN/.claude/context-diet.json"
  fi
  run_checks "$PLUGIN" || exit 1
  echo
  echo "Done. Nothing has been switched on. Try it read-only:"
  echo "  cd \"$PLUGIN\" && python3 scripts/inventory_audit.py --no-calibration"
  exit 0
fi

# 1. Back up the whole existing plugin before touching any of it.
if [ "$MODE" = update ]; then
  BK="$DEST/plugins/.context-diet-backup-$(date +%Y%m%d-%H%M%S)"
  cp -R "$PLUGIN" "$BK"
  echo "backed up the old version to: $BK"
fi

# 2. Replace only the code directories, never the config and never the data.
#    Copy over the top rather than delete-then-copy: deleting is off by default
#    inside a connected folder, so a delete-first install fails halfway and
#    leaves nothing behind. Anything stale is reported at the end instead.
CODE_DIRS="hooks scripts skills commands tests fixture .claude-plugin"
mkdir -p "$PLUGIN"
for d in $CODE_DIRS; do
  [ -d "$SRC/$d" ] || continue      # never create a directory this build does not have
  mkdir -p "$PLUGIN/$d"
  cp -R "$SRC/$d/." "$PLUGIN/$d/"
done
for f in README.md CHANGELOG.md decisions.tsv install.sh; do
  [ -f "$SRC/$f" ] && cp "$SRC/$f" "$PLUGIN/$f"
done
chmod +x "$PLUGIN/install.sh" 2>/dev/null || true

STALE=""
for d in $CODE_DIRS; do
  [ -d "$PLUGIN/$d" ] || continue
  while IFS= read -r f; do
    rel="${f#"$PLUGIN"/}"
    [ -e "$SRC/$rel" ] || STALE="$STALE  $rel"$'\n'
  done < <(find "$PLUGIN/$d" -type f ! -name '*.pyc' 2>/dev/null)
done

# 3. Settings are written only the first time. An existing file is left alone
#    and the new defaults are placed beside it to compare.
mkdir -p "$PLUGIN/.claude"
if [ -z "$TEMPLATE" ]; then
  echo "WARNING: no context-diet.json found in this source tree, so no settings were" >&2
  echo "         written. Every threshold will fall back to its built-in default." >&2
elif [ -f "$PLUGIN/.claude/context-diet.json" ]; then
  echo "kept your existing settings (.claude/context-diet.json) untouched"
  cp "$TEMPLATE" "$PLUGIN/.claude/context-diet.json.new"
  echo "  the new defaults are beside it as context-diet.json.new"
else
  cp "$TEMPLATE" "$PLUGIN/.claude/context-diet.json"
  echo "wrote starting settings to .claude/context-diet.json"
fi

# 4. Prove it works before claiming anything.
echo
echo "running the checks..."
if ! run_checks "$PLUGIN"; then
  if [ "$MODE" = update ]; then
    echo "  The old version is in the backup above. Nothing of yours was lost." >&2
  fi
  exit 1
fi

# 5. Anything an older version left behind that this one does not ship.
if [ -n "$STALE" ]; then
  echo
  echo "Left over from the previous version. Harmless, but delete them when convenient:"
  printf '%s' "$STALE"
fi

# 6. Say what changed.
if [ "$MODE" = update ] && [ -f "$PLUGIN/CHANGELOG.md" ]; then
  echo
  echo "what's new:"
  awk '/^## /{n++} n==1' "$PLUGIN/CHANGELOG.md" | head -20
fi

echo
echo "Done. Nothing has been switched on: enabling the plugin is your call."
echo "Try it read-only first:"
echo "  cd \"$PLUGIN\" && python3 scripts/inventory_audit.py --no-calibration"
