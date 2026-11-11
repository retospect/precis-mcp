---
status: draft
title: nanobud nomenclature paper — systematic classification + generator + open library
pillar: 3d-design
---

# nanobud nomenclature paper

*(Transfer to graph memory is pending — spec-in-memory ruling
`me568168`, target carbon-topology spec `me628772` (structure and
nomenclature; the [9-6] nanobud ruling `me628794` already lives
there), gripe `gr629126`. Both 2026-11-11 transfer passes had no
precis MCP; the executing lane needs the session MCP on PROD — the
writes cannot be done from a no-MCP lane. Do not delete this file
until the nodes below are written, `me628772`'s reading order carries
their handles, and the references are repointed — retiring ahead of
the transfer was reverted once already (`a331c87a4`).)*

*Transfer manifest — coverage unchecked (this lane had no MCP; the
executing lane searches the graph first and folds in what already
exists). Write each as one `section:reference` node, `part-of` →
`me628772`, `Source:` line naming this file + transfer date:*

1. *Title ≈ "No systematic nanobud nomenclature or registry exists".
   Body: the paper's premise — `perplexity-research:339983`
   (2026-09-14) confirmed explicitly; existing names are per-paper
   (NanoBuds, "Cases A–D", PGNB, Phos–C_n); verify against primaries
   before submission — the survey's sources are mixed quality.*
2. *Title ≈ "Paper = .hx name + surgery generator + seed library".
   Body: deliverable shape — hexfold's `.hx` notation is the
   pasteable, deterministic name; the nanobud menus in
   `src/hexfold/menus.py` are the Euler-validated junction menu; the
   graph-surgery generator is the scheme's executable definition; the
   library entries are the seed registry. A separate deliverable from
   the dr173020 review, which gets only the gap note.*
3. *Title ≈ "Selling points: executable name, validity, annotations,
   APM". Body: executable nomenclature — a name regenerates the
   structure deterministically; combinatorial validity — Euler/valence
   asserted before geometry; graph-tier property annotations
   (sublattice-parity → magnetic class, rigidity/floppy-mode count,
   chirality) with the cited-vs-computed provenance split; the
   assembler framing (`precis_se/modes.py`) — a library entry is a
   build spec for atomically precise manufacturing, with per-junction
   synthesis-accessibility honestly annotated for today's stochastic
   routes.*
4. *Title ≈ "Defensive publication; venue and registry format open".
   Body: posture — defensive publication per the standing FTO rule
   (gr334824): publish the scheme + library openly, no patent search;
   figures render via `se-view-figures.md` (the paper eats its own
   dogfood). Open — candidate venue/format, and whether the library
   ships as a nanopub series or a dataset DOI: decide once the
   generator exists.*

*Then: append the new handles to `me628772`'s reading-order line;
repoint `docs/backlog/threads/nanobuds-paper.md` Horizon 5; delete
this file and regenerate `docs/backlog/INDEX.md`
(`scripts/docs-index`).*

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
