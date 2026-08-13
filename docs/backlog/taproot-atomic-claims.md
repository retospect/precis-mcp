---
status: draft
title: Taproot atomic claims — migrate existing compound hubs (quiet-window op)
model: opus
---

# Taproot atomic claims — migration of existing hubs

The decomposition machinery shipped (2026-08-13): `extract_claim` returns a
`ClaimExtraction` (atoms + optional compound + not_claims), `conjunct-of`
relation (migration 0126) through the `link_claims` write door,
`hub.apply_extraction` orchestrator, compound hubs hold no direct evidence,
compound trust = worst-of its atoms, workers exclude compounds from
refine/re-embed, backfill runs the cascade per atom + compound. Present-state
truth: `src/precis/taproot/__init__.py` docstring. **What remains is the
migration of existing hubs**, run as a quiet-window operation.

## Migration of existing hubs

Fold decomposition into the human review pass already planned for the
outstanding claim hubs — each hub is being touched anyway, and that is the
natural moment to approve a split and re-point its existing edges at the
right atoms. Re-point **atomically per hub** (no dual-write: a mixed-grain
state would be misread by the trust rollup).

Target rather than grinding in id order: compound claims come
overwhelmingly from intro / abstract / conclusion summary sentences, while
results-section claims are usually already atomic. Chunk section structure is
available, so rank the backlog by likely compoundness.

Cost: expect roughly 5× hub count. More embeddings and more refine
candidates, but per-atom verification is cheaper and more accurate per
call, so total spend is unlikely to scale 5×.

Note: hub-count estimates have disagreed (~112 in this doc's earlier draft
vs ~1.2k in code comments) — reconcile against prod before sizing the
window.

Pre-req for the human pass: fisheye can't yet show atom↔compound structure —
`fisheye-conjunct-of-surfacing.md`.

## Open questions

1. Migration strategy itself (batching, quiet-window scheduling, rollback
   posture) — to be designed next.
2. Whether `chase.py::_taproot_bridge` (deliberately non-decomposing, see
   its docstring) should decompose post-migration, or chase-minted hubs
   just queue for the same review pass.
