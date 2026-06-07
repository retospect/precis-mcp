# ADR 0023 — `view='dreamable'` ships as a plain ANN ring, no clustering dependency

- **Status**: **accepted** (2026-06) — implemented in
  `Store.dreamable_region` + `PrecisRuntime._dispatch_dreamable`.
  Tracks `docs/design/dreaming.md`, §`view='dreamable'`.
- **Deciders**: Reto + agent

## Context

The dreaming design (`docs/design/dreaming.md`) specified the focus
region — `search(view='dreamable')` — as a **retrieve-then-cluster**
step: ANN-retrieve a salient frontier, then run real clustering
(HDBSCAN / sklearn GMM) on that bounded subset to carve it into several
labelled sub-themes. That clustering library was flagged as landing
"behind an ADR when it's built" and is a new top-level dependency
(per AGENTS.md, a new dep needs an ADR).

In review the question was raised: *the focus region is just the seed's
nearest neighbours by cosine — isn't that already the cluster?* For the
single-region product the design actually wants — "inject the focus
region + a few inspiration sparks, then let the LLM work" — the answer
is yes. Sub-clustering only matters if one call must return *several*
distinct labelled themes for the agent to choose among, which the design
itself lists as deferrable (cluster ids are ephemeral; the cheap
alternative is to let the agent pin a region by a representative member
via `like=`).

## Decision

Ship `view='dreamable'` as **seed-by-salience + a single ANN ring**, with
**no clustering dependency**:

- `select_dream_seed` picks the most-due chunk
  (`argmax(last_seen - last_dreamt)` over target kinds).
- `Store.dreamable_region` returns its `n` nearest embedded chunks
  (card-inclusive, live refs, target kinds) — one cosine neighbourhood.
- `_dispatch_dreamable` stamps `last_dreamt` on every surfaced chunk
  (the rotation) and renders the region. Salience is **not** bumped:
  looking at a region counts as *dreaming* it.

No HDBSCAN / GMM, no `cluster` kind, no new top-level dependency. The
retrieve-then-cluster machinery remains documented as a future option,
gated behind a later ADR, and is only built if multi-theme output in a
single call proves necessary.

## Alternatives considered

- **HDBSCAN/GMM sub-theming now.** Rejected for the current scope: adds
  a heavy top-level dependency and per-call clustering cost for a
  capability the single-region dream loop does not use. The agent can
  already pivot within the frontier via the `angle` spray and `like=`.
- **Persist a `clusters` table with stable ids.** Rejected (and already
  rejected in the design): the durable artifact is the dream + its
  links, not a cluster table.

## Consequences

- **Positive**: `view='dreamable'` ships today with zero new
  dependencies, reusing the salience seed-picker and the angle-spray ANN
  engine; fully in-process testable (`test_dreamable.py`).
- **Negative**: a single call returns one neighbourhood, not several
  labelled sub-themes. If that's needed later it's an additive change
  behind a new ADR — no rework of the shipped path.
- **Neutral**: the design doc's retrieve-then-cluster section stays as a
  deferred-future note rather than being deleted.

## See also

- `docs/design/dreaming.md` (§`view='dreamable'`, §Retrieve-then-cluster)
- [ADR 0010 — Postgres + pgvector system of record](./0010-postgres-pgvector-system-of-record.md)
