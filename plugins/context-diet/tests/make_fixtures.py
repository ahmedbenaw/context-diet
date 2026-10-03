#!/usr/bin/env python3
"""make_fixtures.py - build every fixture the gates need.

Without these the monitor is untestable, so they are built before Increment 3
runs. Each fixture is generated, never hand-edited, so a reviewer can rebuild
them and get byte-identical files.

Usage:
    make_fixtures.py [--out DIR] [--heavy]
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

WINDOW = 500_000


LEAN_CLAUDE_MD = """\
# context-diet-fixture

A tiny order-quoting service. It is deliberately offline: no network calls, no
database, no clock, no randomness. `demoQuote()` in `src/index.ts` returns the
same object on every run, which is what makes this repo safe to assert against.

## Commands

| Purpose   | Command             |
| --------- | ------------------- |
| Build     | `npm run build`     |
| Typecheck | `npm run typecheck` |
| Test      | `npm test`          |

`build` and `typecheck` are the same command (`tsc --noEmit`). Nothing is ever
emitted: the entrypoint is executed straight from TypeScript, so there is no
`dist/` and `tsc` exists here purely as a checker.

Running the entrypoint (`node src/index.ts`) relies on Node's native type
stripping and therefore needs **Node 22.18 or newer**, where it is on by
default. Verified on Node 24.18.0. There is no `tsx` or `ts-node` here.

## Conventions

### 1. Money is integer cents, and rounding is half up

Every monetary value in `src/` is a whole number of cents. Never introduce a
float amount and never round-trip through `formatCents`, which is display only.
`applyDiscountPercent` resolves a half-cent result upward, so 1250 cents at 15
percent is 1063 and not 1062. A test in `tests/pricing.test.ts` pins that
boundary specifically.

### 2. Relative imports carry an explicit `.ts` extension

Write `import { makeIdKey } from "../util/idKey.ts";`, not `"../util/idKey"`.
This is load bearing rather than cosmetic: `tsconfig.json` sets
`allowImportingTsExtensions` and `verbatimModuleSyntax`, and `src/index.ts` is
run directly by Node's native type stripping, which does no extension
resolution at all. Vitest tolerates a missing extension; Node does not, so an
extensionless import passes the test suite and then fails at runtime.

### 3. Services return result objects and never throw

`reserveOrder` returns `{ ok: true, ... }` or `{ ok: false, shortages }`. It is
all or nothing: if any line is short, nothing at all is reserved. Follow the
same shape for any new service function rather than throwing.

## Gotchas

- **Vitest does not typecheck.** A required field added to an interface will
  keep a green suite while `npm run typecheck` fails. Run both.
- **Vitest globals are off.** `tsconfig.json` sets `types: ["node"]`, so every
  test file imports `describe`, `it` and `expect` from `"vitest"` explicitly.
- **`noUncheckedIndexedAccess` is on.** Indexing an array or a `Map.get` yields
  `T | undefined` and must be narrowed before use.
- All lookup keys are built by `makeIdKey` in `src/util/idKey.ts`, which trims
  and lowercases. Never assemble a `Map` key by hand.

"""


def assistant(model: str, cc: int = 0, cr: int = 0, inp: int = 2, out: int = 50,
              tool: dict | None = None) -> dict:
    content = []
    if tool:
        content.append({"type": "tool_use", "id": tool.get("id", "t1"),
                        "name": tool["name"], "input": tool.get("input", {})})
    return {
        "type": "assistant",
        "sessionId": "fixture",
        "uuid": "u%d" % time.time_ns(),
        "message": {
            "role": "assistant",
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
                "output_tokens": out,
            },
        },
    }


def hook_success(name: str, event: str, ms: int, stdout: str = "") -> dict:
    return {
        "type": "attachment",
        "sessionId": "fixture",
        "attachment": {
            "type": "hook_success",
            "hookName": name,
            "hookEvent": event,
            "durationMs": ms,
            "stdout": stdout,
            "stderr": "",
            "exitCode": 0,
            "command": "echo",
        },
    }


def injected(text: str) -> dict:
    return {
        "type": "attachment",
        "sessionId": "fixture",
        "attachment": {"type": "hook_additional_context", "content": [text]},
    }


def injected_by(hook_name: str, text: str) -> dict:
    """An injection that names the hook that made it.

    Every one of the 763 such attachments on this machine carries its own
    hookName. H4 used to charge the bytes to whichever hook had most recently
    logged a hook_success, which is a different hook whenever a chain is
    involved.
    """
    return {
        "type": "attachment",
        "sessionId": "fixture",
        "attachment": {
            "type": "hook_additional_context",
            "hookName": hook_name,
            "content": [text],
        },
    }


def compaction(trigger: str, pre: int, post: int, dropped: int) -> dict:
    """One compaction record, the only place the real compactor ceiling is stated."""
    return {
        "type": "system",
        "sessionId": "fixture",
        "uuid": "u%d" % time.time_ns(),
        "compactMetadata": {
            "trigger": trigger,
            "preTokens": pre,
            "postTokens": post,
            "cumulativeDroppedTokens": dropped,
            "durationMs": 4200,
        },
    }


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    return path


def write_jsonl(path: Path, records: list, truncate_last: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    text = "\n".join(lines) + "\n"
    if truncate_last and lines:
        text = "\n".join(lines[:-1]) + "\n" + lines[-1][: max(8, len(lines[-1]) // 2)]
    path.write_text(text, encoding="utf-8")
    return path


def occupancy_records(model: str, fraction: float) -> list:
    """A session sitting at a chosen fraction of a 500k window."""
    total = int(WINDOW * fraction)
    return [
        assistant(model, cc=1000, cr=0),
        assistant(model, cc=0, cr=max(0, total - 2), inp=2, out=10),
    ]


def build(out: Path, heavy: bool = False) -> dict:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    made: dict = {}

    t = out / "transcripts"

    # Occupancy bands: below amber, at amber, at red.
    made["green"] = write_jsonl(t / "green.jsonl", occupancy_records("claude-opus-5", 0.20))
    made["amber"] = write_jsonl(t / "amber.jsonl", occupancy_records("claude-opus-5", 0.55))
    made["red"] = write_jsonl(t / "red.jsonl", occupancy_records("claude-opus-5", 0.75))

    # A cold turn: cache_creation exceeds cache_read above the floor. This is
    # the event with a price attached, and it has to be caught well below amber,
    # where the band system was silent. Occupancy here is 12% of a 500k window.
    made["cold_cache"] = write_jsonl(
        t / "cold-cache.jsonl",
        [
            assistant("claude-opus-5", cc=1000, cr=0),
            assistant("claude-opus-5", cc=45_000, cr=15_000, inp=2, out=10),
        ],
    )

    # A warm turn at the same size: creation is far below read, so the cold
    # signal must stay silent. Without this pair the gate would pass on a hook
    # that shouted on every turn.
    made["warm_cache"] = write_jsonl(
        t / "warm-cache.jsonl",
        [
            assistant("claude-opus-5", cc=1000, cr=0),
            assistant("claude-opus-5", cc=1_371, cr=58_629, inp=2, out=10),
        ],
    )

    # An occupancy the window cannot hold. The monitor must report a bug in
    # itself rather than escalate to Red on a number it cannot justify.
    made["impossible"] = write_jsonl(
        t / "impossible-occupancy.jsonl", occupancy_records("claude-opus-5", 1.15)
    )

    # H2 has to be able to come out three different ways, or the verdict is a
    # constant dressed as a measurement. Each case uses a model of its own,
    # because H2 is decided per model and a shared model pools the cases.
    #
    # Prefix-driven: turn-1 creation is enormous and cache_read barely moves,
    # so the static prefix is the cost and H2 is REFUTED for this model.
    made["h2_prefix_driven"] = write_jsonl(
        t / "h2-prefix-driven.jsonl",
        [
            assistant("claude-sonnet-5", cc=200_000, cr=1_000),
            assistant("claude-sonnet-5", cc=0, cr=1_500),
        ],
    )

    # Split: one session of each kind under one model, so the share is exactly
    # 1 of 2 and H2 must say INCONCLUSIVE rather than pick a side.
    made["h2_split_prefix"] = write_jsonl(
        t / "h2-split-prefix.jsonl",
        [
            assistant("claude-haiku-4-5-20251001", cc=180_000, cr=900),
            assistant("claude-haiku-4-5-20251001", cc=0, cr=1_200),
        ],
    )
    made["h2_split_growth"] = write_jsonl(
        t / "h2-split-growth.jsonl",
        [
            assistant("claude-haiku-4-5-20251001", cc=1_000, cr=0),
            assistant("claude-haiku-4-5-20251001", cc=0, cr=150_000),
        ],
    )

    # Attribution: the last hook_success and the injection name different hooks,
    # so charging the bytes to the most recent success is visibly wrong.
    made["injector_attribution"] = write_jsonl(
        t / "injector-attribution.jsonl",
        [
            assistant("claude-opus-5", cc=1_000, cr=0),
            hook_success("SessionStart", "SessionStart", 40),
            injected_by("SessionStart", "x" * 9_500),
            hook_success("PostToolUse:Edit", "PostToolUse", 25),
            assistant("claude-opus-5", cc=0, cr=60_000),
        ],
    )

    # Compaction: one automatic and one manual. Only the automatic one says
    # where the built-in compactor actually fires; the manual one says when a
    # person asked, which is not a ceiling.
    made["compaction"] = write_jsonl(
        t / "compaction.jsonl",
        [
            assistant("claude-opus-5", cc=1_000, cr=0),
            compaction("manual", 120_000, 30_000, 90_000),
            assistant("claude-opus-5", cc=0, cr=200_000),
            compaction("auto", 471_234, 88_000, 383_234),
            assistant("claude-opus-5", cc=0, cr=250_000),
        ],
    )

    # Unknown model: must produce exactly one line and nothing else.
    made["unknown_model"] = write_jsonl(
        t / "unknown-model.jsonl", occupancy_records("claude-experimental-9", 0.80)
    )

    # Two models in one transcript: the report must never pool them.
    made["two_model"] = write_jsonl(
        t / "two-model.jsonl",
        [
            assistant("claude-opus-5", cc=50_000, cr=0, out=100),
            assistant("claude-opus-5", cc=0, cr=60_000, out=100),
            assistant("claude-fable-5-1", cc=70_000, cr=0, out=100),
            assistant("claude-fable-5-1", cc=0, cr=90_000, out=100),
        ],
    )

    # A live file whose last line is half written.
    made["truncated"] = write_jsonl(
        t / "truncated.jsonl",
        [assistant("claude-opus-5", cc=1000), assistant("claude-opus-5", cr=2000)],
        truncate_last=True,
    )

    # Not JSON at all.
    corrupt = t / "corrupt.jsonl"
    corrupt.write_text("this is not json\n{also not\n", encoding="utf-8")
    made["corrupt"] = corrupt

    # Instruction reads, for H1.
    made["instruction_reads"] = write_jsonl(
        t / "instruction-reads.jsonl",
        [
            assistant("claude-opus-5", cc=1000,
                      tool={"name": "Read", "id": "a", "input": {"file_path": "/x/CLAUDE.md"}}),
            assistant("claude-opus-5", cr=1000,
                      tool={"name": "Read", "id": "b", "input": {"file_path": "/x/src/main.ts"}}),
            assistant("claude-opus-5", cr=1100,
                      tool={"name": "Read", "id": "c",
                            "input": {"file_path": "/h/.claude/skills/s/SKILL.md"}}),
            assistant("claude-opus-5", cr=1200,
                      tool={"name": "Read", "id": "d", "input": {"file_path": "/x/src/util.ts"}}),
        ],
    )

    # Hook latency and injected bytes, above and below threshold.
    made["hooks_over"] = write_jsonl(
        t / "hooks-over.jsonl",
        [
            assistant("claude-opus-5", cc=1000),
            hook_success("SessionStart:startup", "SessionStart", 9000),
            injected("x" * 9000),
            assistant("claude-opus-5", cr=2000),
        ],
    )
    made["hooks_under"] = write_jsonl(
        t / "hooks-under.jsonl",
        [
            assistant("claude-opus-5", cc=1000),
            hook_success("SessionStart:startup", "SessionStart", 40),
            injected("x" * 50),
            assistant("claude-opus-5", cr=2000),
        ],
    )

    # Static prefix above and below its threshold.
    made["prefix_over"] = write_jsonl(
        t / "prefix-over.jsonl", [assistant("claude-opus-5", cc=120_000), assistant("claude-opus-5", cr=5000)]
    )
    made["prefix_under"] = write_jsonl(
        t / "prefix-under.jsonl", [assistant("claude-opus-5", cc=5_000), assistant("claude-opus-5", cr=5000)]
    )

    # Repeat reads of one unchanged path.
    made["repeat_reads"] = write_jsonl(
        t / "repeat-reads.jsonl",
        [
            assistant("claude-opus-5", cc=1000,
                      tool={"name": "Read", "id": "r%d" % i, "input": {"file_path": "/x/same.ts"}})
            for i in range(4)
        ],
    )

    # The Lean instruction layer for the A/B harness: the fixture's own CLAUDE.md
    # after the audit's cuts. measure.py copies this over the workspace's
    # CLAUDE.md for every Lean run. It is a tracked content fixture rather than
    # something derived, so the generator has to own the text or a rebuild
    # silently turns Lean into a second Fat. measure.py does note that when the
    # file is missing, so the failure is visible in the ledger, but a whole
    # config of the experiment is wasted by then.
    made["lean_instructions"] = write_text(out / "lean-CLAUDE.md", LEAN_CLAUDE_MD)

    # Settings with a duplicate registration and an inert hookify rule.
    settings = out / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "settings-duplicate.json").write_text(
        json.dumps(
            {
                "autoCompactWindow": WINDOW,
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "bash /h/.claude/hooks/x.sh",
                                 "timeout": 15},
                                {"type": "command", "command": "bash $HOME/.claude/hooks/x.sh",
                                 "timeout": 15},
                            ],
                        }
                    ]
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (settings / "hookify.inert.local.md").write_text(
        "# a rule that only loads when cwd is the home directory\n", encoding="utf-8"
    )
    made["duplicate_settings"] = settings / "settings-duplicate.json"

    # The settings every hook gate pins itself to. Without it the occupancy
    # denominator came from whatever autoCompactWindow the developer happened to
    # have set, so the same gate measured a different window on every machine
    # and the unknown-model path could not be reached at all.
    made["pinned_settings"] = write_json(
        settings / "settings-pinned.json", {"autoCompactWindow": WINDOW, "hooks": {}})

    # Oversize stores: the signal must report and delete nothing.
    stores = out / "stores"
    for name in ("backups", "mlruns"):
        d = stores / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob.bin").write_bytes(b"0" * (2 * 1024 * 1024))
    made["stores"] = stores

    # Instruction files for the apply and revert round trip.
    inst = out / "instructions"
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "CLAUDE.md").write_text(
        "# Project\n\n"
        "## Build\n\nRun `npm run build` to build. Run `npm test` for the suite.\n\n"
        "## Style\n\nAlways be thorough and do your best work at all times.\n\n"
        "## Legacy\n\nWhen using the old model, repeat the task back before starting.\n\n"
        "## Duplicate\n\nRun `npm test` for the suite.\n",
        encoding="utf-8",
    )
    made["instructions"] = inst / "CLAUDE.md"

    # The file that made two section defects visible at once. A fenced block
    # whose lines start with a hash is not a set of headings, and a heading is
    # not a unique key: three `### Notes` live here, two of them under the same
    # parent, so a plan that names one by text alone cannot say which.
    made["tricky_instructions"] = write_text(
        inst / "tricky-CLAUDE.md",
        "# Project\n\nTop matter.\n\n"
        "## Testing\n\nHow the suite runs.\n\n"
        "### Notes\n\nFirst notes, under Testing.\n\n"
        "### Notes\n\nSecond notes, under Testing too, so the crumb matches as well.\n\n"
        "## Setup\n\nRun the two commands below.\n\n"
        "```bash\n"
        "# install deps\n"
        "npm ci\n"
        "# build it\n"
        "npm run build\n"
        "```\n\n"
        "### Notes\n\nA third Notes, under Setup, sharing the heading text but not the crumb.\n\n"
        "## Deploying\n\nThe one heading in this file that is unambiguous.\n",
    )

    # A store big enough to bound the Stop hook's timing.
    if heavy:
        big = out / "big-store"
        big.mkdir(parents=True, exist_ok=True)
        for i in range(2000):
            (big / ("s%04d.jsonl" % i)).write_text(
                json.dumps(assistant("claude-opus-5", cc=100)) + "\n", encoding="utf-8"
            )
        made["big_store"] = big

    # Relative to the fixtures directory, never absolute. This file is tracked
    # in a public repository, and an absolute path here published the machine's
    # home directory layout and account name for no benefit: every consumer
    # resolves these against `out` anyway.
    def relative(v):
        try:
            return str(Path(v).resolve().relative_to(out.resolve()))
        except ValueError:
            return str(v)

    manifest = {k: relative(v) for k, v in made.items()}
    (out / "INDEX.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="context-diet fixtures")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "fixtures"))
    ap.add_argument("--heavy", action="store_true", help="also build the 2,000-file store")
    args = ap.parse_args()
    made = build(Path(args.out), heavy=args.heavy)
    print("built %d fixtures in %s" % (len(made), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
