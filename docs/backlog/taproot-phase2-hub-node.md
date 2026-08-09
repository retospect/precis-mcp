---
status: draft
title: Taproot Phase 2 slice 2d — citation-card dedup (stop double-counting vs the hub card in ANN)
model: opus
---

# Taproot Phase 2 — slice 2d, citation-card dedup

Context: Phase 2 promoted `finding` to the claim hub node. Slices 2a
(TAPROOT classifier axis, `data/axes/taproot.yaml`), 2b (evidence vocab
migration `0094` + the single write door `src/precis/taproot/hub.py`), 2c
(`view='evidence'` + `seniority.py::derive_evidence`), and 2e
(cite→originators export, `src/precis/cli/resolve.py`) are SHIPPED —
present-state in the `src/precis/taproot/` package docstring; full Phase-2
decomposition + locked decisions in git history of
`docs/backlog/taproot-phase2-hub-node.md`. The one unshipped slice:

**2d — citation-card dedup.** Stop double-counting citation cards vs the
hub's `card_combined` in ANN retrieval (taproot.md open #3 residual): when a
claim hub exists, the `citation`-kind cards covering the same claim/passage
compete with the hub card in embedding search, inflating the same claim into
multiple hits. Depends on 2b (shipped), so it is unblocked.

Related unbuilt Phase-3+ items tracked elsewhere / later: the S2
global-citation-count originator fallback (seniority), the integrity axis
(Phase 4), the corpus-wide backfill sweep (`taproot-hub-refine.md` owns the
reconcile worker).

## Acceptance

- A claim covered by both a hub card and citation card(s) surfaces once in
  ANN-backed search cohorts (no duplicate hit for the same claim), with the
  hub card winning.
- No body chunks mutated (`ord >= 0` append-only); only `ord < 0` card
  variants are DELETE/re-INSERTed by a registered synthesis pass.
