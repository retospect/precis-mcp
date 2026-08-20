---
status: draft
title: "`load_bundle` silently drops edgar/datasheet evidence — attachable, then invisible to the nanopub"
---

# Two evidence kinds can be attached but never published

`taproot/hub.py::attach_evidence` accepts any source in
`EVIDENCE_SRC_KINDS = {paper, patent, edgar, datasheet}`. The nanopub read
path narrows further, without saying so: `nanopub/evidence.py::load_bundle`'s
`_source` helper returns `None` for any ref whose kind is not `("paper",
"patent")`, and a `None` source is skipped by both the supporter loop and the
`contradicts` loop.

So for an `edgar`- or `datasheet`-sourced edge:

- **A supporter vanishes from the minted artifact.** A claim grounded solely
  in a datasheet would mint a nanopub whose source list is empty, with no
  error — the gate that checks for evidence reads the same emptied bundle.
- **A `contradicts` edge does not block.** `gates.py::check_contradicts`
  iterates `bundle.contradicts`, so a dispute filed from an SEC filing or a
  datasheet is silently unenforced. (Hub- and finding-sourced disputes are
  also absent, but *deliberately* — see
  `disputes-edge-nonblocking-disagreement.md`. This one is not deliberate.)

## Blast radius today: zero

Measured read-only against prod 2026-08-20 — every evidence edge into a
`TAPROOT:claim` hub, by source kind:

| source kind | relation | rows |
|---|---|---|
| paper | corroborates | 1321 |
| paper | establishes | 161 |
| paper | contradicts | 1 |
| finding | contradicts | 1 |

No `edgar`, `datasheet` or `patent` evidence edges exist. The defect is purely
latent — which is exactly why it should be fixed before someone attaches the
first one and trusts the result. Note the corpus also has **zero patent
evidence edges** despite `patent-evidence-parity.md`; `patent` at least
survives `_source`, so that is a separate, non-silent gap.

## The fix, and the question inside it

Mechanically: derive `_source`'s kind tuple from `EVIDENCE_SRC_KINDS` instead
of restating it, so the write door and the read door cannot drift again. That
is a one-line change plus a test asserting the two sets are equal.

But it needs a decision first, because the narrowing may have been intentional
and merely undocumented: **is an SEC filing or a datasheet an admissible
citation in a published nanopub?** A datasheet has no DOI, no authors and no
retraction channel, so the citation model the nanopub emits (`doi`, `year`,
`pdf_sha256`) degrades. Two coherent answers:

1. **Yes, publishable** — widen `_source`, and decide what identifier stands
   in for a DOI in the emitted citation.
2. **No, internal-only** — then `attach_evidence` should *refuse* these kinds
   for hubs on a publication path, rather than accepting a write that is
   silently discarded downstream. Narrow `EVIDENCE_SRC_KINDS`, or gate at
   approve with an explicit violation naming the unpublishable source.

Either way the two doors must agree. Today they disagree in the direction that
loses evidence without telling anyone, which is the worst of the three
options.

Found 2026-08-20 while correcting the `contradicts` gate-scope claim in
`precis-nanopub-help`; the double filter was the surprise.
