---
status: draft
title: scripts/ship --impacted can skip the migration-numbering guards, letting cross-worktree number collisions land on main
model: sonnet
---

# `scripts/ship --impacted` can skip the migration-numbering guards

## Motivation / why
On 2026-08-10 two worktrees each grabbed migration number `0118`
(`0118_drop_dead_indexes` and `0118_resource_slot_holds`). Each shipped
green; the collision only appeared in the *post-merge* combination and
first reddened an unrelated worktree's gate a day later (this session
caught it, then renumbered `resource_slot_holds` → `0119` to unblock main).

The guards that exist for exactly this — `test_migration_numbering.py`
and `test_schema_baseline.py::test_migration_number_prefixes_unique` —
did **not** fire on the introducing ship. Likely root cause (to confirm):
`/land` runs `scripts/ship --impacted`, which narrows pytest to
`testmon`'s affected set. `testmon` maps tests to code by **import /
coverage**, but these guards discover migrations by **globbing the
`src/precis/migrations/*.sql` directory at runtime** — they import none
of them. So adding a new `.sql` file does not mark the guard tests as
impacted, and the `--impacted` gate skips them. The full suite (`/go`,
or a fresh worktree's first `--impacted` run) still catches it — which is
why it surfaced only on a later sync.

Note this is orthogonal to the pre-merge-vs-post-merge race: even if the
gate ran against the correctly-merged tree, `--impacted` would still
deselect the guard. The two guards are cheap, pure-Python, no-DB — there
is no cost reason to ever skip them.

## In scope
Guarantee the migration-numbering guards always run, even under
`--impacted`. Options (pick one in the spec):
- Make `scripts/ship`/`scripts/test` always append the guard node ids
  (or `-m` an always-run marker) on top of the testmon selection.
- Register the `migrations/` dir as an explicit testmon dependency of the
  guard tests (a `testmon`-visible read, or a conftest hook).
- A tiny always-run "structural guards" lane in the ship gate, separate
  from the testmon-selected pytest run.

## Explicitly NOT in scope
- The pre-merge/CAS race in `scripts/ship` (a separate concern; the guard
  running reliably is enough to catch the collision on the *next* sync).
- Auto-renumbering or any change to sealed migrations.
- Broadening what `--impacted` selects in general — this is scoped to the
  structural guard tests only.

## Acceptance criteria
- Introducing a duplicate migration number and running
  `scripts/ship --impacted` (with a warm testmon map) fails the gate on
  the numbering guard — demonstrably, via a repro.
- The two guard tests appear in the selected set regardless of testmon
  state.
- `/go` (full suite) behaviour unchanged.

## Target + blast radius
`scripts/ship`, `scripts/test`, possibly `tests/conftest.py` /
`tests/test_migration_numbering.py` / `tests/test_schema_baseline.py`,
testmon config. No product-runtime code.

## Open questions / decisions log
- Confirm the root cause empirically: warm the testmon map, add a dup
  `.sql`, run `scripts/test --impacted`, check whether the guards are
  selected. (Strongly expected to be deselected.)
- Are there *other* glob/data-driven guards with the same testmon
  blind spot (schema-drift, baseline-checksum)? If so, the "always-run
  structural lane" option covers them all at once.
