---
status: draft
title: an se test fixture that runs ops through prepare AND finish, so phase bugs fail a test
pillar: 3d-design
prio: normal
---

# An se test fixture that runs ops through prepare and finish

## Motivation / why

`se` ops are built as a prepare/finish pair: `prepare_*` is the pure,
in-memory half that validates and computes, `finish_*` is the half that
touches the store, deferred so a partial failure leaves no orphan. The
split is deliberate and documented in the op modules' own docstrings.

Tests do not exercise it. They construct a store and call the component
directly, so anything that depends on *which phase* a call runs in is
invisible to them.

gr457996 is the demonstration. Slice 2's catalogue store shipped with ten
tests and passed as part of an 80-test green run: SQL, JSONB round-trip,
the measured-row gate, first-wins semantics, `resolve_edge` integration.
All of it correct, all of it calling `DbCatalogueStore(store)` directly.
The actual wiring put a write in `prepare`, where it never commits, so the
feature is a no-op in production — and no test could have noticed, because
no test crossed the boundary the bug lives on. A two-call prod dogfood
found it immediately.

The lesson is not "write more tests". It is that a component-level fixture
cannot see a phase-level defect, and `se` has phases.

## In scope

- A fixture that submits a real op batch — the same shape a caller sends —
  through the handler's full prepare-then-finish path against the test DB,
  and returns both the echo and the resulting tree.
- Coverage of the two things component tests structurally cannot assert:
  that a write issued by an op actually commits, and that a failing op in
  a batch leaves no partial state.
- A test written on the fixture that fails against gr457996's current
  wiring and passes after the fix — the fixture's first customer.

## Explicitly NOT in scope

- Replacing the component-level tests. They are the right tool for the
  store's own semantics and stay.
- A fixture for every op. Start with `generate` and `join`, the two that
  mint structures and therefore have the most to lose across the seam.
- Any production code change; this is test infrastructure. The gr457996
  fix is its own item.

## Acceptance criteria

- A test can assert "after this op batch, table X contains row Y" and that
  assertion fails when the write is issued from the prepare half.
- A test can assert that a batch whose second op raises leaves the design
  exactly as it was before the first — no orphaned structure refs.
- The fixture is usable from a plain `tests/test_se_*.py` with the
  existing `store` fixture, requiring no new container or service.

## Target + blast radius

`tests/conftest.py` (or an `se`-local conftest) and the `se` handler's op
entry point, used read-only. No production code, no migration.

## Open questions / decisions log

- Does this need the MCP verb layer, or is the handler's op entry point
  enough? Handler level is cheaper and covers the prepare/finish seam,
  which is the defect class; the verb layer would also cover schema and
  echo rendering. Leaning handler level for slice 1.
- `generate` + `join` cannot share one op batch today (the join cannot see
  the binding). Worth pinning that behaviour in this fixture so it is a
  recorded constraint rather than a surprise.
