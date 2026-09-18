# Harness tasks

Five tasks for the A/B measurement harness. Each one is stated as the
instruction to hand to the agent, followed by the exact shell command the
harness runs to decide pass or fail.

Every pass command is run from the fixture root with `bash`, and every one ends
in `echo "exit=$?"`. **Pass is `exit=0`.**

Between tasks, reset the working tree to the branch tip: restore tracked files
and remove untracked ones, including `answer.txt`, which Task 3 creates and
leaves untracked on purpose. Do **not** remove ignored files as part of that
reset: `node_modules/` is ignored and is the only dependency store, so wiping
it makes the repo unrunnable offline.

Tasks 1, 3, 4 and 5 start on `main`. Task 2 starts on `broken-test`.

---

## Task 1: add a field to the order model

**Branch:** `main`

> Add a required `currency` field of type `string` to the `Order` interface in
> `src/models/order.ts`, thread it through `createOrder` as a parameter, update
> every existing call site, and add a test that covers it. The whole suite must
> stay green and the typecheck must stay clean.

**Pass:** the field exists in the model, at least one test references it, the
typecheck is clean and the suite is green. The typecheck is part of the
condition because Vitest does not typecheck, so a required field added without
updating the call sites would otherwise pass silently.

```bash
grep -q "currency" src/models/order.ts \
  && grep -rq "currency" tests \
  && npm run typecheck \
  && npm test; echo "exit=$?"
```

---

## Task 2: fix the deliberately broken test

**Branch:** `broken-test`

> One test in this repo fails. Find the cause and fix it. The bug is in the
> source, not in the test, so do not touch anything under `tests/`. The
> assertion is correct as written.

**Pass:** the suite is green **and** `tests/` is byte-identical to `main`. The
diff check is what enforces "without editing the assertion", and it also
catches deleting the test or marking it `.skip`.

```bash
git diff --quiet main -- tests/ && npm test; echo "exit=$?"
```

---

## Task 3: explain what `src/services/pricing.ts` does

**Branch:** `main`

> Read `src/services/pricing.ts` and write a short explanation of what the
> module does into a file called `answer.txt` at the repo root. Name the
> functions it exports and say what each one is for.

**Pass:** `answer.txt` names at least three functions that the file actually
exports. The expected names are derived from the file at check time rather
than hardcoded, so the check survives Task 4's rename.

```bash
ANSWER=answer.txt; [ "$(grep -oE 'export function [A-Za-z0-9_]+' \
  src/services/pricing.ts | awk '{print $3}' \
  | while read -r f; do grep -q "$f" "$ANSWER" && echo "$f"; done \
  | wc -l | tr -d ' ')" -ge 3 ]; echo "exit=$?"
```

---

## Task 4: rename a symbol across four files

**Branch:** `main`

> Rename the exported function `makeIdKey` to `buildIdKey`. It is defined in
> `src/util/idKey.ts` and imported in three other modules, so four files change
> in total. Leave no reference to the old name behind and keep the typecheck
> clean.

**Pass:** no occurrence of `makeIdKey` remains under `src/` or `tests/`, at
least four files under `src/` or `tests/` mention `buildIdKey`, and the
typecheck is clean. The bound is "at least" rather than "exactly" so that an
agent who also adds a test for the renamed helper is not marked down; deleting
or inlining the helper instead of renaming it still yields a count of 0 or 1
and fails. The greps are scoped to `src tests` so that the prose in this file
and in `CLAUDE.md` cannot affect the count.

```bash
! grep -rq "makeIdKey" src tests \
  && [ "$(grep -rl "buildIdKey" src tests | wc -l | tr -d ' ')" -ge 4 ] \
  && npm run typecheck; echo "exit=$?"
```

---

## Task 5: add an npm script and run it once

**Branch:** `main`

> Add an npm script named `smoke` to `package.json` that runs the service
> entrypoint once, then run it. It must print the demo quote and exit 0.

**Pass:** a `smoke` script exists and running it exits 0. `npm pkg get` prints
`{}` for a missing key, which is what the first half tests.

```bash
[ "$(npm pkg get scripts.smoke)" != "{}" ] && npm run smoke; echo "exit=$?"
```
