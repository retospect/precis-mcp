---
status: draft
title: Pathway figures in draft + export-time data-package appendix
---

# Pathway figures in draft + export-time data-package appendix

## Motivation / why
Reto (2026-08-16): "I want a way to include the diagrams in draft; both
the pareto front and the generated path. When this is included, a data
package (a formatted, small print appendix) is added at export time for
the computational people to get at least the numbers and params and
versions we had … the key need is to make it so that someone can copy
pasta it from a file. Or should it go to zenodo."

Design discussed same day; awaiting Reto's confirmation of the shape
below before promotion to ready. The engine-version stamp just became
load-bearing (the 0.14.0 void-re-score incident): a figure without its
`autocatpath_version` + params is unreproducible.

## Proposed shape (from the discussion)
- **Figures via static renderers**, entering the draft as `figure` refs:
  viz.py's matplotlib profile for the path; a new small matplotlib twin
  of `build_frontier_scatter` for the pareto front, reusing the shipped
  marker grammar (star/circle = frontier, filled/hollow = trust, color =
  band). Each figure ref links `derived-from` the pathway/quest refs it
  renders.
- **Data package generated, not curated** — trigger is figure inclusion:
  the draft exporter walks included figures' provenance links and builds
  the appendix from ref meta already stamped (energies/barriers, MLIP
  params, `autocatpath_version`, precis sha, dispatch token). No new
  bookkeeping.
- **Two artifacts, not one**: (a) printed appendix 7–8 pt monospace
  (6 pt fails journal floors + OCR), verbatim/fixed-columns, no
  ligatures/hyphenation — compact per-figure table (species, E, Ea,
  tier, trust, params, versions); (b) machine-readable JSON/CSV sidecar
  emitted next to the PDF — for arXiv, into the ancillary `anc/`
  directory. Sidecar is the canonical copy-paste source; the appendix
  is the human skim + fallback.
- **Zenodo = third tier, not instead**: full package (structures, NEB
  trajectories, complete params) at publication time, DOI slot printed
  in the appendix header once it exists. Optionally the deposit IS the
  nanopub artifact bundle (signed/OTS continuity figure → numbers →
  deposit).

## Explicitly NOT in scope
- Interactive/JS figures in draft export (that's the web surface;
  see pathway-profile-renderer-unification for the JS story).
- Automatic Zenodo upload.

## Acceptance criteria (provisional until confirmed)
- A draft can include pareto-front + path-profile figure refs; export
  renders them via the static renderers.
- Any export containing ≥1 such figure automatically appends the
  small-print data appendix AND emits the JSON sidecar; both carry
  `autocatpath_version` and the full param set per figure.
- Sidecar content round-trips: parsing it reproduces every number
  printed in the appendix.

## Target + blast radius
draft export pipeline, `figure` kind ingestion of quest/pathway SVG-or-
matplotlib output, new matplotlib pareto renderer (home: autocatpath or
precis.quest — decide at spec time), provenance-link walk in the
exporter. **Sibling appendix**: `nanopub-export-appendix.md` adds a
different end-matter section (nanopub artifacts behind cited claims) to
the same latex.py/docx.py paths via the `_trust_marks.py` accumulator
pattern — whichever lands second must slot into the other's end-matter
ordering, and this item should reuse that accumulator pattern.

## Open questions / decisions log
- Reto to confirm the three-tier shape (appendix + sidecar always,
  Zenodo optional) — discussion sent 2026-08-16, unanswered.
- Pareto matplotlib twin lives in autocatpath (engine owns grammar) or
  precis.quest (frontier data lives here)?
- PDF-embedded attachment (`\attachfile`) in addition to the sidecar,
  or sidecar-only (viewer support for attachments is uneven)?
- Appendix font size: 7 pt vs 8 pt (both above journal floors).
