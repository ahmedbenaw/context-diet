# Review board, round 1 — 19 Sept 2026

Five lenses, each a reviewer dispatched as its own agent against the real tree:
architecture, harness, correctness, security, docs-truth. Every finding below is
anchored to a file and line and was tagged CONFIRMED by its reviewer, most with a
reproduction. Three findings were reported independently by two lenses, which is
recorded rather than merged away.

Status values: `fixed`, `open`, `blocked` (needs Ben), `rejected` (with a reason).

## Blockers

| # | Where | Defect | Status |
|---|---|---|---|
| B1 | patch.py cut_section | A non-heading match computed level 0, so the terminator test could never fire and everything from the match to end of file was deleted, reported as "applied 1 of 1". Reproduced against a settings.json, which was left as a single byte. | fixed |
| B2 | patch.py do_revert | `original` and `backup` were read straight out of INDEX.json and copied with no fence and no containment check, so a crafted index overwrote any absolute path. The checksum afterwards only compared the written file to the blob just written, which assures nothing. | fixed |
| B3 | measure.py prepare_workspace | `shutil.rmtree` ran before the guard, and the guard disabled its own path fence whenever CONTEXT_DIET_WORKSPACE was set, so pointing that variable at any directory deleted it. | fixed |
| B4 | session_start.py | An armed manifest found on disk was injected as additionalContext with no check that this plugin wrote it, so a cloned repo could feed text straight to the model. verify_manifest never inspected `unresolved`, `tasks` or `decisions` at all. | fixed |
| B5 | occupancy_monitor.py speak | On PostToolUse the directive was written as bare stdout while only the UserPromptSubmit branch produced an injectable directive, and the one-shot band marker meant whichever event fired first was the only one that ever spoke. | fixed |
| B6 | occupancy_monitor.py | The acted marker was written unconditionally, so a failed manifest write still emitted "Please run /compact now" and then permanently suppressed retries: a directive to discard transcript detail with no state on disk. | fixed |
| B7 | patch.py do_apply | `read_text` is strict and runs inside the mutation loop, after files are already rewritten and before INDEX.json exists, so one undecodable byte in a later target left the earlier file modified with no recoverable backup set. | fixed |
| B8 | measure.py write-out | The header was taken from the first record's keys while `num_turns` is only added when the agent's JSON parses, so a late unparsed trial raised KeyError after all 45 paid runs with nothing yet written. | fixed |
| B9 | measure.py reset_workspace | `git()` ignored the return code, so a dirty tree aborted `git checkout broken-test` while `git reset --hard broken-test` then moved main itself onto the buggy commit, making the fix-broken-test check trivially true for every later trial. | fixed |

## Correctness and harness

| # | Where | Defect | Status |
|---|---|---|---|
| H1 | handoff.py verify_manifest | Recomputed and overwrote `sha256`/`mtime` instead of comparing, so a changed file was silently re-hashed and presented as verified. Found by two lenses. | fixed |
| H2 | occupancy_monitor.py | `already_acted`/`mark_acted` is a non-atomic check-then-act; four concurrent monitors on one band let three through, each doing a full manifest build. | fixed |
| H3 | cdlib.py last_usage | Read only a fixed 512 KB tail, so on large transcripts the last record carrying usage fell outside the window and the monitor went inert. One of the twenty largest real transcripts returns empty. | fixed |
| H4 | precompact_manifest.py | Skipped whenever any manifest existed, but the amber monitor writes `armed=False`, so a session that went amber and then compacted handed over nothing. | fixed |
| H5 | handoff.py write_manifest | Non-atomic `write_text` inside a hook with a timeout, so a kill mid-write leaves truncated JSON that `load_armed` discards silently. | fixed |
| H6 | read_tracker.py | Read-modify-write of one shared JSON under parallel hooks lost 14 of 50 updates across five trials. | fixed |
| H7 | read_tracker.py | `int(st_mtime)` has one-second resolution, so an edit and re-read inside one second is reported as an unchanged repeat read. The gate hides this by sleeping 1.1 s. | fixed |
| H8 | stop_ledger.py | `ledger_ms` was computed before the MLflow sink ran, so the number used to police hook cost omitted its own largest cost. | fixed |
| H9 | stop_ledger.py dir_size | Stops at 20,000 files and reports the partial total as fact. | fixed |
| H10 | occupancy_monitor.py | The unknown-model pause notice has the same delivery defect as the band directive, and is stamped one-shot before any injectable emission. | fixed |
| C1 | extract.py / budget.py | No fenced-code tracking, so `# install deps` inside a bash block parsed as a heading. Board said patch.py too; patch.py already had `fence_mask`. One parser now lives in cdlib and all three use it. | fixed |
| C2 | extract.py section_id / patch.py cut_section | Ids collided, and fixing them alone was not enough: cut_section matched by heading text and took the first hit. Ids now carry an occurrence ordinal, and an ambiguous heading is refused rather than guessed. | fixed |
| C3 | context_census.py | Injected bytes attributed to a carried-over `last_hook_name` although all 201 sampled attachments carry their own `hookName`. | fixed |
| C4 | context_census.py | The H2 verdict was the hard-coded string "H2 SUPPORTED", so census_completeness passed on a constant. Now a paired per-session test that can return SUPPORTED, REFUTED or INCONCLUSIVE, with a fixture for each. | fixed |
| C5 | measure.py | The runs.tsv writer strips tabs but not newlines, while `note()` embeds multi-line agent output, so a row spans two lines and both are dropped silently. | fixed |
| C6 | handoff.py verify_manifest | Assumes shapes it never checks, so a hand-written or truncated manifest raises AttributeError instead of dropping the field and naming it. | fixed |
| C7 | report.py | A row whose cell count does not match the header is skipped with no counter and no message. | fixed |
| C8 | census / report | `median()` returned the upper middle value, not the median. Board said three copies; there were two implementations, and hook_audit only consumes a value the census computed. Both now import one definition from cdlib. | fixed |
| C9 | cdlib.py state_dir | State is resolved from the shell's cwd, not the project root. Found live: this session's monitor hit amber while the shell had drifted into `tests/fixtures/transcripts`, and wrote the handoff and its acted marker there. A session starting from the repo root will never find that manifest, so the resume path silently misses any handoff written from a subdirectory. | fixed |
| A1 | cdlib.py append_decision | Writes runtime state into the plugin's own distribution directory rather than the project state dir, so every project mutates the installed package and one file has no rotation. | fixed |
| A2 | report.py / mlflow_sink.py | `cfg.get("_project")` is never set by load_config, so the MLflow store always resolved against the process cwd. Found by two lenses. | fixed |
| A3 | measure.py workspace | Derives its path from `Path.cwd()` while the rest of the run uses `--project`, so results land in a directory containing none of the work. | fixed |
| A4 | stop_ledger.py | Imported mlflow inside a hook: about two seconds per session end plus a stderr hint, breaking the stdlib-only contract and the "silent always" promise. | fixed |
| A5 | run_gates.py | Asserts the presence of Ben's personal duplicate hook and his inert rules, so the suite is green here and red on any clean machine. | fixed |
| A6 | run_gates.py | `run_hook` never pins CONTEXT_DIET_SETTINGS, so the occupancy denominator comes from the developer's real settings. | fixed |
| A7 | run_gates.py | Runs hooks with the repo venv while hooks.json invokes bare `python3`, so the suite measures an interpreter production never uses. | fixed |
| A8 | run_gates.py monitor_budget_ms | Times only the green path, never the amber branch that does the work. A real invocation recorded 568 ms against a 50 ms budget. | fixed |
| A9 | run_gates.py storage_signal_cost | Never asserts the cache property; the cached run measured slower than the first and the gate still passed. | fixed |

## Found after the board, by measurement (round 1.5)

None of these was reported by any of the five lenses. All were found by
running the census numerator against all 334 transcripts on this machine. M2 is
recorded as withdrawn rather than deleted, because the wrong reading is the
useful part of the trail.

| # | Where | Defect | Status |
|---|---|---|---|
| M1 | cdlib.occupancy | One assistant turn can hold several inference calls. The record then carries an `iterations` list and the top-level usage fields are a sum across them, so the numerator measured billing, not context. A three-iteration turn whose real prompt was 654,052 tokens read as 1,306,241. Of 34,617 usage records, 519 carry more than one iteration and the worst inflation is exactly 2.00x. Landing on one of those fires Amber at half the real occupancy and spends a compaction the session did not need. | fixed |
| M2 | .claude/context-diet.json and §6 | **Withdrawn — I was wrong.** I claimed the denominator was about 2x too small because real prompts reached 999,231 and 18 transcripts passed 500,000, and concluded `autoCompactWindow` was not the window. The two figures were not the same measurement: those peaks span all of this machine's history, and the setting is five days old. `compactMetadata` records the trigger and `preTokens` directly, and ordered by time the auto-compactions read 1,000,499 · 1,033,177 · 1,316,455 up to 2026-09-11, then 467,778 · 467,425 · 488,138 · 545,876 · 545,201 · 467,794 · 467,744 and, today, 467,295 · 470,467. The setting changed on about 2026-09-14 and is live. Auto-compaction now fires at ~93.5% of 500,000, so Amber at 50% and Red at 70% sit properly below it and `claude-opus-5: 500000` is correct as an operating window. | withdrawn |
| M3 | compactMetadata | Not a defect, a source the plan did not know about. Every compaction record carries `{"trigger": "auto"\|"manual", "preTokens", "postTokens", "cumulativeDroppedTokens", "durationMs"}`. That is a direct measurement of where the built-in compactor fires, which Part 5.3 asks the plugin to sit below and which the census currently infers from nothing. Worth reading in the census rather than reasoning about. | fixed |

| M4 | measure.py prepare_workspace | The same defect the board found in `reset_workspace` (B9), in a second place it did not look. `git init` fails under `.claude/context-diet` because the sandbox will not let it create `.git/hooks`, and with the return codes discarded `git add -A` and `git commit` walked up to the enclosing repository and committed this session's whole working tree there as two commits titled "fixture baseline". Nothing was lost and nothing was pushed; `git reset --mixed d12f639` restored the exact prior state. An exit check alone is not enough, because the fall-through succeeds: after `init` the workspace now has to prove it is its own repository with `rev-parse --show-toplevel`. | fixed |
| M5 | measure.py workspace location | The default A/B workspace under the project's own `.claude/context-diet/ab/` is unusable in this sandbox: `.git/hooks` cannot be created and `.git` cannot be deleted. The harness refuses that location loudly now rather than half-working, and the run uses `CONTEXT_DIET_WORKSPACE` pointed somewhere writable. | fixed |

## Deviations from the v3 spec (need Ben's sign-off)

The plan file is unchanged; these are code decisions that differ from it.

| # | Spec text | What ships | Why |
|---|---|---|---|
| X1 | §6 denominator lookup order: "(1) `autoCompactWindow` from settings when present. (2) `context_windows[model]`." | When both are known, the **smaller** wins. Settings-only and table-only cases are unchanged, and an unknown model with `autoCompactWindow` set still uses it rather than pausing. | `autoCompactWindow` is one global number. At 500,000 it measures a 200,000-token model against 500,000, so Amber lands at 250,000 real tokens, past the point that session still exists, and the monitor never speaks on that model at all. Taking the smaller keeps Amber below the built-in compactor, which is Part 5.3's rule, and below the model's real ceiling. Verified: Haiku now resolves to 200,000, Opus to 500,000. The unknown-model-with-setting case still carries the same never-fires risk for any unknown model smaller than the setting; it is left on the spec's order rather than changed twice, and the mitigation is that every model in the census is in the table. |

## Security

| # | Where | Defect | Status |
|---|---|---|---|
| S1 | handoff.py scan_transcript | Stores every Bash command verbatim, so a token typed into a command lands in a 0644 manifest and is read back into the next session. | fixed |
| S2 | mlflow_sink.py resolve_uri | Passes any non-file, non-sqlite scheme through untouched, so a repo-committed config with an http:// tracking URI sends session metadata off-box. | fixed |
| S3 | handoff.py write_manifest | Nothing writes a .gitignore into the state dir; this repo escapes only because its own top-level .gitignore happens to cover it. | fixed |
| S4 | patch.py append_decision | Logs absolute paths into a file tracked in a public repo, leaking home-directory layout and username. | fixed |

## Docs truth

| # | Where | Defect | Status |
|---|---|---|---|
| D1 | README.md:18 | The Fable 5.1 census row claims 2 and 45 reads; the shipped census output gives 0 and 2. The other three rows reproduce exactly. | fixed |
| D2 | README.md:113 | Claims the suite covers "the report refusing to state a difference smaller than its own spread", but no gate executes report.py at all. | fixed |
| D3 | README.md:159 | Claims the under-200 ms hook cost is checked by the gate suite's timing tests. No gate bounds total per-session hook cost, and the Stop gate permits 20 s. | fixed |
| D4 | README.md:26 | Says "Four slash commands" above a table of six. | fixed |
| D5 | selfheal.py:39 | The closed self-heal table names `hook_latency_budget`, which is not a gate name, so `--only hook_latency_budget` reports 0 passed and exits 0. | fixed |
| D6 | report.py:149 | Compares raw pass counts with no spread guard and prints "the post's prescription held", contradicting its own docstring. | fixed |
| D7 | .claude/context-diet.json:2 and cdlib.py:33 | Both claim the first run prints the effective table. `effective_table()` has zero callers. | fixed |
| D8 | fixture/TASKS.md | The prose said checks must not end in `echo "exit=$?"` while every shown command did. The harness was always right; only the doc was wrong. Now gate-enforced to equal `measure.TASKS`. | fixed |
| D9 | .gitignore:3 | Names a `fixture-seed` path that does not exist, and calls the fixture a git repo of its own when it contains no .git. | fixed |
