---
status: draft
title: Design workbench — realize-in-the-loop (slice 4 of the design-workbench build)
prio: normal
model: opus
blocked-by: se-atomic-round2
---

# Design workbench — realize-in-the-loop

Slice 4 of the design-workbench build (2026-09-18, Reto + agent, shared
with Malay). Slices 1–3 shipped the same day: the `Design` tab
(`precis_web/routes/design.py`), the revision record
(`design_revisions` + `design_checkpoints` snapshots,
`precis/design/history.py`), the `?rev=N` scrubbers, and the tool-less
chat turn (`precis_web/design_turn.py`; policy in the `precis_web` and
`precis_se` package docstrings). Decided and not up for debate: a human
drives; feedback fast-ish (inline `route()`); single user; relax
defaults cheap.

## Motivation / why

The chat can argue at L0–L2 and propose atom ops, but "realize this box
with a building block" still runs by hand: `se_propose_atomic` returns a
candidate, and turning it into a bound `structure` is the unshipped
Apply in `se-atomic-round2.md` item 1.

## In scope

- From a box in the chat: "realize `bud` with fullerene" → run the
  `se_propose_atomic` body inline (same tool-less contract as a workbench
  turn), show the candidate with its port→atom map in the 3D view; human
  Applies → Apply (`se-atomic-round2.md` item 1) mints the structure and
  `bind_structure`s it, relaxes on `geo`/`emt` in-process, and feeds
  `envelope_fit` + interface fit into the next turn's digest.
- "Relax on GPU (`ml`)" is an explicit button dispatching a
  `struct_relax` job; never the default.
- Library order: `precis_se/atomic/generators/__init__.py::GENERATORS`
  first, then `structure` designs tagged `building-block`, free
  generation as fallback.

## Explicitly NOT in scope

- A first-class building-block kind (ports + relaxed reference geometry
  as schema) — tags until one real design has gone through this loop.
- Auto-applying a realization; the human gate stays.
- Streaming turns; multi-user.

## Acceptance criteria

- "realize `bud` with fullerene" on a fixture se design binds a
  structure, records a `struct_runs` row at rung `emt`, and the next
  turn's digest contains the `envelope_fit` numbers; no `struct_relax`
  job exists unless the GPU button was posted.
- `scripts/test` green on the design-chat/turn/scrubber suites; `mypy
  src tests` clean.

## Target + blast radius

`precis_web/design_turn.py` (realize intent + digest fit numbers),
`precis_web/design_chat.py` + `_design_chat.html.j2` (candidate render +
GPU button), `precis_se/atomic/propose.py`/`apply.py` (Apply, via
`se-atomic-round2`), `precis_web/__init__.py` docstring.

## Open questions / decisions log

- Library as a kind vs a tag: tag (decided 2026-09-18, revisit after
  first real use).
- How the chat recognises a realize intent: an explicit `realize
  <block> with <fragment>` op in the reply (preferred — keeps the
  ops-only contract) vs a free-text classifier. Decide at `ready`.
