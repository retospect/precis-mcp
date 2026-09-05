---
status: draft
title: design-graph relations — the remaining typed edge (realizes)
prio: medium
model: opus
---

# Design-graph relations — `realizes` (the last unminted edge)

Design session 2026-09-04 (Reto + agent); grounding
`perplexity-research:317035` (local-global linkage via typed edges).
Of the four relations the multi-scale mesh hangs on, three are live:
`contains`/`part-of` (mig 0095, cad emission since slice 3),
`analyzed-by`/`analysis-of` (mig 0153, attached-models v1),
`made-by`/`makes` (mig 0154, make-tree v1). Git log has the shipped
decision detail.

## Remaining: `realizes` (+ inverse `realized-by`)

Block → the thing that makes it real (axis 1): a manufacturing-mode
realization, a bought `component` part, a synthesized molecule.
Many-to-one legal (candidate realizations). **Enters only with its first
consumer** — the cad-side catalog→bom write (`cad-machine-spec.md`
§Parallel track, catalog atoms backed by `component`). Pattern:
`0095_component_contains.sql`; check the migration ledger for the next
free number at mint time.

se stays plugin-local (name-keyed rows, no stable link endpoint —
coordinated with the se track 2026-09-05); cad refs are the linkable
blocks. Terminology: se's `demands_relation` is a tolerance relation
between measures, unrelated to the `relations` table.

## Non-goals

No generic graph query language (`view='links'` + search is the
navigation surface); no backfill job (a one-shot re-save sweep is one
ops command when wanted).
