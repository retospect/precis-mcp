---
status: ready
title: se `join` is reported as an unknown op in every web-workbench proposal
prio: normal
model: sonnet
---

# `join` dry-runs as "unknown op" in the design workbench

## Motivation / why

`precis_web/design_turn.py::dry_run_se` branches per handler-level op and
falls through to `precis_se.ops.apply_ops` for everything else. It has
branches for `bind_structure`, `unbind_structure`, `generate`, `realize` and
(as of se-nucleic-acid slice 2) `relax_chain` — but **not for `join`**, which
is in `precis_se.atomic.apply.HANDLER_LEVEL_OPS` and therefore absent from
`precis_se.ops.known_ops()`. So a model reply containing `join` reaches
`se_apply_ops`, which reports it as an unknown op, and the turn comes back as
a `dry_run` error instead of a proposal: `join` is unreachable from the
workbench, in a UI whose whole point is proposing ops.

Found while adding the `relax_chain` skip in the same function
(`docs/backlog/se-nucleic-acid.md` slice 2 pass B2). Not that item's op, so
it was left alone rather than fixed opportunistically. The `precis` MCP was
disconnected in that session, so this file is the finding's only record —
there is no gripe.

## In scope

- A `join` branch in `dry_run_se`. `join`'s own store-aware half is
  `precis_se/atomic/join.py::prepare_join(store, tree, op, design_slug)`,
  which is the read-only/in-memory half — the same split `generate` and
  `realize` already use here, so the branch is one line plus the reason.
- A test that a proposal containing `join` dry-runs clean (the bug is that it
  does not), alongside the existing `dry_run_se` cases.
- **Check the other direction while there**: the roster this function
  branches on is maintained by hand against `HANDLER_LEVEL_OPS`, and it has
  now drifted at least once. Either derive the skip/prepare mapping from one
  table, or add a test that every name in `HANDLER_LEVEL_OPS` is either
  branched on or deliberately listed as pure-dry-runnable — a missing branch
  must fail the suite, not surface as an unknown op to a user.

## Explicitly NOT in scope

- Changing what `join` does, or its Apply-side behaviour. This is only about
  the dry run that decides whether the user is shown a proposal at all.

## Acceptance criteria

- A turn whose ops include `join` returns a proposal, not a `dry_run` error.
- Adding a name to `HANDLER_LEVEL_OPS` without touching `dry_run_se` fails a
  test that names the missing op.

## Target + blast radius

`src/precis_web/design_turn.py` (`dry_run_se`), `tests/precis_web/` (the
design-turn tests). No migration, no deploy step.
