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

