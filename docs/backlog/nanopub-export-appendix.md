---
status: draft
title: export appendix listing the nanopub artifact behind each cited claim
---

# export appendix listing the nanopub artifact behind each cited claim

## Motivation / why
A `[fi<id>]` cite already resolves to its primary-source paper(s) at
export (`_render_finding_cite` → `taproot.cite.finding_cite_keys`), but
once a claim hub has a signed/anchored/published nanopub, the export
says nothing about it — the reader can't see that the claim exists as a
verifiable, signed, timestamped artifact, nor find its trusty URI. The
appendix makes minted claims visible in the paper itself with zero
draft edits: claims not yet minted simply don't appear, so the appendix
grows as signing proceeds. First slice of the larger fi→np migration
(see the retire-fi discussion; `[np<id>]` cite syntax is NOT this item).

## In scope
- A per-export accumulator (pattern: `_TrustCtx` in
  `src/precis/export/_trust_marks.py` — resolve once per finding,
  insertion-ordered) that, for every finding cited in the export,
  looks up the hub's publish row and records it when state is
  `signed` / `anchored` / `published`.
- End-matter section ("Published claim artifacts" or similar) in the
  LaTeX and docx export paths, one entry per minted claim: the frozen
  AIDA sentence, the trusty URI, and the anchored/published date.
  Embargoed (signed-but-unpublished) artifacts render with their
  local `/np/<code>` URL or an "embargo" mark — decide at build time.
- Emitted only when at least one cited claim is minted (no empty
  section).

## Explicitly NOT in scope
- `[np<pubrow>]` citation syntax and the fi→np prose migration —
  separate item.
- Inline per-cite nanopub marks (superscript "np") — polish, later.
- Any change to mint gates, review flow, or the nanopub schema.
- Retiring or changing `[fi<id>]` resolution.

## Acceptance criteria
- An export of a draft citing a hub whose publish row is `signed` (or
  later) contains an appendix entry with that claim's AIDA sentence and
  trusty URI; a draft citing only unminted hubs produces no appendix
  section.
- A hub cited many times yields exactly one appendix entry.
- `published` vs embargoed entries are visually distinguishable.
- Existing exports of nanopub-free drafts are byte-identical (no
  regression in the `[fi]` cite path or trust-mark end-matter).

## Target + blast radius
`src/precis/export/latex.py`, `src/precis/export/docx.py`, likely a
small shared helper beside `src/precis/export/_trust_marks.py`; reads
`nanopub_publish` / `nanopub_artifacts` via existing store accessors.
No workers, no routes, no schema.

## Open questions / decisions log
- Embargoed artifacts: local URL vs "under embargo" text — pick when
  building; either satisfies the AC.
