---
status: draft
title: nanobud nomenclature paper — systematic classification + generator + open library
pillar: 3d-design
---

# nanobud nomenclature paper

Methods/data paper: the systematic nanobud classification
(hexfold's `.hx` notation is the pasteable, deterministic name;
hexfold nanobud menus in `src/hexfold/menus.py` are the Euler-validated
junction menu), the graph-surgery generator as its executable definition, and
the library entries as the seed registry. Separate deliverable from
the dr173020 review (which gets only the gap note).

- **The claim**: no systematic nomenclature or structure registry for
  nanobuds exists (`perplexity-research:339983`, 2026-09-14 — confirmed
  explicitly; existing names are per-paper: NanoBuds, "Cases A–D",
  PGNB, Phos–C_n). Verify against primaries before submission; the
  survey's sources are mixed quality.
- **Selling points**: executable nomenclature (a name regenerates the
  structure deterministically); combinatorial validity (Euler/valence
  asserted before geometry); graph-tier property annotations
  (sublattice-parity → magnetic class, rigidity/floppy-mode count,
  chirality) with the cited-vs-computed provenance split; the
  **assembler framing** (`precis_se/modes.py`) — a library entry
  is a build spec for atomically precise manufacturing, with per-
  junction synthesis-accessibility honestly annotated for today's
  stochastic routes.
- **Posture**: defensive publication per the standing FTO rule
  (gr334824) — publish the scheme + library openly; no patent search.
- Figures render via `se-view-figures.md` (the paper eats its own
  dogfood). Candidate venue/format decision + whether the library
  ships as a nanopub series or a dataset DOI: open, decide once the
  generator exists.
