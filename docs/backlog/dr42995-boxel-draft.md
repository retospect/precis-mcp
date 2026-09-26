---
status: draft
---

# Dr42995 boxel draft

Grouped 2026-09-26 from 4 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## What the boxel draft actually rests on

_Grouped 2026-09-26; was `dr42995-grounding-audit-results`, status draft._

Every claim hub cited by `dr42995` that has a readable passage was checked,
sentence against source, by opus verifiers reading the actual chunk text. Method
and its evolution: `grounding-verification-rubric.md`. Population and partition:
`evidence-edges-assert-support-with-no-passage.md`.

**920 cited hubs → 590 with a passage → 428 hub-edge pairs verified** (a hub can
carry more than one evidence edge; each was scored separately, which repeatedly
mattered — see §Per-edge below).

### Result — complete, all 10 shards, 619 hub-edge pairs

| disposition | count | share | repair |
|---|---|---|---|
| **NONE** | **450** | **73%** | none needed |
| CLAIM_DEFECT | 80 | 13% | fix the sentence |
| NEEDS_SECOND_EDGE | 26 | 4% | add an edge |
| WRONG_SOURCE | 21 | 3% | re-cite; re-grounding cannot help |
| SCOPE_DRIFT | 15 | 2% | none — broader subject, nothing false |
| FRONT_MATTER_ANCHOR | 12 | 2% | re-anchor |
| WRONG_CHUNK | 8 | 1% | re-ground; **never edit the sentence** |
| NO_ANCHOR | 7 | 1% | → `repair-evidence` |

Passage-level: **62% clean** (384 SUPPORTED or ADJACENT_CHUNK).

Counting `SCOPE_DRIFT` as benign — it is — **75% of the draft's verified claims
need no work at all.** The genuine repair load is **149 edges**, and only 80 of
those are claims; the rest is plumbing.

Per-shard `NONE` ran 54–82%. Two outliers pull down: shard 05 (protein-design
cluster, 59% — see §Author-collision) and shard 02r (54%, driven by 10
`NEEDS_SECOND_EDGE` and 6 `NO_ANCHOR`). Stability across the other eight means
this is a property of the corpus, not of sampling.

`source_corrupt` fired **zero times in 428 edges**. The Greek-strip scar is real
(`ingest-strips-greek-glyphs.md`) but does not reach this draft's grounding.

### The rubric decision that dominated everything

**`ADJACENT_CHUNK` absorbed 165 of 619 edges — 27%.**

These are edges where the anchored chunk is on-topic but the confirming detail
sits elsewhere in the same paper. Every one is benign. Under the first rubric's
`ord ± 1` reading they scored PARTIAL, and an earlier three-shard run reported
"51% partial, only 34% supported" on exactly that basis.

Confirmed cases ranged **1 to 398 chunks away** (that one in a 619-chunk
review), so the correct rule is *any chunk of the same source, provided the
anchored chunk is already on-topic*. Distance is not the signal; topicality is.

The three shards first run on the coarse rubric were **re-verified from scratch**
under the corrected one. The comparison is the proof: `WRONG_CHUNK` fell 4 → 0
(shard 00) and 5 → 1 (shard 01). The coarse rubric was not merely inflating
PARTIAL — it was *manufacturing mis-anchored edges* out of chunk boundaries, and
acting on it would have re-grounded edges that were already correct.

Had the repair lanes been driven off the coarse rubric, a quarter of the corpus
would have been "repaired" for a chunk-boundary artifact — rewriting correct
sentences to match passages that were never the whole evidence.

### Defect classes worth naming

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

### Author-collision is the most alarming finding

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

### Per-edge scoring earned its place

Several hubs carry a sound primary edge **and** a dead one. `fi177585`,
`fi176770`, `fi176775`, `fi176773`, `fi176861` all have a good edge that would
have masked a broken co-edge under per-hub scoring. In two cases
(`fi176770`/`fi176769`, `fi176775`/`fi176774`) two hubs share the **identical
chunk** and one is right while the other is wrong — retrieval found a real
passage and extraction invented a second claim it does not support.

### A ref with bad metadata attracts bad edges

`ref 5267` appears **three times** as a wrong source (`fi176638`, `fi177749`,
`fi177585`). Its title was stored as *"Proceedings of the National Academy of
Sciences"* — the journal, not the paper — until repaired 2026-08-20 from its own
first chunk to *"Limits of economy and fidelity for programmable assembly of
size-controlled triply periodic polyhedra"*. A source with no meaningful title
gives retrieval nothing to match against. Plausible causal story, unproven.

Related, and worse: `ref 3185` carries a *Helicobacter pylori* title on a body
that is entirely a nanotoxicology review — metadata and body are different
papers.

### Substring false positives — the guard that paid for itself

Zero-hit probes without word boundaries produced errors in **every** shard:
`amino` inside *aminoterephthalate*, `IQ` inside *unique*, `face` inside
*surface*, `2 V` inside *0.2 V*. The last one **reversed a would-be
CLAIM_DEFECT** on re-probe — the claim was genuinely supported. Always
boundary-anchor before recording a zero.

### Coverage

Complete. All 590 hubs with a readable passage, all 619 edges, all under the
corrected rubric. The coarse-rubric verdict files (`verdicts_00/01/02.jsonl`) are
**superseded** by `verdicts_00r/01r/02r.jsonl` and must not be merged with the
rest.

Not covered, by construction: the 330 hubs whose edges have no passage at all
(271 repairable, 59 needing acquisition) — those go to `repair-evidence`, not to
verification.

### Standing rule for every repair below

A mismatch has more than one cause. Before editing any sentence, establish
whether the **claim**, the **passage**, the **edge**, or the **source metadata**
is what is wrong. `WRONG_CHUNK` and `ADJACENT_CHUNK` claims are *true* — editing
them to match a partial passage destroys correct work.

## What is actually left to convert

_Grouped 2026-09-26; was `dr42995-conversion-remainder`, status draft._

Companion to `dr42995-grounding-audit-results.md` (which audits what is *already*
grounded). This item covers the inverse: what remains ungrounded, and why the
obvious plan for it is wrong.

### The population, measured

Of dr42995's ~6,858 `paragraph`/`item` chunks:

| class | count | status |
|---|---|---|
| already carry `[fi` | 754 | done |
| uncited, name a source ("et al.") | 20 | **wave 3 leg A** |
| uncited, carry a bare quantity | 700 | the real remainder |
| authorial prose (design, plan-of-record, transitions) | ~5,400 | legitimately needs no citation |
| `chunk_kind='table'` | 251 | **unblocked 2026-08-27** — see §The table blocker |

Of the 20 attributed-uncited, only **12 are groundable claims**. Six are the
draft's own embedded `Review finding (…)` / `Open question [TQ-…]` annotations,
and two are authorial. See §The meta-annotation trap.

#### The measurement itself was wrong once — do not repeat it

An earlier pass reported **675** attributed-uncited chunks. The regex
`\[@?[a-z]+[0-9]{4}` matched `[dc1516244]` — `dc` satisfies `[a-z]+`, `1516`
satisfies `[0-9]{4}` — so it was counting the draft **citing itself** via internal
chunk cross-references. The true figure is 20, a 34× overcount that made the
appendix leg look 92 chunks short when it was complete.

**Rule: never regex-match bracket citations without excluding
`[<2-letter-code><digits>]` handles.**

#### And filter `retired_at IS NULL`

`chunks.retired_at` marks superseded chunks. They stay in the table but are
excluded from reading order and export, so **the MCP draft door never shows
them** — but raw SQL does. dr42995 carries **135 retired prose chunks**, 21 of
them in the quantitative set (hence 706 → 700).

This only bites when targets are handed to an agent as SQL-derived chunk ids,
which is exactly how wave 3 leg A was briefed: 2 of its 12 targets
(`dc1507445`, `dc1507894`) turned out to be retired, with live successors
(`dc2928113`, `dc1516370`) *already cited*. Work that looked undone was done.
Any batch pass driven off SQL must filter `retired_at IS NULL`; one driven off
the MCP reading order gets it for free.

### The remainder is far smaller than it looks — measured, not estimated

The 700 are the hard class: the number is stated bare, with **no source named**,
so grounding one means *finding* a source rather than matching a stated one.
That framing led to an early estimate of "100+ agent-waves, a batch job." **Wave
3 leg B measured it instead, and the estimate was wrong.**

Leg B swept all 500 chunks of ord 6500–6999 and triaged the 93 that carry a
quantity:

| class | count |
|---|---|
| authorial design spec | ~42 |
| derived / arithmetic | ~15 |
| already cited before the leg | 14 |
| empirical, searched, **no held source** | ~15 |
| **empirical, newly grounded** | **5** |
| off-domain / tool output | 2 |

So **~6% of uncited quantity-bearing chunks convert.** Roughly 60% never needed
a citation at all — they are the authors' own design parameters and their own
arithmetic. Extrapolated across the 700, the whole remaining draft is worth on
the order of **40 new hubs**, not thousands.

The cost driver is *reading*, not minting: a leg must read ~500 chunks to find
~5 groundable ones, and both legs run so far used well under their 10-hub cap.
That makes the remainder roughly **12–15 more sweep legs** — tractable as agent
waves after all. A worker lane would still be cheaper per chunk, but it is no
longer a prerequisite.

**The triage census is the real deliverable of each leg**, more than the mint
count. It is what converts "700 unconverted chunks" from an alarming backlog
into a known, mostly-benign population.

Leg C (ord 1000–1499) independently reproduced the shape: ~25–30 authorial,
~25–30 derived, ~8–10 off-domain, ~15 meta-annotations, 14 tables — 4 grounded.
Two legs, two buckets, the same distribution.

### A cite is not proof of grounding

Wave 3 leg C surfaced a defect that undermines every census above, filed as
gripe **265228**: deleting a claim hub leaves dangling `[fi<id>]` cites in draft
prose. Corpus-wide, live chunks only: **7 dangling cites, 3 documents, 6 dead
hubs** — three of them in dr42995, and two created as recently as 2026-08-27,
so this is ongoing rather than a historical scar.

The reason it matters *here*: a conversion pass treats a chunk that already
carries a cite as converted and skips it. So a deleted hub silently reverts
grounded prose to ungrounded **while still looking grounded**, and every census
in this document counts it as done. `dc1507432` is the worked example — it cites
dead `fi176919`, while its sibling `dc1507242` carries live `fi177523` for the
same σ = 40 S/cm figure.

Any future sweep should therefore validate existing cites, not just count them.
The detection query lives in the gripe.

Densest 500-ord buckets (tight number+unit regex, uncited only):

```
6500 → 77   1000 → 73   0 → 55   7000 → 51   1500 → 49
2000 → 47   7500 → 46   3000 → 43   500 → 42   6000 → 34
```

`section_path` is **empty** on this draft and heading `meta` carries no `level`,
so sections are addressable only by **ord range**, never by name. Any batch pass
must bucket by ord.

### The triage is the deliverable, not the mint count

A pass that grounds 8 claims honestly and reports 40 as unfindable is a success;
one that grounds 40 by stretching hubs is damage. Every chunk sorts into:

- **empirical** — a measured value about the world → ground it
- **authorial design spec** — a number the authors *chose* (tolerance, protocol
  setting, budget) → no citation, correctly
- **derived / theorem** — computed from other draft values, or a mathematical
  fact ("the cube is the only Platonic solid that tessellates 3D space")
- **off-domain** — economics, package counts, SMT throughput. This chemistry
  corpus can never ground these; report, don't chase.

Wave 2 established the precedent by correctly leaving the DNA sequence-design
filters, optical hardware choices, and the C1–C8 risk narrative uncited.

### The meta-annotation trap

dr42995 contains its own review annotations as ordinary body chunks:

> `Review finding (GAPS) [MINOR]: Microfluidic DNA circuit claim is uncited. Add
> reference (e.g. Karzbrun et al. 2014 or Kim et al. 2006).`

These are **addressed to the author** — a TODO list, not claims. They name
sources and lack `[fi`, so every retrieval heuristic and every conversion agent
scores them as prime targets. Grounding one produces a hub asserting that a gap
exists. Six sit in the 20-chunk attributed set alone (dc1507266, dc1509405,
dc1509410, dc1509962, dc1511412, dc1512300).

A batch pass **must** exclude chunks opening with `Review finding` or
`Open question [TQ-`. Their *content* is still valuable — it is a list of gaps
the author already knows about — but as input to acquisition, not to minting.

### The table blocker was a stale flag — fixed 2026-08-27

The 251 table chunks were written off as unreachable: `meta.flag =
'needs-table-review'` with no `meta.table` grid, so every structured edit door
refused while `get()` rendered the LaTeX from `text` perfectly. 407 such chunks
across 24 refs corpus-wide.

The divergence was one-sided by construction. The **read** path
(`table_data.py::table_payload`) already falls back to `parse_latex_table`; the
**write** path read `meta.table` and nothing else. And the flags are stale —
they were written by an older parser. Measured against today's parser on all
251: **244 recover (97%)**; the 7 that don't are float wrappers whose `tabular`
landed in a different chunk, plus two exotic column specs (`>{\scriptsize}`,
nested `\multicolumn` spanning a header).

The fix is the DRY one: `handlers/draft.py::_edit_table` now recovers through
`table_payload` — the *same* function the read path uses — and clears the stale
flag once a grid is persisted. No migration, no backfill, and it covers all 407
corpus-wide, not just this draft's 251.

**This is still not a licence to hand-reconstruct `table={header,rows}`.** The
recovery is a deterministic re-parse of the chunk's own stored text; typing a
grid by hand is 251 chances to mangle live append-only content. The distinction
is the whole point, and two agents were right to refuse the hand version.

Note the recovered cells stay **strings** — raw LaTeX carries no type
information, and coercing `"2"` to `2` would silently retype identifiers.

### Prerequisite for any batch lane

`wants=` / `provenance=` acquisition mode is unreachable over MCP — the verb
signature in `tools/core.py::put` *is* the schema and omits them, and
`workers/planner_prompt.py` actively teaches the broken call. The fix shipped in
`52b680d1` but **is not deployed**, so until a `/go` lands it, any worker pass
must file acquisition stubs via a plain `put(kind='paper')`.

## Shelf order, not prose

_Grouped 2026-09-26; was `dr42995-fable-structure`, status draft._

Read of dr42995's live heading tree (1,875 live headings, `retired_at IS NULL`)
via a recursive `parent_chunk_id` walk. `section_path` is empty and heading
`meta` carries no `level`, so every depth below is *derived* — distance from the
root chunk dc1505619.

Everything in §Mechanical moves is executable by writing `parent_chunk_id`
and/or `ord`. Nothing in it requires touching a word of text.

### STATUS: mechanical moves EXECUTED 2026-08-29

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

### Mechanical moves

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

#### Do not "delete-empty-wrapper" here

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

### Needs prose

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

### Considered and rejected

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

### Validation of the four carried-over items

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

## Boxel draft dr42995 — citation residue after waves 4+5 (2026-09-17)

_Grouped 2026-09-26; was `boxel-42995-citation-residue`._

Open items left by the findings-citation conversion of prod draft `dr42995`
(slug `nano-computer`). Full per-leg record: memory
`boxel_42995_taproot_and_assembly.md` §WAVE 4/5. Owner for tool gaps:
`src/precis/draft/` (edit door) and `src/precis/taproot/`.

### Reto decisions (prod data, no code)

- **Approve queue.** Every hub cited from the draft since 2026-09-16 is
  still `candidate`. Hold the review-grounded ones until their primaries
  ingest: fi344137 (nacre; stub pa344160), fi344166, fi345433 (Berger
  1966), fi345435 (kinesin 8 nm; grounded on a reference-list entry),
  fi345436 (DUT-8 254 %; stub pa345389), fi345597, fi345598, fi345618.
- **UiO-66 modulus.** dc1512450 / dc1512524 / dc1512617 use E = 20 GPa;
  held sources say ~40 GPa (fi177485). The value feeds the buoyancy tables
  dc1512517 / dc1512563. Recompute, or label 20 GPa as a deliberate
  conservative assumption.
- **Azobenzene in MIL-53(Al).** pa54525 (pc1826524) states the
  photoisomerisation is blocked inside the host, but dc1513485 / dc1513628
  recommend that pairing as the PoC actuation mechanism.
- **dc1510490** "hours to days" crystallisation vs pc194078 "within a few
  minutes" once in the temperature window.
- **dc1513606** MIL-53(Al) table row was blanked (no Al-specific data);
  relabel to Cr-MIL-53 ~9 % (fi177501) if wanted.
- **dc1513596** now carries an in-prose "no held source pins this
  threshold" remark; cut or keep.
- Numbers deleted for lack of any held source, leaving derived
  figures-of-merit on unheld inputs: CNC E 110–150 GPa (dc1515132 /
  dc1515146 → FOM in dc1515137), CD-MOF ρ 0.76 (dc1515155 → FOM 1.9),
  kinesin 6 pN stall, DUT-8 40 % strain, MIL-53 8–50 % contraction.

### Hand edits the draft edit door cannot make (gr344147: caption text is
outside the tabular body)

- dc1507410: delete `, Cr--CO $\approx 37$~kcal/mol (155~kJ/mol)` from the
  `\mciteboxpC{uddin2001a}` text (pa154 never studied Cr–CO).
- dc1507690: drop `~\citeC{uddin2001a}` from the caption (supports no row).
- dc1510282: `stability\cite{nielsen1991,shakeel2006}.` →
  `stability [fi176833].`

### Corpus defects (fixable by an agent, prod writes)

- refs.title wrong: ref 1768 holds von Neumann 1956 "Probabilistic logics"
  text but is titled as a Lie-algebroid survey; ref 1257 holds Lyons &
  Vanderkulk 1962 (TMR) but is titled as an IASLC lung-cancer paper.
- Ref-level-only grounding on hubs cited by the draft: fi176854, fi176914,
  fi176927, fi177517 (pre-existing), fi345428 (Maekawa; add the pc3878131
  passage edge with `src_chunk_id`).
- fi344166 `scope.material` still reads "ZIF-8-vs-UiO-66" after the
  ZIF-8-only reword.
- dc1515596 RC-demos table: "Conductive polymer network" row has no cite;
  table uses legacy `\cite{}`.
- dc1510282 table: LNA / 2′-OMe RNA rows cited by nothing.

### Still unsourced in the held corpus (acquire or leave qualitative)

Stubs filed: pa343747, pa343814, pa343816, pa344107 (PEDOT), pa344148 +
pa337318 (kinesin), pa344160 (nacre), pa345389 (DUT-8), pa345429 (van
Ginneken), pa345496 (green chemistry), pa345579 (Kapton datasheet).
No target identified: dc1510382 Hamaker ~1e-20 J; dc1510420 / dc1510383 /
dc1510400 / dc1510463 DNA duplex ΔG 17–20 kT; dc1512120 aqueous
diffusion-limited 1e10 M⁻¹s⁻¹; dc1512234 boron 1 mg/L; dc1512231 zeolite
persistence; dc1507690 metal–ligand ΔG/Kd rows; dc1515779 / dc1515784 /
dc1515758 / dc1515781 antifuse-FPGA / EPROM precedents; dc1516092 PEDOT
1e-1 vs 1e-5 S/cm; dc1515173 PLA/Al barrier; dc1514882, dc1514940,
dc1514947, dc1515154; ord 5174 DNA-mismatch 1 nm; ord 5189 binding table;
dc1513605 azobenzene-driven contraction fraction; dc1512569 / dc1512639
COF-300 E, H.

### Tooling gaps surfaced (repo work)

- `tools search` degrades to lexical silently when the local embedder
  returns 429 (`embedder_service.py` `max_inflight`=4, saturated by
  sibling gate stacks); the caller cannot tell. Surface the mode in the
  result header, or let `scripts/prod-precis` honour a pre-set
  `PRECIS_EMBEDDER_URL` so a quiet cluster embedder can be used.
- Subagents parked on their own background draft edits twice per leg;
  the edit CLI blocks in psycopg under a deep pgbouncer queue while the
  write has already landed (memory `precis_search_hang_no_progress`).
