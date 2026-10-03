# Changelog

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
