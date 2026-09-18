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

    # A store big enough to bound the Stop hook's timing.
    if heavy:
        big = out / "big-store"
        big.mkdir(parents=True, exist_ok=True)
        for i in range(2000):
            (big / ("s%04d.jsonl" % i)).write_text(
                json.dumps(assistant("claude-opus-5", cc=100)) + "\n", encoding="utf-8"
            )
        made["big_store"] = big

    manifest = {k: str(v) for k, v in made.items()}
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
