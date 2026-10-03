# Changelog

## 0.3.0 — 4 October 2026

Every open defect from the review closed, and the 115% reading explained.

**Hook cost names the hook and the model.** The census keyed hook time by event
(`PreToolUse:Bash`), so every hook on an event pooled into one figure, and it
copied a session's total onto each model row, so a two-model session counted it
twice. Time is now keyed by the hook's command and charged to the model whose
turn it fell in. Injected bytes stay keyed by event, because the record names
nothing else.

**The hook audit sees what actually loads.** It walked every `hooks.json` in the
marketplace catalogue (10 files for 4 installed plugins) and compared whole
command strings. It now reads only installed, enabled plugins plus skill folders
that carry hooks, says what it left out, and treats two registrations of one
script as a duplicate whatever interpreter runs them. On this machine that found
the design checker's Stop hook running twice per reply.

**A resumed session is told where it stopped.** The handoff's `next_action` was
always empty. It is now lifted word for word from the task in progress, or the
last request, within 25 words. Files read with `cat`, `sed` or `grep` are listed
too, so the read list went from 1 to 61 on the session that found it.

**The 115% reading has two causes, both measured.** A turn that consults the
advisor reports its two passes summed, about double; the monitor already reads
the last pass. And a session keeps the compaction trigger it started with, so one
begun before the setting was lowered compacted at 660,956 and is really past 100%
of today's window. The notice now says the window is not that session's instead
of calling it a bug.

**Window order is the spec's.** `autoCompactWindow` is used first whenever it is
set, by Ben's decision; a smaller per-model window is named in the reading.

**Smaller fixes.** The H2 cutoff is the config key `h2_growth_share`. The MLflow
bootstrap no longer rewrites a portable `.mcp.json` with an absolute home path.
The per-event hook timing gate takes the fastest of three runs, so it measures
the hook rather than a busy machine.

## 0.2.0 — 21 September 2026

The release that stopped the plugin making things worse, in two places.

**The monitor no longer asks for a compaction.** Crossing 50% used to tell the
model to run `/compact`. Measured across 413 transcripts on this machine, an
ordinary turn above 20,000 prefix tokens reads a median 284,209 tokens from
cache and writes 1,371; the turn right after a compaction writes 90,337 and
reads 34,068, and creation exceeds read on 86% of post-compaction turns against
4.5% of ordinary ones. A compaction moves the conversation from the read price
to the write price, so asking for extra ones spent more. Fullness is now written
down and never acted on.

**It watches the cache instead**, which is the quantity with a price attached. A
turn that re-creates the prefix rather than reading it is reported once, with
your place saved first. This fires well below 50%, where the old band system was
silent.

**An occupancy above 100% pauses instead of escalating.** A reading the monitor
cannot justify is a bug in the monitor, so it says so rather than arming a
rollover on it.

**The A/B report refuses a verdict it cannot support.** A run killed by a usage
limit used to be recorded as a failed task, so 62 runs that never executed read
as 62 lost tasks and the report was ready to state a confident headline about
the cost of deleting an instruction file, from a config that never ran once. Runs
now carry whether they executed, every median and pass count comes from the ones
that did, and a config with no valid runs gets a refusal naming it.

**New: `scripts/inventory_audit.py`.** Prices the plugin inventory every message
carries and sets it beside how often each plugin was actually used. On this
machine: 52,799 tokens per message across 196 loaded plugins, 15,305 of it from
138 plugins with no recorded use. It proposes and writes nothing; disabling is a
reversible settings change you apply.

**New: `install.sh`.** Re-runnable. Backs up before touching anything, never
overwrites your settings or your data, and refuses to claim success unless the
gates pass.

50 gates, up from 33.

## 0.1.0 — 19 September 2026

First build. Census over 413 transcripts with a verdict per hypothesis per
model, occupancy monitor with the handoff contract, the audit and apply path
with byte-identical revert, and the A/B harness.
