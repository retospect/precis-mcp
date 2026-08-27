---
status: draft
title: "rubric for the grounding-verification pass — what the 18-hub pilot proved and what it broke"
---

# The pass works; its rubric does not

A pilot ran 2026-08-20 over 18 claim hubs cited by `dr42995` (3 mandated
calibration hubs + 15 drawn with `setseed(0.42)`), reading each hub's sentence
against the full text of its grounding passages. **It passed its calibration
bar**: it independently caught `fi176432` and `fi177486`, the two hubs whose
detection I had declared the go/no-go condition for scaling to 922.

It also broke its own rubric in four places. Fix these before the full run —
each one changes verdicts, not just labels.

## 1. `WRONG_CHUNK` is missing, and it is the failure that matters at scale

Six of 18 hubs are cases where **the paper supports the sentence but the
attached chunk does not**:

- `fi176432` — sentence claims HKUST-1 Young's modulus 9–12 GPa; the attached
  chunk is about methane storage capacity. Another chunk of the *same ref 1698*
  reads *"the indentation modulus (I) of mono HKUST-1 is 11.5 ± 0.4 GPa … its
  Young's modulus (E) to be 9.3 ± 0.3 GPa"*. The claim is **true of its source**.
- `fi177412` — "12 diverse targets" is absent from the chunk, present in the
  paper's extended-data table (*"Number of binders against the 12 targets"*).

Verdicting strictly on the passage, as the pilot was instructed, labels these
UNSUPPORTED/PARTIAL. **If downstream repair edits claims rather than re-grounds
edges, this pass actively destroys correct work** — the exact inversion Reto's
ruling warns about, arriving from the opposite direction (there the corpus
looked wrong and the claim was right; here the *edge* is wrong and both claim
and paper are right).

Make it a second axis, not a label: every verdict carries
`passage_verdict` × `paper_verdict`. `passage=fail, paper=pass` ⇒ re-ground the
edge, never touch the sentence.

## 2. The corruption tell is wrong, and this is the second base-rate error

The pilot was told: zero Greek + non-ASCII present ⇒ extraction damage. Seven of
17 checkable sources (**41%**) matched it, and **every one** carried LaTeX
`\mu` / `\pi` / `\tau` macros. Mathpix/marker-style extraction escapes Greek by
design; those papers are not damaged.

This is the same species of error as the earlier 24%-of-corpus detector recorded
in `ingest-strips-greek-glyphs.md`. Two independent detectors, two base-rate
failures, same root cause: **a signal was read as evidence of damage without
first measuring how often it occurs in undamaged documents.**

Required discriminator, in order:
1. zero Greek codepoints (U+0370–U+03FF, U+00B5); **and**
2. other non-ASCII present; **and**
3. **no LaTeX Greek macro anywhere in the doc's chunks** — if `\mu`/`\pi`/`\tau`
   appear, Greek is escaped, not lost. Not corrupt. Stop.

Consequence: the `325 Greek-exposed / 313 exclusively` figure recorded for
`dr42995` was produced by the pre-(3) detector and must be re-derived before
anything is decided on it.

## 3. `NO_GROUNDING` conflates two defects with opposite remediations

Five hubs had no readable passage. They split cleanly:

| shape | example | source has live chunks? | fix |
|---|---|---|---|
| edge lost its chunk pointer | `fi177486`, `fi176638`, `fi176729`, `fi177479` | **yes** (51–126) | re-ground the edge — mechanical, no acquisition |
| source never ingested | `fi176753` (Stoddart, *The Nature of the Mechanical Bond*) | **no** (zero) | acquire and ingest the text |

Same label today, completely different cost. Split into `EDGE_UNGROUNDED` and
`SOURCE_UNINGESTED`, and partition **mechanically in SQL before spending any LLM
tokens** — a pilot rate of 5/18 means ~28% of a 922-hub run would be spent
discovering that there is nothing to read.

## 4. Technique/quantity misattribution has no label

`fi177394` claims *"validated by cryo-EM at 2.7 Å"*. The source's 2.7 Å is an
**X-ray crystal structure**; its cryo-EM reconstruction is **5.1 Å** (the 2.7 Å
that appears near cryo-EM is an RMSD, not a resolution). Neither reading is in
the source. This is not partial support, not off-topic, and
`STUDY_TYPE_MISREAD` is defined as the *reader's* error, not the claim's. It
landed in PARTIAL, which badly undersells it. Add `MISATTRIBUTED`.

## 5. The search boundary must be stated

For every PARTIAL the pilot had to decide whether to read beyond the passage.
Doing so changed the verdict's *meaning* three times and cost ~⅓ of its
queries. Once (`fi177394`) the wider check made the finding **worse**. Policy:
verdict strictly on the passage; run a bounded whole-paper keyword probe and
record it in a separate field. Both facts are needed — the passage verdict
drives the edge repair, the paper verdict protects the claim.

## The failure class the PARTIALs share

Nine of 18 were PARTIAL, and they fail the same way: **the sentence asserts more
structure than the passage carries** — a superlative, a cause, a comparison, a
priority claim, or a unit upgrade.

- `fi176409` — "*the primary* driver of research"; source lists it as one
  application among several, never ranks it.
- `fi176800` — source says "~200- to **3500**-fold"; sentence reports the top of
  a 17× range as the value.
- `fi177720` — source says 5 nm **process node**; sentence says "sub-5 nm **gate
  lengths**". Real gate lengths at that node are ~16–20 nm. A marketing label
  silently upgraded into a physical dimension.
- `fi177597` — quantitative core is verbatim-supported; "**the first** direct
  experimental evidence" is a priority claim the source never makes.
- `fi176612` — source's objection is "large size and mass"; sentence renders it
  "lower bandwidth". Mass→slowness is the reader's inference.
- `fi177646` — passage covers rotaxanes; sentence also asserts catenanes.

This class is invisible to every existing gate: the edge is real, the source is
primary, the quote verifies. **These are repairable by weakening the sentence to
what the passage carries** — cheap, safe, and it makes the draft more true. This
is the highest-yield repair lane in the corpus.

## One structural fact to verify before scaling

Every hub in the 18-hub sample had **exactly one** evidence edge. If that holds
across all 922, then `EDGE_UNGROUNDED` and `WRONG_CHUNK` are *unrecoverable*
failures rather than degradations — there is no second witness to fall back on.
That changes repair design, so measure it first.

## Wave-1 revisions (2026-08-20, 118 hubs across two shards)

Two opus shards ran the rubric above. It held — the two-axis split did its job
and `source_corrupt` correctly never fired (no disputed quantity in either shard
hinged on a Greek-prefixed unit). Rates are stable across shards and match the
pilot: **SUPPORTED ~35%, PARTIAL ~48%, WRONG_CHUNK ~9%**. Six changes before
scaling.

### 1. Split `PARTIAL` by severity — it is carrying 48% of all verdicts

It currently spans a dropped adjective and a wholly invented subject. Replace
with:

- `PARTIAL_MINOR` — a qualifier is dropped; meaning survives intact. Often no
  repair needed.
- `PARTIAL_MATERIAL` — the sentence adds structure the source does not carry
  (superlative, cause, comparison, priority, unit upgrade, extra subject).
  Repair = weaken the sentence to what the passage supports.
- `PARTIAL_FABRICATED` — an element has **zero** support anywhere in the source.
  Repair = delete the element or retire the claim. Example: `fi176460`'s
  "5–10-fold enhanced cascade efficiency", where the source gives a detection
  limit and no multiple at all.

### 2. `WRONG_SOURCE` ≠ `WRONG_CHUNK` — add it

`WRONG_CHUNK` means the paper supports the claim and the edge points at the
wrong passage — re-grounding fixes it. `WRONG_SOURCE` means **the paper does not
contain the result at all**, so re-grounding within it is guaranteed to fail:

- `fi176620` — "20 pW/K" grounded on Lee 2013; `pW` has 0 hits there (the result
  is Cui 2019, a different paper).
- `fi176623` — Ni₃(HITP)₂ claim on a ref that never says `HITP`.
- `fi176594` — "multi-kilogram scale" on a milligram-scale solid-phase paper.
- `fi176638` — grounded on ref 5267, whose real title (recovered from its own
  first chunk, 2026-08-20) is *Limits of economy and fidelity for programmable
  assembly of size-controlled triply periodic polyhedra* — geometric assembly,
  not conductive MOFs.

The repair pass must report `WRONG_SOURCE` and stop, not grind.

### 3. `ADJACENT_CHUNK` — check `ord ± 1` before ever scoring PARTIAL

A chunk boundary is not a grounding defect. `fi176643` scored PARTIAL only
because an abstract split across two chunks: 428885 ends at *"superior to that of
metals"* while **428886** carries *"five times stronger and half the weight …
retaining 80%"* verbatim. Every element was correct. The verifier nearly
mis-scored it from a truncated dump.

**Always read the neighbouring chunks of the same ref before scoring anything
below SUPPORTED.** This is a false-PARTIAL generator and it is cheap to rule out.

### 4. `NEEDS_SECOND_EDGE` for comparative claims on one-sided sources

`fi176659`, `fi176660`: the source measures the subject but never the baseline,
so the comparison is unsupported. Folding these into UNSUPPORTED hides that the
repair is *adding an edge*, not fixing one.

### 5. Widen `misattributed` from techniques/quantities to **named entities**

`fi176448` swaps a reagent — source says TMB/DAB/OPD, sentence says TMB/**ABTS**/
OPD, and `ABTS` returns zero rows across the paper. Caught only because the
verifier stretched the definition. Materials, molecules, reagents and instruments
all belong in scope.

### 6. Two traps to warn verifiers about explicitly

- **The number belongs to the comparison device, not the subject.** `fi176436`'s
  "30 ms" is the MEMS comparator's, not azobenzene's. Mis-scores in both
  directions.
- **A second corruption mode exists**, unrelated to Greek: chunk 395239 renders
  `±` as `(`. Do not assume Greek-drop is the only extraction scar.

### Also surfaced: nothing checks claims against each other

`fi176623` says single-crystal Ni₃(HITP)₂ is metallic; `fi176640` says it is a
bulk semiconductor. Both live, both in the same draft's cohort. Every check we
have compares a claim to a *source*; none compares claims to **each other**.
That is a distinct detection lane and it is cheap — the hubs are already
embedded. Worth building after this pass.

### Distinguish "no support found" from "source not fully ingested"

`fi176401`'s NOT_FOUND is an ingest limit: all 21 chunks of that ref are
supplementary material and the main text is absent. That is `UNDECIDED`, not
counter-evidence. Verifiers must check whether the source is *completely*
ingested before reading silence as absence.

## Gate wiring — the rubric's approve-time consumer (2026-08-27)

An external review of the staged candidate queue independently re-derived
this rubric's taxonomy (PARTIAL severity split, `WRONG_CHUNK`/`WRONG_SOURCE`/
`MISATTRIBUTED`/`NEEDS_SECOND_EDGE`) and added the requirement the rubric so
far only implies: **approval must check claim-level coverage, not edge-level
support.** Its 30-hub sample: 20 had a `partial` edge, only 2 were
all-`yes`; ~7 material mis-groundings (fabricated range, wrong source,
inference-as-result). The failure the field-containment gate cannot see: a
single `partial` quote releasing an unsupported conjunction.

Wire, at approve time (`nanopub/gates.py`, payload-dependent — it has the
quote envelope the mint-time gates lack):

- Every atomic proposition of the sentence must be covered by the **union**
  of selected passages; multiple complementary quotes are allowed
  (fi191120/fi191293 shape: different passages cover different clauses).
- `PARTIAL_MINOR` passes; `PARTIAL_MATERIAL`/`PARTIAL_FABRICATED` block.
- Verdicts stamp `verified_claim_sha` so a claim edit re-opens coverage
  (the edge-level plumbing shipped 2026-08; this extends it to the
  approval payload).

Run the review's pilot before scaling: 10–20 claims, human-reviewed,
including complementary-partial cases.

## Method caveat, unchanged

Verdicts are LLM judgments, advisory and unreviewed. The nanobud audit that
preceded this had **three** errors found on verification, including two
"contradictions" that were both wrong in opposite directions. Treat output as
leads. Nothing is written from this file without reading the source.
