---
status: draft
title: "dr42995 structural review — 14 mechanical shelf moves, 3 that strand a transition, and why the four carried-over items are half stale"
---

# Shelf order, not prose

Read of dr42995's live heading tree (1,875 live headings, `retired_at IS NULL`)
via a recursive `parent_chunk_id` walk. `section_path` is empty and heading
`meta` carries no `level`, so every depth below is *derived* — distance from the
root chunk dc1505619.

Everything in §Mechanical moves is executable by writing `parent_chunk_id`
and/or `ord`. Nothing in it requires touching a word of text.

## STATUS: mechanical moves EXECUTED 2026-08-29

All 30 chunk-moves in §Mechanical moves are **applied and verified in prod**,
with two deliberate exclusions (below). Verification: every one of the 30 has
its planned `parent_chunk_id`, and **every `ord` is unchanged** — nesting moved,
reading order did not. Post-move tree check: `live_roots = 1` (was 2),
`live_under_dead_parent = 0`, `live_total = 9692`.

Moves write only `pos` + `parent_chunk_id` and log a `moved`/`reparented` event
carrying `from`/`to`, so each one is individually reversible from its event row.
No text changed, so nothing re-embeds.

**Excluded, on evidence rather than caution:**

- **dc1514500 "Proof-of-Concept: Simplified Target"** — the review flagged its
  boundary as a read-the-body call. Its body is *assembly* validation ("Before
  full 2×5×3 implementation, validate with: linear chain (3 boxels, 2
  interfaces)… 2×2×2 assembly (8 boxels, 12 interfaces)"), not software. It
  belongs to the scope section dc1514305 it already sits in. Left alone.
- **dc1514577 "Loop Structure"** — the other flagged boundary. Its children are
  DNA construct arithmetic (dc1514580 "Total: 2×(5+17+1+26)+24 = 122 bp"), which
  is not obviously Stitching Window Design. No body evidence either way, so not
  moved.

Still open from this file: everything in §Needs prose (3 items), and the full
re-nesting of dc1514305, whose remaining ~70 flattened children were out of
scope for the confident subset.

## Mechanical moves

Ranked by reader gain. *(All executed — see §STATUS.)*

| chunk_id | current parent | current ord | operation | target | why |
|---|---|---|---|---|---|
| dc1511596 "Conclusion" | dc1511544 | 5251 | promote | parent → dc1505619 (root); ord unchanged | It is the **document's** conclusion — it summarises face codes, signal physics, the standardized interface, co-design, cascade assembly, the C1–C8 open questions and the MVT pathway (dc1511597–dc1511632). It currently renders as a subsection of the Part "How to Model and Discover Boxels". Its ord already sits after that Part's last chunk and before "Potential Papers" (dc1511633), so the parent flip alone is the whole fix. |
| dc1508169 "Comparison: Protein vs. Covalent Cage vs. MOF Scaffold" | dc1508016 | 2017 | promote | parent → dc1507053; ord unchanged | Depth 4: root → "How to Make a Boxel" → "Structure Exploration with Generative AI" → "**Alternative Scaffold**: De Novo Protein Cages" → Comparison. This subtree carries dc1508170, the scaffold-comparison table that was edited on 2026-08-29 to state the hybrid architecture (covalent cage for the optical PoC, conductive framework walls only for inter-boxel relay). The document's central scaffold decision is shelved as a footnote to a rejected alternative. Promoting in place makes it the closing section of the Part it decides. |
| dc1514323, dc1514329, dc1514331 | dc1514305 | 7716, 7722, 7724 | demote | parent → dc1514322 "Design Constraints" | dc1514322 has **zero** children, live or retired. Its three constraint subsections were flattened up to be its siblings. |
| dc1514337, dc1514341, dc1514346, dc1514354 | dc1514305 | 7730, 7734, 7739, 7747 | demote | parent → dc1514336 "Computational Workflow" | Same flattening. Phases 1–4 of the workflow sit as siblings of the workflow heading, which is childless. |
| dc1514359, dc1514476, dc1514490, dc1514498, dc1514500 | dc1514305 | 7752, 7779, 7793, 7801, 7803 | demote | parent → dc1514358 "Software Architecture" | Same flattening. Run ends before dc1514515 "Petal Sequence Generation Algorithm", which opens a new top-level topic — confirm that boundary by reading dc1514500's body, not by prose judgement. |
| dc1514562, dc1514565, dc1514570 | dc1514305 | 7865, 7868, 7873 | demote | parent → dc1514561 "Stitching Window Design" | Same flattening. Whether dc1514577 "Loop Structure" joins the run is a read-the-body call, not a prose call. |
| dc1511312, dc1511323, dc1511326, dc1511335 | dc1511184 | 4967, 4978, 4981, 4990 | demote | parent → dc1511311 "Hybrid Optimization Strategy" | dc1511311 is childless; "Phase 1: Simulated Annealing", "Phase 2: Local Search" and "Convergence Criteria" are the strategy it names. Clean run to the end of dc1511184's children. |
| dc1510159, dc1510165 | dc1510156 | 3814, 3820 | demote | parent → dc1510158 "Structural Design" | dc1510158 is childless; "Functional Regions" and "Self-Protection Mechanism" are its content. Only four children under dc1510156, so the run is unambiguous. |
| dc1505620 "Glossary" | *(none — NULL)* | 1 | re-parent | parent → dc1505619 | dc1505620 is a **second root**. It and the title chunk dc1505619 are the only two live chunks in the draft with `parent_chunk_id IS NULL`; every other top-level section (including "External References", dc1516535) parents to dc1505619. Any depth-derived render or tree walk sees two documents. Its 36 `term` children are fine. |
| dc1512192, dc1512193 | dc1512176 | 5847, 5848 | re-parent | parent → dc1505619 | These are `\printglossaries` and `\appendix` — document-level LaTeX structure, currently at depth 4 inside the *Outline* subsection of a paper proposal ("The Selectivity Spectrum", dc1511899). The main-matter/appendix boundary is buried inside an unrelated section. Ord is already correct: they land immediately before "Supplementary Material" (dc1512194). |
| dc1516533, dc1516534 | dc1516531 | 9836, 9837 | re-parent | parent → dc1505619 | Same class: `\printindex` and `\bibliographystyle…\bibliography` shelved as body paragraphs of "Acknowledgements". Ord already places them before "External References" (dc1516535). |
| dc1509382 "The amplifier imperative: a historical parallel" | dc1509371 | 3037 | promote | parent → dc1508856; ord unchanged | Depth 4, sitting inside a section literally titled "Summary". It is not a summary item: five paragraphs of argument plus open question TQ-SD-01 (dc1509387) and the Song et al. result (dc1509388). Promoting makes it the closing section of "Signal Domains and Transduction" — where the gain problem is the payload. |
| dc1509455 "Why MOF Boxels, Not DNA Bricks?" | dc1509389 | 3110 | demote | parent → dc1509391 "State of the Art: Connectivity, Energy, and Interfacing" | A single-paragraph aside (dc1509456) shelved as a Part-level peer of six multi-section topics. It answers the state-of-the-art survey directly above it and its ord is already contiguous with that section's tail (dc1509454, ord 3109). See §Needs prose — the *title* is a separate, non-mechanical problem. |
| dc1506641 | dc1506620 | 489 | re-parent | parent → dc1506640 | A `paragraph` whose text opens `- Cassette vs cage: …` — the answer body to open question TQ-POR-09 (dc1506640), shelved as its sibling instead of its child. |

### Do not "delete-empty-wrapper" here

Six live headings have zero children (dc1510158, dc1511311, dc1514322,
dc1514336, dc1514358, dc1514561) — and zero *retired* children too, so nothing
was cascaded out from under them. They are not emptied wrappers; they are real
section titles whose subsections were flattened up one level by the converter.
Deleting them destroys the only surviving grouping labels. The fix is always to
demote the siblings, never to delete the wrapper.

Section dc1514305 is the worst instance: **83 direct heading children, all at one
depth**, including four wrapper-level titles and a duplicated "Validation
Criteria" (dc1514670 and dc1514807 are same-titled siblings). The four rows
above are the confident subset; the section would repay a full re-nesting pass.

## Needs prose

**ALL THREE ADDRESSED 2026-08-29** on Reto's instruction ("fix these please").
What was written, so a reader can judge it rather than take it on trust:

1. **dc1506631** heading → `The NOR Gate: Second Milestone (Two Inputs)`
   (parallels its sibling dc1506623 `The NOT Gate: Primary Target`). And
   dc1506637's closing sentence, which still read as if NOR were optional
   (*"deferred to a second iteration if the single-input NOT gate succeeds"*),
   became: *"It is also where the proof of concept ends (Section [dc1506283]):
   the two-actuator CASSETTE is synthetically more challenging than the
   inverter, so the NOR pentamer is built after the single-input NOT gate
   succeeds, not instead of it."* This keeps the real sequencing (NOT first)
   while making NOR the PoC endpoint rather than a stretch goal.

2. **dc1508169** moved to the front of Part dc1507053, immediately after the
   Plan of Record paragraph dc1507054 — and the review's concern about stranded
   framing turned out not to apply: **dc1508169 has exactly one child, the table
   dc1508170, and no prose at all.** Nothing to strand. The lead-in was still
   worth writing because the table previously had no introduction of any kind; a
   new paragraph now opens the section, names the three scaffold families, and
   ties the comparison to the hybrid Plan of Record above it.

3. **dc1509455** retitled `Why MOF Boxels, Not DNA Bricks?` →
   `Why Not DNA Bricks as the Computational Substrate?`, which is what the
   section actually argues and no longer asserts MOF as *the* scaffold. Its body
   dc1509456 ended *"…and MOFs for the functional boxels themselves"*, which the
   hybrid contradicts; it now reads *"…and conductive frameworks—MOF or COF—for
   the walls of those boxels that must relay electronic signals between cages
   (Section [dc1507103])."*

Original findings follow, for the record.

**dc1506631 "Stretch Goal: NOR Gate (Two Inputs)"** — the XOR→NOR conversion
made NOR the PoC gate, while dc1506624 makes the single-input NOT gate the
primary target. The heading still calls NOR a stretch goal. *The shelving
implication is: none.* dc1506623 (NOT, "Primary Target") and dc1506631 (NOR) are
already correct siblings under dc1506620 "Logic Gate Implementations", in the
right order — single-input before two-input. Moving either makes the section
worse. What is needed is a heading rewrite of dc1506631 that names NOR's actual
status, and a check that dc1506637 ("NOR is functionally complete…") is still
positioned as the justification rather than as a stretch-goal rationale. **Not
mechanical — text edit only.**

**dc1508169 to the *front* of Part dc1507053** — the mechanical move above
promotes it in place, leaving it at the Part's end. Putting the scaffold
comparison up front, adjacent to the Plan of Record paragraph dc1507054 that
declares the hybrid, would serve the reader better. But dc1508169 currently
opens as a comparison *against* the protein-cage alternative it sits under, and
lifting it to the front strands that framing: a new lead-in sentence would be
needed introducing the three-way comparison cold. **Not mechanical.**

**dc1509455's title** — "Why MOF Boxels, Not DNA Bricks?" asserts MOF as *the*
scaffold, which the 2026-08-29 hybrid edits (dc1507054, dc1508170) contradict:
the boxel is a covalent cage, and framework walls are the relay option. The
demote above is mechanical and independent; retitling to reflect the hybrid, and
checking dc1509456's single paragraph against it, is a prose job. **Not
mechanical.**

## Considered and rejected

- **Demote "Potential Papers" (dc1511633) into "Supplementary Material"
  (dc1512194).** Eight paper proposals look like appendix material. They are
  not: `\appendix` (dc1512193) falls at ord 5848, *after* the entire Potential
  Papers subtree. In the LaTeX source this is main matter. Leave it.
- **Move "The Selectivity Spectrum" (dc1511899) into the Assembly Part
  (dc1509883).** It is ~295 chunks and 78 headings — roughly 8× its siblings —
  and it reads as a treatise on assembly strategy. But it carries the full
  paper-proposal skeleton: "Target Venue" (dc1511900), "Abstract" (dc1511902),
  "Prior Art and Novelty" (dc1512148), "Key Contributions" (dc1512161),
  "Outline" (dc1512176). Those subsections are meaningless outside "Potential
  Papers". The shelf is right; only the size is anomalous.
- **An orphaned wrapper at the business-plan cut site.** There is none. The
  wrapper dc1515855 "Business Plan and Market Analysis" is itself retired along
  with its whole subtree, and a corpus check for live chunks under a retired
  parent returns **zero rows** for this ref. The cascade was clean.
- **The supplement dangling after the cut.** It does not. dc1512194 retains 30
  live section children spanning ord 5850–9834; the cut removed one of them from
  the middle. Ord 9157 → 9340 is a numbering gap, not a structural gap.
- **Reordering NOT and NOR under dc1506620.** See §Needs prose — current order
  is correct.
- **Merging "Critical Challenges" (dc1506270, under Introduction) with "Critical
  Challenges and Risk Retirement" (dc1506986, under Design Principles).** Two
  near-identically titled sections at the same depth in different Parts. This is
  a content-duplication question, not a shelf question; the Introduction copy is
  a deliberate forward summary and the Executive Overview names the same pair
  again (dc1506203). Out of scope.
- **"Broader Impact" (dc1506216) breaking the numbered 1–6 run under Executive
  Overview.** It is deliberately outside the numbered list of research thrusts.
- **"Glossary" appearing at ord 1, before "Executive Overview".** Correct front
  matter. Only its NULL parent is wrong (see §Mechanical moves).

## Validation of the four carried-over items

- **Promote dc1511596 — HELD.** Still parented to dc1511544; still the document
  conclusion. Now the highest-value move in the file.
- **Promote dc1509382 — HELD.** Still at depth 4 under a "Summary" heading
  (dc1509371). Promote one level to dc1508856.
- **Re-parent dc1511899 into the Assembly Part — SUPERSEDED.** Rejected above:
  it is a paper proposal with venue, abstract and outline; the shelf is correct.
- **Delete the emptied wrapper dc1512176 — STALE.** dc1512176 "Outline" is not
  empty. It holds a live 14-item outline (dc1512177–dc1512191) that is a
  legitimate part of the paper proposal. It does, however, contain the two
  misfiled LaTeX structure chunks dc1512192/dc1512193 — re-parent those to the
  root and leave the wrapper standing.
