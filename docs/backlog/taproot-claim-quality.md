---
status: draft
---

# Taproot claim quality

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Taproot — gate evidence to the paper's own results, not its lit review

_Grouped 2026-09-26; was `taproot-evidence-section-gating`._

A claim hub accepted a paragraph from a prior-work/review section as evidence.
That paragraph's own sources did not support the claim, so the chase laundered
an unchecked assertion into a "verified" one — a false-verification path, not a
ranking nuisance. Evidence must come from the meat of a paper ("X was doped
with Y and we measured Z"), never from "it is known that…": a review paragraph
cites *elsewhere*, so attaching one asserts a provenance the chunk doesn't have.

Fix: gate admissible evidence on the `role3` axis the chunk classifier already
defines (`own` / `background` / `furniture`, `src/precis/workers/classify.py`)
— accept `own`, refuse `background`, and surface the refusal reason so a chase
can look elsewhere rather than silently scoring lower. Owner
`src/precis/taproot/` (attachment in `hub.py`, scoring in `trust.py` /
`seniority.py`).

Hard dependency: `role3` must actually be populated. The classifier is
default-OFF and the melchior handler has not written a `role3` value since
2026-08-08 (gripe gr204385) — so ship the backfill first, or the gate refuses
everything. Until then a downgrade-not-refuse variant is the safe interim.

Found the hard way during the nanobuds review, 2026-08-13. Correctness;
medium.

## Claim-strength inflation: an unsized failure class

_Grouped 2026-09-26; was `taproot-claim-strength-inflation-sweep`, status idea._

The reground pass over draft 173020 was built around two known failure shapes:
**proxy grounding** (the passage asserts or defers rather than evidencing —
"right paper, wrong chunk" was the dominant real fix) and **term leak** (a term
from an adjacent claim contaminating a hub during minting). A third shape
exists, was found by accident, and has never been searched for.

### The shape

The claim's **modal or epistemic register drifts** while every word remains
present in the source. The source hedges; the claim asserts. Nothing is
fabricated, no citation is wrong, the grounding chunk genuinely is the right
passage in the right paper — and the claim still overstates what the source
says.

### The one known instance

Finding `192706`, live and unresolved, holds a `contradicts` edge onto hub
`fi191316`. Verified 2026-08-14: direction 192706 → 191316, created
2026-08-04 21:03 UTC, no annotation on the link row. Its title states the
defect in full:

> `dc2445944`: `fi191316` claim-strength inflation — "will ultimately require"
> vs source's "could be used"

Note the `dc` prefix: it was raised against a **draft chunk**, i.e. caught on
the drafting side rather than by any hub-side check.

### Why every existing check misses it

- **Evidence grounding** passes — the passage genuinely supports the topic, and
  the strict-judge rubric asks whether the content is primary, not whether the
  certainty matches.
- **The prose pass** passes — the sentence is well-formed and faithful at the
  word level.
- **Term screens** pass — no foreign or leaked vocabulary is involved.

The defect lives entirely in the gap between "could" and "will". This is also
why it is plausible the class is common: nothing in the pipeline is looking.

### Suggested direction

A sweep comparing each hub's claim against its grounding chunk on modal and
hedge strength. A cheap first detector is a lexicon diff — assertive registers
(`will`, `must`, `requires`, `demonstrates`, `shows that`, `establishes`)
appearing in a claim whose grounding passage sits in a hedged register
(`could`, `may`, `suggests`, `indicates`, `is consistent with`, `potentially`).
That is a candidate generator, not a verdict; each hit still needs judging.

Two design points worth deciding up front:

- The existing `contradicts` edge is already the right affordance for recording
  a hit — `192706` demonstrates the shape, so a sweep can emit into an
  established structure rather than inventing one.
- Where the fix belongs is genuinely open. Tightening the hub's claim text is
  one option; the instance above suggests the inflation can equally originate
  draft-side, in which case fixing the hub leaves the draft sentence wrong.

### The meta-finding

That `contradicts` edge has sat live and unresolved for ten days. Nothing
consumes `contradicts` edges — they are written and then not read. Whatever
sizing sweep gets built will produce more of them, so a triage path for the
relation is arguably the prerequisite rather than the follow-on.

## Citation matcher can attach a wrong title to a correctly-matched reference

_Grouped 2026-09-26; was `citation-matcher-title-mismatch`, status idea._

### The defect

Discovered during a manual taproot reground pass over draft 173020, by two
independent scout agents that hit the same artifact separately — which is why
this is filed as a real defect rather than a one-off.

In paper `ref_id=783` (Torrens & Castellano 2014, *J Mol Model* 20:2263,
"Cluster solvation models of carbon nanostructures"), the ingested bibliography
entry for reference **[15]** carries author list, journal, volume and page range
that correctly identify **Krishnan, Dujardin, Treacy, Hugdahl, Lynum & Ebbesen
(1997), "Graphitic cones and the nucleation of curved carbon surfaces", Nature
388:451–454, DOI 10.1038/41284** — but its **title field reads "Photoisomerization
in dendrimers by harvesting of low-energy photons"**, an entirely unrelated
paper. The bibliography chunk is `pc64792`.

### Why it matters more than a cosmetic metadata wart

Reference [15] is the load-bearing citation for two taproot claim hubs
(fi189542, fi189543) whose only grounding is a proxy passage in ref 783 that
defers to [15] for the actual evidence. Resolving those hubs down to their true
primary depends on that bibliography entry being trustworthy. A wrong title
means:

- **Title-based dedup/lookup against Crossref or S2 will either miss or match
  the wrong record.**
- **An agent chasing the citation chain can be led to ingest an unrelated paper
  while believing it has found the primary.**
- **Any claim grounded through that chain inherits a silent provenance error.**

This is exactly the failure mode the taproot evidence graph exists to prevent.

### Scope is unknown and should be measured before designing a fix

One confirmed instance, found incidentally. The obvious first question is
whether this is a marker/GROBID parse slip local to this PDF's reference list,
or a systematic mismatch introduced when a parsed reference is reconciled
against an external metadata source. A cheap detector: for ingested bibliography
entries, cross-check the title against the DOI/volume/pages-derived record and
flag disagreements — the corrupted rows are self-inconsistent, so they are
mechanically findable without human review.

**Cross-check that raised confidence:** the same Krishnan 1997 reference is
cited correctly, with the right title, in at least five other papers'
bibliographies in the corpus. So the underlying reference data is fine in
general; the corruption is specific to this entry in this paper.
