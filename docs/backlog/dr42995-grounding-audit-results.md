---
status: draft
title: "dr42995 grounding audit — 76% of the boxel draft's claims need no repair; the defect load is 53 claim errors, 17 wrong sources, 6 mis-anchored edges"
---

# What the boxel draft actually rests on

Every claim hub cited by `dr42995` that has a readable passage was checked,
sentence against source, by opus verifiers reading the actual chunk text. Method
and its evolution: `grounding-verification-rubric.md`. Population and partition:
`evidence-edges-assert-support-with-no-passage.md`.

**920 cited hubs → 590 with a passage → 428 hub-edge pairs verified** (a hub can
carry more than one evidence edge; each was scored separately, which repeatedly
mattered — see §Per-edge below).

## Result

| disposition | count | share | repair |
|---|---|---|---|
| **NONE** | **325** | **76%** | none needed |
| CLAIM_DEFECT | 53 | 12% | fix the sentence |
| WRONG_SOURCE | 17 | 4% | re-cite; re-grounding cannot help |
| NEEDS_SECOND_EDGE | 12 | 3% | add an edge |
| SCOPE_DRIFT | 8 | 2% | none — broader subject, nothing false |
| FRONT_MATTER_ANCHOR | 7 | 2% | re-anchor |
| WRONG_CHUNK | 6 | 1% | re-ground; **never edit the sentence** |

Passage-level: **65% clean** (279 SUPPORTED or ADJACENT_CHUNK).

Per-shard `NONE` rates were 73–82% with one outlier at 59% (shard 05, the
protein-design cluster — see §Author-collision). Stability across six shards
means this is a property of the corpus, not of sampling.

`source_corrupt` fired **zero times in 428 edges**. The Greek-strip scar is real
(`ingest-strips-greek-glyphs.md`) but does not reach this draft's grounding.

## The rubric decision that dominated everything

**`ADJACENT_CHUNK` absorbed 105 of 428 edges — 25%.**

These are edges where the anchored chunk is on-topic but the confirming detail
sits elsewhere in the same paper. Every one is benign. Under the first rubric's
`ord ± 1` reading they scored PARTIAL, and an earlier three-shard run reported
"51% partial, only 34% supported" on exactly that basis.

Confirmed cases ranged **1 to 40 chunks away**, so the correct rule is *any
chunk of the same source, provided the anchored chunk is already on-topic*.
Distance is not the signal; topicality is.

Had the repair lanes been driven off the coarse rubric, a quarter of the corpus
would have been "repaired" for a chunk-boundary artifact — rewriting correct
sentences to match passages that were never the whole evidence.

## Defect classes worth naming

**Unit-magnitude errors are the most common numeric defect**, and they are large:
- `fi176610` — Landauer limit as ≈2.9 **aJ**; it is 2.9 **zJ** (10³)
- `fi176705` — photomechanical force in **nanonewtons**; source says 0.084–4.4 **mN** (10⁶)
- `fi176620` — "20 pW/K" attributed to a paper where `pW` has zero hits

**The number belongs to the comparison, not the subject** (confirmed twice):
- `fi177497` — "DMOF-1 shear modulus ≈0.3 GPa" is **MIL-47's** value from the same table; DMOF-1's are 0.16 and 0.11
- `fi176436` — a "30 ms" figure belonging to the MEMS comparator, not the material

**A claim refuted by its own quoted passage.** `fi177615` says 430 kΩ "per
amine-gold link"; the anchored quote says "~430 kΩ **for two** amine–Au bonds".
Quote fidelity was perfect; the reading was wrong. No gate that verifies quotes
can catch this.

**Named-entity relabels** — the class that made `misattributed` widen beyond
techniques: TMB/DAB/OPD → TMB/**ABTS**/OPD; ferritin for cytochrome cb562;
H⁺/**Ca²⁺** for H⁺/Na⁺; "2'-O-methyl" absent from 128 chunks; a 2.7 Å **X-ray**
structure reported as **cryo-EM** (found independently by two separate passes).

**Grounding in the source's own introduction.** `fi176793`'s ">10⁴ switching
cycles" is intro background citing *other groups'* compounds. `fi177423` is
anchored on a paper that *cites* the work rather than performing it. The mint
gate checks that a source is primary **in general**; it cannot see that a source
is secondary **for this claim**. `reground.py`'s hearsay-section filter is the
existing guard — the pass that created these edges had none.

## Author-collision is the most alarming finding

`fi177399` (Top7) and `fi177401` (macrocyclic D-peptides) are `WRONG_SOURCE` on
**both** their edges, grounded to NCAA/rotamer-parametrization papers with zero
hits for `Top7`, `macrocyc` or `D-amino`. The shared factor is an author surname
(Kuhlman). Two false attributions in the same shard fit the pattern — `fi176950`
credits "Yaghi and co-workers" to a paper whose corresponding author is Hexiang
Deng (Yaghi appears only in the reference list); `fi177415` credits
"Korendovych et al." to Pirro/Lombardi/DeGrado.

If author-name similarity is driving retrieval, that is a systematic defect, not
a per-claim error. **Not established** — it is one agent's inference from four
cases. Worth a dedicated check before it is repeated as fact.

## Per-edge scoring earned its place

Several hubs carry a sound primary edge **and** a dead one. `fi177585`,
`fi176770`, `fi176775`, `fi176773`, `fi176861` all have a good edge that would
have masked a broken co-edge under per-hub scoring. In two cases
(`fi176770`/`fi176769`, `fi176775`/`fi176774`) two hubs share the **identical
chunk** and one is right while the other is wrong — retrieval found a real
passage and extraction invented a second claim it does not support.

## A ref with bad metadata attracts bad edges

`ref 5267` appears **three times** as a wrong source (`fi176638`, `fi177749`,
`fi177585`). Its title was stored as *"Proceedings of the National Academy of
Sciences"* — the journal, not the paper — until repaired 2026-08-20 from its own
first chunk to *"Limits of economy and fidelity for programmable assembly of
size-controlled triply periodic polyhedra"*. A source with no meaningful title
gives retrieval nothing to match against. Plausible causal story, unproven.

Related, and worse: `ref 3185` carries a *Helicobacter pylori* title on a body
that is entirely a nanotoxicology review — metadata and body are different
papers.

## Substring false positives — the guard that paid for itself

Zero-hit probes without word boundaries produced errors in **every** shard:
`amino` inside *aminoterephthalate*, `IQ` inside *unique*, `face` inside
*surface*, `2 V` inside *0.2 V*. The last one **reversed a would-be
CLAIM_DEFECT** on re-probe — the claim was genuinely supported. Always
boundary-anchor before recording a zero.

## Coverage caveat

Shards 00–02 (177 hubs) first ran on the coarse rubric and are being re-verified;
their original numbers are **not** comparable and must not be merged with the
table above. The 428 pairs are the trustworthy measurement.

## Standing rule for every repair below

A mismatch has more than one cause. Before editing any sentence, establish
whether the **claim**, the **passage**, the **edge**, or the **source metadata**
is what is wrong. `WRONG_CHUNK` and `ADJACENT_CHUNK` claims are *true* — editing
them to match a partial passage destroys correct work.
