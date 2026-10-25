---
status: draft
pillar: memory-graph
prio: normal
owner: knowledge-mesh
---

# Corpus quantitative extraction

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

**Ruling 2026-10-12** (Reto, chat): owned by
[knowledge-mesh](threads/knowledge-mesh.md#resume), not claims-and-evidence.
The point is comparability: extracted numbers from partially overlapping,
similar experiments must carry traceability to the source span and
conditions, and pass validation gates before they count as citable. Claims
as they stand are not comparable or graphable; this item is the comparable
layer. The survey paper that consumes it is December at the earliest and
probably later (the manual-check volume in the sign-off question below).

## Reconciliation with what shipped (2026-10-21)

The port landed as `src/precis/extract/` (anchors, verify, proposals,
prompt, accept) plus the `survey_extract` and `measure_accept`
coordinator jobs (`workers/job_types/survey_extract.py`); build notes
are in [measures-substrate](measures-substrate.md) §"Next slice". The
shipped shape differs from the pilot in one way that settles several
items below: there is no binding document. The local reader sees one
chunk at a time and its reply is checked and written in the same call,
and the stored evidence is raw `[chunk, start, end]` offsets on an
append-only chunk, so a later splitter change cannot re-point a stored
row.

| item | state | where |
|---|---|---|
| 1 anchor scheme | shipped; the "no scheme declared" guard is moot (no documents) | `extract/anchors.py::ANCHOR_SCHEME` = 2, stamped on `meta.anchor_scheme` |
| 2 condition role | shipped | input rows carry `role` context/preparation/model; the keyword guard became the registry's `required_conditions` |
| 3 group condition sets | open | slice T2 below |
| 4 escalation severity | replaced | see "Escalations" below |
| 5 sub-sentence anchors | moot | evidence is the sentence span; `verify.literal_supported` proves `550–575` is a complete numeric region inside it; no `sub_sentence_span` check was ported |
| 5 recipe overrides, units in a column | open | slice T1 below |
| 5b categorical rows | shipped | `value_form='categorical'`, verbatim-only check |
| 6 unattributed | shipped | `source_attribution='not_established'` + `tier='asserted'`; NULL tier = could not establish |
| 6 intra-paper conflict | decided: no `contradicts` relation | the deterministic numeric rule (knowledge-mesh §3, gardener-value slice 5(b)) writes `disputes` with `meta.scope='intra-paper'`; adjudication stays on the nanopub path |
| 6 author-declared non-reproducibility | open, small | `meta.source_caveat` exists on the row shape; the reader contract has no field for it |
| 7 three small bugs | shipped | `eb5f64103`, `f4df6bcb8`, `377bc418d` |
| 8 port with seams | shipped | library pure, worker owns DB + LLM, no MCP verb |

**What has run on prod.** One `survey_extract` (job 496639, pa1866,
2026-10-18): 99 of 99 chunk calls failed with "placement='local'
requested but the big chain has no local rung", 0 rows written; its
`measure_accept` child (496669) failed on an empty batch. The up-front
placement refusal and the zero-write failure line have landed since.
No extracted row exists on prod (`measures` with `meta.extraction_run`:
0 of 239 live output rows, 2026-10-21).

### Wilson batch acceptance, as built, and three findings

As built: a batch is one `(paper, extraction_run)`; strata are
measurands within that paper; 60 rows sampled
(`accept.DEFAULT_SAMPLE`); the frontier tier judges each sampled row
against its anchored sentence; `wrong` and `undecidable` both count as
errors; the batch is accepted when the one-sided Wilson upper bound
(z = 1.645) is at or under 0.05. The verdict lands on the accept job's
`meta.acceptance`; sampled rows get a `reviews` row.

1. **A per-paper batch almost never passes.** With zero errors the
   upper bound is z²/(n+z²), which first reaches 0.05 at n = 52
   (`test_zero_errors_at_n_52_passes_the_bar`). Prod holds 239 live
   output rows over 53 subjects (about 4.5 per subject); the qu202467
   pilot wrote 122 runs over 14 papers. A typical paper yields fewer
   than 52 rows, so every batch fails however good the rows are.
   **Recommendation:** the batch is the run token, not the paper.
   `survey_extract` already takes `params.run_token`; give a trial or a
   filler window one token across many papers, make `measure_accept`'s
   `paper` optional when `extraction_run` is given, and stratify by
   paper × measurand, which is what the 2026-10-14 ruling says.
2. **Acceptance marks no row.** Nothing reads `meta.acceptance`, so an
   accepted batch and a failed one look the same to `best_measure` and
   to any mesh pass. Row `meta` cannot carry it: `measures` is
   append-only and `precis_measures_frozen` (0187) freezes `meta`.
   **Recommendation:** no row write. A store read
   `accepted_runs(tokens)` maps an `extraction_run` token to the
   latest finished `measure_accept` job's `meta.acceptance` for that
   token; mesh stages (gardener-value slice 5 (b)–(d)) and promotion
   (T3) read rows whose token is accepted, and `best_measure` gains an
   `accepted_only` flag (default off, so the pilot's hand-written rows
   still read). Rows the sampler judged `wrong` are superseded on
   re-extraction.
3. **The sampler sees one sentence.** A condition stated in the next
   sentence or in Methods reads `undecidable`, which counts as an error,
   so the bar measures span width as much as extraction quality.
   **Recommendation:** send the whole chunk with the anchored span
   marked, plus each input row's own span; the verdict gains `class` ∈
   {wrong_number, wrong_measurand, missing_condition, wrong_unit,
   bad_anchor, other}. These are the failure classes gardener-value
   slice 5's trial reads.

### Escalations

Item 4's motivating noise (`inferred_condition_basis`, 62 % of the
pilot's escalations) came from the binding format's per-result
applicability basis, which the port does not have. What escalates now
is drops (`unknown_measurand`, `unresolvable_anchor`,
`literal_not_in_span`, `bad_shape`), flags
(`unit_without_text_support`, the store's `implausible_value` and
`glyph_suspected`), and LLM errors, capped at 200 per job in the job
summary. A severity rank is not needed. What is needed is a count per
drop reason in the job's `summary_meta`, so a trial can be read without
paging through 200 lines. It is a small change, part of slice T1.

### Castor trial results (gardener-value slice 5) — HOOK

> **Pending.** The coordinator will paste the castor `survey_extract`
> trial (about 100 jobs over held full-text papers of active quests)
> here: rows written per paper, drops by reason, acceptance per batch,
> and the Opus failure classes. Use it to decide (a) whether T1 or the
> prose-path fixes (findings 1–3 above, the `value_form` bug) come
> first, (b) whether tables are where castor's errors are (then T1
> before T2), and (c) the `sample_n`/bar for filler batches. Until it
> lands, the order below is a default, not a ruling.

## Slice T — tables to structured measures and subject refs

**Status 2026-10-24:** the acceptance substrate (findings 1–2) shipped —
`measure_accept` takes `extraction_run` without `paper` (run-level batch,
sample stratified paper × measurand) and each verdict records its coverage
(the papers in the sampling frame + the frame's `max_id` high-water mark,
frozen at the draw; a run-level accept is refused while extract jobs for
the token are live).
`Store.accepted_runs(tokens)` resolves the latest *counting* verdict per
covered paper (only `bar` ≤ `DEFAULT_BAR` and `z` ≥ `DEFAULT_Z` counts;
pre-coverage verdicts grant nothing), and `best_measure(accepted_only=True)`
keeps a token row
only under its paper's pass, at or under the mark, with its anchor.
ACs 5–6 (accept half) are covered by `tests/test_survey_extract_job.py`
`TestRunLevelAcceptance`. T1–T3, the sampler context widening (finding 3)
and the promotion half of AC6 remain open.

Prod holds 124,257 live `table` chunks on papers, 114,410 of them
pipe-delimited (2026-10-21); median 4 lines, p90 11, p99 21. Today the
reader emits one JSON result per cell under `max_tokens=2048`, so a
table of 10 rows × 3 value columns costs about 30 results and truncates.
The pilot's answer, a recipe the materializer expands, is ported here.
Ingest stores tables as pipe text only, with no column metadata, and
the splitter already makes each pipe row its own sentence
(`anchors.SENTENCE_BREAK`'s `(?<=\|)\n(?=\|)`), so a row has an anchor.

### T1 — table recipes, with units in a column

Moved to [table-numeric-extraction](table-numeric-extraction.md)
(gr506762, 2026-10-25), which builds it as slice 2. Its contract adds
row orientation, log-scale cells, detected citation attribution and a
cell-level anchor in place of the row span. The escalation counts
(`drops_by_reason`, `flags_by_kind`) ship there too. Slice T's ACs
1, 2 and 7 below are covered by that item's slice 2 ACs.

### T2 — group condition sets for prose results

The prose reply gains a top-level `"groups": {"G1": {"inputs": [...]}}`;
a result naming `"group": "G1"` inherits those inputs, and its own
`inputs` override by condition name. Storage does not change: each run
still carries its own input rows (the comparison and `best_measure`
read per run). The compression is in the reply, so its size is linear
in conditions plus results, not their product. `meta.condition_set =
"G1"` on the input rows keeps the provenance.

### T3 — promote paper-local subjects to material refs

`measures.subject` is a paper-local label ("PdCoP-2", "Cu(111) with
O-vacancy"); `subject_ref_id` is the paper. Promotion gives a label a
ref, so rows from several papers about one catalyst become one
`material`'s rows.

- **When:** after a batch is accepted (finding 2 above), per
  `(paper, subject label)` with at least one `measured` or `computed`
  row.
- **Resolve, deterministic first:** exact match of the normalized label
  against a live `material` ref's slug, title or `meta.aliases`
  (`material_entity_upsert` dedups on slug; `_material_ops` has a
  lexical name/alias matcher). One hit → that ref. No hit or several →
  a gardener-ledger proposal (`action='promote'`, payload `{paper,
  label, candidates, measure_ids}`), structural, never a direct write;
  castor may draft the material title and aliases, Opus reviews per
  the gardener rulings.
- **Write:** the promoted row is a new `measures` row with
  `subject_ref_id` = the material, the same literal, measurand, inputs
  and primary evidence link, and `supersedes` = the paper row. Today
  `insert_measure` refuses a supersede across subjects (the target must
  share `subject_ref_id`); T3 allows exactly one crossing: paper →
  material/structure, same measurand, literal and span. The paper stays
  reachable through the evidence link's chunk, so nothing about
  provenance is lost and `best_measure` never counts the value twice.
- **Structures:** never minted from text. A `structure` ref needs a
  scene (`structure_save`), which a paper does not give. A row is
  promoted to a structure only when the label names an existing
  structure ref (by handle or exact alias) — the catpath quest's
  slabs. A `computed` row with no matching structure promotes to a
  material like any other.
- **Cross-paper identity** stays out of scope here (Explicitly NOT in
  scope, below); two papers' labels meet only when both resolve to the
  same existing material, or later through the gardener's merge path.

### Acceptance criteria (slice T)

1. A fixture table chunk with a header, 5 data rows, two output columns
   (one with `12.3` and `<1`), a unit column and one input column
   expands to 10 output runs; their `value_form`s differ for `12.3` and
   `<1`; the unit-column rows have non-null `reported_unit`; each run
   carries the row's input and the table-level condition.
2. A ragged table (one row with a missing cell) drops whole with
   `ragged_table`; a recipe naming an unknown measurand drops that
   column only.
3. A prose reply with one group of 3 inputs and 4 results writes 4 runs
   with 3 inputs each; one result overriding one input by name changes
   only that run; the reply fixture is linear in size.
4. A reply with `"value_form": "upper"` is written (or dropped with a
   recorded reason), never raises; `<1` lands `upper_bound`.
5. `measure_accept` with `extraction_run` and no `paper` samples across
   papers, stratified by paper × measurand, and a 60-row all-correct
   batch spanning 6 papers is accepted.
6. `accepted_runs` reports an accepted and a failed token correctly and
   `best_measure(accepted_only=True)` skips the failed token's rows; a
   promotion run on a fixture with
   one label matching a material alias writes a superseding row on the
   material, the paper row is superseded, and `best_measure` for the
   quest returns the value once; an unmatched label yields one ledger
   proposal and no write.
7. `summary_meta.drops_by_reason` counts match the escalation list.

Target: `src/precis/extract/{recipes,proposals,prompt,accept}.py`,
`workers/job_types/survey_extract.py`, `store/_measures_ops.py`
(`insert_measure` cross-subject supersede, `accepted_runs`,
`best_measure(accepted_only=)`),
`graph/garden/` (`promote` action), skill `precis-measure-help`. No
migration.

## Source-bound quantitative extraction — bind anchors, materialize records

_Grouped 2026-09-26; was `source-bound-quantitative-extraction`, status draft, prio normal._

### Motivation / why

We want sourced numeric results out of papers: a measurand, an exact reported
literal, its conditions, and evidence that points at the source rather than
paraphrasing it. The obvious shape — ask a capable reader to emit the finished
record as JSON — makes the reader copy quotations, units and boilerplate enum
fields into every row. That is expensive, and the copying is where fidelity is
lost: a pasted quotation can be silently cropped mid-literal, and a shared
condition can be restated per result until nobody can tell which results it was
actually demonstrated under.

A local pilot (untracked `paper-extraction-pilot/`, code committed without its
source-paper text) worked the alternative: the reader validates a *semantic map*
of one document and emits **bindings** — anchor ids instead of quotations,
shared defaults with explicit per-result exceptions, and recipes that expand
ordinary tables. A script materializes the full record by slicing the snapshot.

Measured over 20 papers, the binding encoding is 35% smaller than the pooled
record it reproduces byte-for-byte, and 67% smaller than the fully-expanded
form. Four fresh reader runs then tested whether a reader can actually work this
way. All four completed with **zero materialization errors**, producing 224
results from 451 evidence spans, of which **97.6% are anchor ids and 11 (2.4%)
are raw character offsets** — every one of those in the same paper, and every
one of them a sub-sentence interval literal (`550–575`, `125~150`) that no
anchor can address without cropping the value. That residue is not reader
sloppiness; it is a missing anchor granularity, and it is item 5 below.

Amplification from what the reader writes to the materialized record is 1.8–3.0×
against the pooled form and 5.2–7.0× against the fully expanded one. The best
case is the paper whose four table recipes produced 46 of its 66 results: 15.3k
of bindings → 46.2k pooled → 82.9k expanded. That same paper extracted the old
way produced 6 results and skipped its three tables — **which is a cost result,
not a quality result.** Both runs declared themselves representative rather than
exhaustive, they had different prompts and producers, and neither is a gold
standard; what it shows is that recipes price tabular data low enough to record,
not that the extraction got more correct. Nothing in this item measures accuracy
(see Explicitly NOT in scope).

Two behaviours worth keeping in the design: readers corrected the selector
rather than trusting it (one recovered two blocks dropped with *zero* flags, one
carrying the paper's basis for its central mechanistic claim), and one caught
and repaired a wrong span of its own during self-verification. Both are load
bearing — the map is a proposal, and the check loop is what makes a fallible
proposal safe to start from.

The pilot also surfaced defects that only appear once real binding documents
exist, and those are what this item is mostly about. They are listed under
In scope in priority order because the first one is a correctness bug, not a
feature gap.

### In scope

Items 1, 2, 5b, 7 and 8 shipped and item 4 was replaced; see the
reconciliation table at the top. What is left of the original list:

- **3. Group-level condition sets** → slice T2 (prose) and T1's
  table-level `conditions`.
- **5. Recipe per-row overrides, `<` → `upper_bound`, replicated
  `9.6 ± 1.7`, units in a third column** → slice T1 (`overrides`,
  `parse_literal` owns the form, `unit_column`).
- **6. Author-declared non-reproducibility** → a `source_caveat` field
  in the reader contract, written to `meta.source_caveat`; small, rides
  with T2.
- **6. Intra-paper conflicts** → the numeric `disputes` rule with
  `meta.scope='intra-paper'` (knowledge-mesh §3); not this item.

### Explicitly NOT in scope

- **Scientific adjudication.** Nothing here approves a result. Output stays
  proposals, and integration goes through the existing findings/nanopub path —
  no parallel signing or publication system.
- **Accuracy measurement.** The pilot establishes feasibility, not precision or
  recall. No held-out set and no human-adjudicated benchmark exists; do not let
  the 5.4× amplification or the 66-vs-6 comparison be read as a quality score.
  Both papers in that comparison were already inspected in an earlier round, and
  the two runs had different prompts and producers.
- **Cross-paper work.** Identity resolution across papers, a curated measurand
  ontology, and any comparability engine are all separate and larger.
- **OCR, supplements, figure digitisation, curve refits.** A table that is an
  undelimited character run (`L-Pd/rGO0.03055454.61`) stays `ocr_uncertainty`;
  we do not infer column boundaries. Two of the four pilot papers hit this — a
  collapsed header and a fully undelimited table — and in both the reader
  correctly promoted nothing. Recipes only reach well-formed pipe tables, and
  `table_shape` being non-null does not mean a table holds results: of eight
  shaped chunks in one paper, one held results, three held designed setpoints
  (bound as conditions), three were glossaries and one was OCR-broken.
- **The information-gain research** in `paper-evidence-selection-fisher-rao.md`
  and prod todo td449041. Do not let this item quietly become that project.

### Acceptance criteria

Slice T's list (above) replaces the original seven; the shipped items'
criteria are covered by `tests/test_extract_survey.py` and
`tests/test_survey_extract_job.py`.

### Target + blast radius

As slice T: `src/precis/extract/`, `workers/job_types/survey_extract.py`,
`store/_measures_ops.py`, `graph/garden/`. No migration (measures rows
are not a kind; decided below). Nothing in this item writes to prod,
signs or publishes from the build; prod runs are jobs launched by the
thread owner. The pilot stays the reference: its data never enters a
shipping commit.

### Open questions / decisions log

- **Kind or finding subtype?** Decided: neither. Rows live on
  `measures`, owned by the paper, promoted to a hub only when the result
  becomes a claim (knowledge-mesh decisions log); no migration.
- **Where does the reader run?** Decided and built: a queued
  `survey_extract` coordinator job on the local lane
  (`llm.op.survey_extract`), never an in-process call.
- **Escalation loop.** Decided 2026-10-14 (Reto): frontier-model sampled
  acceptance per batch; only failed batches and escalations reach Reto.
  Recommendation for the sampler tier: Opus, the same reviewer the
  gardener rulings use, so failure classes are comparable across both.
- **Batch unit (new, recommendation):** the run token across papers,
  not the paper (Wilson finding 1). Default taken until Reto says
  otherwise: trial and filler runs share one token per window.
- **Promotion scope (new, recommendation):** within-paper labels resolve
  to existing material/structure refs only; new materials go through a
  ledger proposal; cross-paper identity stays with the gardener's merge
  path.
- The 4th pilot paper's effect on item 5 is resolved (it widened it);
  the history is in git.

## Descriptor graph over a paper corpus, and review-grade synthesis on top

_Grouped 2026-09-26; was `descriptor-graph-for-review-synthesis`, status draft, prio normal._

### Motivation / why

Reto, 2026-09-20, on the NO→NH3 / H2 corpus behind `norr-her-survey`
(~599 papers, his figure — re-measure before scoping):

> Synthesize the 599 papers into a review of 50 pages for Coordination
> Chemistry Reviews … by connecting experimental studies to computational
> studies to focus on mechanisms, and suitable descriptors (i.e. limiting
> potentials, adsorption energies, relevant barriers).
>
> It would be very difficult and take years of time to do that manually;
> we did this for N2 reduction since 2021, and this review paper is still
> in preparation.

That last line is the whole argument: a human team has had a comparable
review in preparation for five years. The experimental↔computational
comparison is not hard because any single comparison is hard — it is hard
because there are thousands of them, each needing a number pulled out of
a paper, normalized, and set beside a number from a different paper that
used a different word and a different unit for it.

The shape Reto wants is two layers, and he named the order:

> So, some mechanism for these queries, then generate these types of
> summaries.

**Layer 1 — the substrate.** One extraction pass over the corpus: key
value-and-number extraction, entity resolution ("what's this anyways"),
and a dedupe engine over competing terminologies and units. Then query
the graph and verify, rather than re-reading 40k papers per question.

> I kind of want a … numeric dynamic range core, akin to the
> characterizer, that I don't have to manage as a table (discover the key
> numbers, and … make them discoverable). So you can say "what other
> papers about \<thing\> have \<key variable\> near \<this value\>" and get a
> good answer without looking thru 40k papers each time. In a dynamic,
> unspecified way. Some graph problem I figure.

The load-bearing word is *discover*: the descriptor vocabulary is not
declared up front and maintained as a table — it emerges from the corpus
and is normalized after the fact.

**Layer 2 — the payoff.** With the substrate in place, the review is a
query plus prose: "the agent can make a detailed comparison between
experimental and computational research", and can then answer *any*
question over that corpus rather than the one review question it was
built for.

### Prior art in-tree — start here, don't restart

The `material` kind is most of layer 1's data model already, for a
different domain (`src/precis/handlers/material.py`):

- a slug **entity** row plus per-property **sourced value** rows in
  `material_values` (value + unit + `conditions` + `maturity` + source +
  originating chunk);
- a **property registry** with canonical units and a `proposed` / `core`
  tier — an unknown `property=` mints a `proposed` row when the call
  declares a canonical unit, while `core` properties are curated by
  migration only;
- the range-filter read that is "this kind's reason to exist":
  `search(kind='material', property=…, min=…, max=…, maturity=…)`.

That is the characterizer-like core, already sourced and already
tiered. The gaps between it and what this item wants:

1. **Keyed on materials**, not on whatever a paper is actually about — a
   catalyst system, a facet, a dopant site, a reaction.
2. **Hand-written values.** Nothing populates it from a corpus at scale.
3. **No unit conversion** — v1 is canonical-units-only and *rejects* a
   non-canonical unit. This item's premise is competing units, so
   conversion moves from deferred follow-on to prerequisite.
4. **Returns entities, not papers.** "Which papers report \<variable\>
   near \<value\>" is a different read than "which materials have
   \<property\> under \<bound\>".
5. **Continuous only.** Reto wants discrete descriptors captured
   alongside continuous ones (facet, adsorption site, mechanism class).

`component` shares the same provenance rule ("identical to `material`'s"
— `precis-component-help` §"Sourcing a value"): a value carries a paper
or datasheet ref, optionally with `chunk=` pointing at the exact span, or
a bare URL, or nothing while `maturity='speculative'` stays honest. So
the sourcing grammar this item needs is already settled across two kinds
— reuse it rather than inventing a third.

Also relevant: `finding` hubs already carry one claim per sentence with
their own evidence, and `norr-her-survey` is now structured as ~1729
`item` chunks each holding a cited claim — a ready-made extraction
target that did not exist when this was first discussed.

### Problem summary — what is actually hard

Reto, 2026-09-20: "We may need a taxonomy of paper measurements (bond
length [m], and then subdivided for conditions). A dag or a tree or
something. It'll be complicated."

**The error to avoid: this is not one taxonomy.** Subdividing a taxonomy
by condition values is combinatorial — a node per (pH × temperature ×
additive × coverage) combination is thousands of leaves, and one paper
introducing a new additive re-partitions the tree. Taxonomies subdivide;
measurements *parameterize*. Stop subdividing where conditions begin.

**Five orthogonal axes, currently conflated:**

1. **Dimension** — bond length is a length [m]. Small, closed, universal.
   Not a tree: dimensional analysis is a free abelian group over the SI
   base dimensions (m·s⁻² is a product, not a child). Cheap; steal it.
2. **Measurand** — "bond length" is not "a length", it is the length of
   bond X in structure Y. Adsorption energy needs a species, a site, and
   a **reference state**; a barrier needs initial and final states. A
   measurand takes *arguments*, and arguments are entities. Taxonomy
   breaks here; a relation is required.
3. **Subject** — the system: catalyst, facet, dopant site.
4. **Qualifiers** — pH, T, additive, coverage, potential. Open-ended.
5. **Method** — DFT functional / basis / cell, or experimental technique
   / instrument. *Not* a physical condition: PBE is not a state of the
   world, it is a property of the observation. It behaves identically for
   comparability, and it is the axis the experimental↔computational join
   runs along.

**Where a DAG earns its place — and it is not measurements.** Three small
subsumption hierarchies used for **query widening** only: entities
(Cu(111) ⊂ Cu surface ⊂ metal surface, so "papers about copper" matches
Cu(111)), methods (PBE ⊂ GGA ⊂ DFT), species. Real is-a relations with
one job: letting a query generalize. Measurements stay flat tuples with
typed slots.

**The hardest part: value identity is not value equality.** In this
domain specifically:

- A potential vs RHE and vs SHE differ by 0.059 × pH. The qualifier is
  therefore not metadata *about* the number — it is an **input to
  computing** the number. Any design with conditions as a side-bag dies
  on this one fact.
- Adsorption energies vary by sign convention, ΔE vs ΔG, per-atom vs
  per-mole.
- Limiting potential and overpotential are different measurands reported
  under overlapping words.

Two papers reporting "U = 1.2" may be reporting different quantities, and
no amount of unit normalization catches it. Extraction must capture
reference state and convention or the corpus silently mixes incompatible
numbers — the failure mode that yields a confident, wrong review.

**Ranked by what decides feasibility:**

1. Measurand identity (references, conventions, sign).
2. Sign-off economics — the qualifier-delta idea below is the lever, and
   it is unproven.
3. Unrecorded qualifiers — invisible by construction; representable only
   as "N axes unrecorded".
4. Open-vocabulary discovery + dedupe — tractable; the `proposed`/`core`
   tiering already fits.
5. Dimensions and units — nearly free.

**Cheapest next move, and it is not a design argument.** Take ~20 papers
from the corpus, extract by hand, and count the distinct (measurand,
reference-state, convention) triples that fall out of what you would have
called *one* descriptor. That number separates a 3-month build from a
2-year one, and it costs a day.

### Qualifications — the modelling crux

Reto, 2026-09-20:

> There's also qualifications — ie there are different, say, voltages
> under different conditions, so the graph is genuinely a bit more
> complex. We need to figure out how to DRY model it.

A descriptor is rarely a scalar. A limiting potential is a limiting
potential *at pH 13, in 1 M KOH, on Cu(111), at 298 K*. The same system
yields a different number under different qualifiers, and a review that
sets two numbers side by side without their qualifiers is comparing
nothing.

**What the existing kinds do, and why it does not carry over.** Both
`material_values` (migration 0092) and the component spec table (0093)
store `conditions jsonb NOT NULL DEFAULT '{}'`, rendered for display by
`_fmt_conditions` as `k=v` strings. So the *result* quantity gets the
full treatment — registry entry, canonical unit, `proposed`/`core` tier,
range-filter search — and the *qualifier* quantities get none of it: an
untyped bag, no units, no registry, not queryable. That asymmetry is
survivable for a handbook lookup. It is fatal here, because every
interesting query in this item is a query *across* qualifiers.

**The candidate DRY model: a qualifier is a descriptor too.** pH is a
dimensionless quantity; electrolyte concentration is mol/L; temperature
is K; facet is discrete. Each is exactly the thing the property registry
already knows how to hold. So do not build a second system for
conditions — make a measurement a point in a space of named quantities,
where `property` is simply the *distinguished axis* of that point. Which
quantity is the result and which are the qualifiers becomes a property of
the **query**, not of the stored row. "Limiting potential vs pH" and "pH
at which limiting potential crosses X" are then the same data read two
ways, and the registry, the canonical units and the tiering are written
once.

This is also what makes the item's headline query honest: "papers with
\<key variable\> near \<this value\>" is only meaningful as "…near this
value *under comparable conditions*", and comparability is computable
only if qualifiers are typed quantities. As strings, "pH 13" and "1 M
KOH" never match; as quantities, they can be reasoned about. It is the
same point for the experimental↔computational join this whole item
exists to serve — a computed limiting potential is at 0 K with no
electrolyte on a named facet, a measured one is at 298 K in an
electrolyte. Unmodelled, that comparison is silently apples-to-oranges,
which is precisely the class of error that makes such reviews take years
and then get argued over.

**One value per system is the wrong shape, and the tree already says
so.** Reto, 2026-09-20:

> U=1.2 under condition A and U=.9 under condition B, and the conditions
> can be somewhat complex (ph, temp, … additives, who knows). And we want
> to kind of know what apples we are comparing. So that extraction is a
> bit tricky. It can be done tho.

Migration 0092 settled the storage half of this for `material_values`
already: "Multiple rows per `(material, property)` is a **feature** — the
handbook shows the spread across sources/conditions; **nobody picks a
canonical number at write time**." That principle transfers unchanged and
should not be relitigated. The same migration reserves `input_unit`
(always NULL in v1, "the unit-conversion follow-on's write column") — the
hook for keeping the number *as the paper wrote it* next to its
normalized form, which is part of knowing which apples you have.

**The condition space is open, so comparability must be reported, not
assumed.** Reto's "who knows" is the hard constraint: you cannot
enumerate the qualifier vocabulary up front, so it has to be discoverable
on the same `proposed`/`core` footing as descriptors — a qualifier the
corpus turns out to use mints `proposed` and gets curated later.

The consequence for reads: a comparison should carry its **qualifier
delta** as a first-class part of the answer, not return two numbers and
leave the reader to assume they match. "U = 1.2 V vs 0.9 V; same
catalyst and facet; differs in pH (13 vs 1); additive unrecorded in the
second" is the useful answer. "1.2 vs 0.9" is a trap.

And because the space is open, the strongest honest claim is never "all
else equal" but "all *recorded* else equal, with N axes unrecorded in one
or both sources". An unmentioned additive is invisible to extraction by
construction, so the epistemic hedge belongs in the output rather than in
a caveat someone remembers.

This is also the lever on the sign-off problem below: if every comparison
ships its qualifier delta, human review becomes checking a short diff
rather than re-reading two papers — which is a very different cost per
validation, and the thing that might make thousands of them tractable.

**The cost, stated up front.** Typing qualifiers is more expensive at
extraction time, and papers under-report them constantly. So a third
state is load-bearing: a qualifier that is *absent* is not the same as a
qualifier that is *known to be irrelevant*. Collapse those two and the
dedupe engine will merge measurements that must not merge — and that
merge is invisible in the output, which makes it the worst available
failure mode. Whether unknown-qualifier values are stored-but-unmergeable
or quarantined is undecided.

### In scope (when this graduates from draft)

Layer 1 first and alone. Layer 2 is the acceptance test for layer 1, not
a parallel workstream.

### Explicitly NOT in scope

- Writing the Coordination Chemistry Reviews paper. That is the
  demonstration, and it needs Reto as author, not the backlog.
- Replacing `material`. If the generalized store subsumes it, that is a
  later migration with its own item.
- Mechanism classification as a *required* field — see open questions.

### Acceptance criteria

Load-bearing and deliberately query-shaped:

- "What other papers about \<thing\> have \<key variable\> near \<this
  value\>" returns a correct, ranked answer over the corpus without a
  per-question full-corpus read.
- A descriptor that appears in the corpus under two terminologies and two
  units resolves to one queryable variable, with both source spellings
  recoverable.
- An experimental value and a computational value for the same descriptor
  on the same system can be set side by side, with both provenances
  intact — that comparison being the thing that takes humans years.
- Both a discrete and a continuous descriptor survive the round trip.

### Target + blast radius

New extraction pass (worker), a generalized descriptor/value store
adjacent to or generalizing `material_values` + the property registry, a
normalization/dedupe engine over terminology and units, and a query
surface. Touches the paper corpus read path and whatever kind ends up
owning descriptors. Large; slice it at `ready` time.

### Open questions / decisions log

Reto's own, recorded unresolved — **he is deliberately sitting on this**
("I'll sit on this for a bit"), so it stays `draft` until he picks it up:

- **Mechanism is not always stated.** "Mechanism is another thing, but it
  is not always stated I guess." Is mechanism a first-class descriptor
  that is often null, an inference the agent makes and flags as inferred,
  or out of scope for v1?
  **Recommendation (2026-10-21):** an inference, flagged. Mechanism is
  a categorical measurand (`value_form='categorical'`, shipped) written
  only when the paper states it, with `measurand_status='interpreted'`
  when the reader infers it; never required, so a missing mechanism
  never blocks a row.
- **Sign-off is the hard part, and it does not shrink with accuracy.**
  "We can get pretty good (95+% accuracy, I think, but then someone still
  needs to … look at the thing itself. It's 'right there' but still,
  that'll be thousands of validations." 95% of ~thousands of extractions
  still leaves hundreds wrong *and* every one needs a human to confirm it
  is not in the 5%. **[decided 2026-10-14, Reto: "get started, sampled
  sign off is good"] — extracted values are signed off by SAMPLED
  acceptance.** A frontier model reviews a stratified sample per paper ×
  measurand; the batch is accepted when the sampled error rate is under
  the bar (start 5 %, with a Wilson interval), and only escalations
  reach Reto. Per-value human sign-off is retired. The rejected
  alternatives were per-value review (thousands of decisions, the
  cost that motivated the question) and per-descriptor review (a bad
  normalization poisons a whole variable). The sample design — how
  many rows per (paper, measurand) stratum, and which failures count
  as errors — is a build decision of the extraction slice in
  measures-substrate.md §"Next slice".
  As built, the stratum is the measurand within one paper and that makes
  per-paper batches unpassable; see Wilson finding 1 at the top.
- **How much of `material` to generalize vs. build beside.** Settled by
  what shipped: `material_values` became `measures` (0187, folded with
  `component_spec_values`, Reto 2026-09-30). Generalize, done.
- **Is the qualifier-as-descriptor model right?** Shipped in effect:
  a condition is an input `measures` row on the same run, keyed by a
  measurand taxon with its own unit (0187 `direction`/`role`/`run_key`).
  **Recommendation:** keep it; the typed-schema alternative would need a
  second registry. Proof is due when the numeric mesh (gardener-value
  slice 5 (b)) compares conditions as quantities on real rows.
- **Unknown vs. irrelevant qualifiers.** **Recommendation:** relevance
  is declared on the measurand, not per row. `required_conditions` on
  the taxon names what matters; a row missing one is *unknown* (it
  escalates and is never merged or compared); a condition the taxon
  does not require is irrelevant by declaration. No per-row
  "irrelevant" marker.
- Is the ~599 figure current? Still open; a one-query check (papers
  serving `norr-her-survey`) before scoping layer 1.
