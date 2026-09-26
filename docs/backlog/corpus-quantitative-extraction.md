---
status: draft
prio: normal
---

# Corpus quantitative extraction

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

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

**1. Version the anchor scheme (correctness — do this first).**
Anchor ids are positional: `c.n` means "the n-th sentence span of chunk c". They
are only meaningful under the splitter that produced them. Changing the splitter
re-points existing anchors at *different source text* while the source
fingerprint still matches, so nothing catches it. Measured on the three pilot
documents, a one-line splitter improvement silently re-pointed **35 of 222
anchors (16%)** to different text and hard-failed another 18. The pilot now
carries `ANCHOR_SCHEME`, emits it on maps and binding documents, and refuses to
materialize a document whose declared scheme differs — **or that declares none
at all**, since an unstamped document has no provenance for its anchors and
defaulting it to the current scheme would assume exactly what the guard checks.
(The first cut of this guard got that wrong and failed open on a missing field;
review caught it.) Port the guard, fail closed on absence, and treat the scheme
constant as append-only.

**2. Give conditions an explicit role (`operating` | `preparation` | `model`).**
Today the distinction survives only inside a free-text condition *name*, and the
"is a temperature visible?" guard is a keyword match over that name. Both
consequences were observed: one reader named a condition "post-laser heat
treatment", the guard saw no temperature and let it through; renaming it to
contain the word "temperature" made it fire. A second reader worked out that
attaching its 80 °C PET-hydrolysis pre-treatment would have *silently satisfied*
the guard and suppressed the true "no operating temperature stated" signal — so
it left a real condition unattached and filed a gap instead, losing information
to stay honest. The guard is unsound as built; a role field makes it decidable.

**3. Group-level condition sets with per-result exceptions.**
A condition item is (condition, basis, applicability-evidence), so giving every
result its own applicability basis is quadratic: 31 conditions became 224 items
on one paper, 46 became 146 on another. Both readers independently proposed the
same fix, and one noted the rule as written ("every result selects its own
conditions with its own applicability basis") is *not reachable* through a table
recipe at all. Inherit a condition set at group level, override per result, same
mechanism as `defaults.results`.

**4. Rank escalations; stop drowning the auditor.**
503 escalations over four papers, of which 312 (62%) are `inferred_condition_basis`
almost all saying "Methods states the electrolyte, the result does not restate
it" — ordinary paper structure, indistinguishable in the output from a genuinely
shaky inference. A further 25 are a single matcher bug (item 7) and 11 are the
unavoidable interval spans of item 5, so well over two thirds of what an auditor
would read is noise. Item 3 removes most of it structurally; the rest needs a
severity so a strong-model audit has an order to work in. A related blind spot:
where a condition is *defined and applied in the same clause* ("below 200 °C"),
its applicability evidence necessarily repeats its definition, and the
`applicability_not_shown_for_this_result` check cannot tell that apart from
genuinely unsupported applicability.

**5. Sub-sentence anchor granularity, and recipe per-row overrides.**
Two faces of one gap: the anchor vocabulary cannot address a fragment, so an
exact interval literal (`550–575`, `125~150`, `80~400`) can only be bound with
raw `[chunk, start, end]` offsets — which the `sub_sentence_span` check then
flags. All 11 offset spans and all 11 such flags in the round are this, and none
is avoidable without cropping the value. **The check currently penalises the only
correct encoding.** Add a fragment anchor (a numeric-region span that can cover a
range expression, or an explicit sub-sentence id) so intervals are anchorable.
Then, on recipes: a recipe fixes one field block per column, so cells that differ
mechanically leak back to hand-writing — six `<1` upper-bound cells had to be
lifted out purely to change `value_form`. Add per-row overrides, and let the
materializer set `value_form: upper_bound` when a cell literal begins `<`. A
`listed` recipe also cannot bind `9.6 ± 1.7`, because numeric anchors are single
atoms, so recipes work on un-replicated data and fail on replicated data, which
is backwards. Finally, a table recipe binds one unit per column, so a table that
carries its units in a *third column* materializes its results with
`reported_unit: null`.

**5b. `reported_value` is numeric-only, which silently drops categorical rows.**
A spec table whose rows are "Pd", "γ-Al2O3, Ce0.5Zr0.5O2", "Cordierite" cannot
produce results at all; one reader could only preserve them as prose inside an
assertion's limitations, which is not queryable. Either admit a categorical
value form or define where non-numeric sourced facts live — right now they are
lost by construction, quietly.

**6. A `contradicts` relation, and a way to say "stated but unattributed".**
Nine intra-paper conflicts were found across two papers (150 vs 200 mg;
`7381.1 ± 59.5` vs `± 594.7` for the same quantity; three incompatible
definitions of "activity"). All had to be flattened into prose `gaps` with no
link to the results they qualify. One reader also could not record that the
authors *themselves* declare their electrochemical results non-reproducible —
that is a source-stated reliability claim, not an uncertainty and not a gap in
our reading. A third case: two activation energies (95 and 77.6 kJ/mol) are
stated with no citation and no method, and the only available encoding is
`not_established` on all three of evidence_mode / value_generation /
source_attribution — which *understates* what is known, namely that the number
is asserted by this paper and unattributed. "Unattributed assertion" is a
distinct epistemic state from "we could not establish it".

**7. Small verified bugs.** `expansion_directory` is built from a hardcoded path
that ignores the map label (`bindings.py`). The sentence splitter breaks after
`ca.`, `Fig.`, `satd.`, inflating anchor counts and forcing runs — fix under a
new anchor scheme, never in place. `pilot.unit_has_text_support` false-negatives
a `%` unit when the source glues it to a word (`X% NO3`): its letter-lookaround
is right for `mA`, wrong for `%`. That one matcher produced all 25
`unit_without_text_support` escalations on one paper.

**8. Port to `src/precis/` with the right seams.** Map builder, materializer and
deterministic checks are pure functions over chunks — library code, no agent. The
reader call belongs in a worker pass alongside the existing synthesis passes. The
MCP surface is thin reads plus an approve path; the checks and materializer are
NOT MCP verbs.

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

1. A binding document written under anchor scheme N refuses to materialize under
   scheme N+1, **and a document declaring no scheme is refused too**, with a
   test that demonstrates the silent-repoint failure the guard prevents.
2. A condition carries an explicit role; a `preparation` or `model` temperature
   can be attached to a result without satisfying the operating-temperature
   check, and a result with no operating temperature still escalates.
3. A condition set declared at group level and inherited by N results produces
   N materialized results each carrying it, with a per-result override changing
   exactly one of them; item count is linear, not quadratic, in a test fixture.
4. Escalations carry a severity; a fixture where the only issue is "Methods
   states it, result does not restate it" ranks below one with a genuinely
   unsupported condition.
5. An interval literal (`550–575`) is bindable by anchor, materializes with the
   literal intact, and does NOT raise `sub_sentence_span`.
6. A table recipe expands a column containing both `12.3` and `<1` into results
   whose `value_form` differs, with no hand-written result; a table carrying its
   units in a separate column materializes with a non-null `reported_unit`.
6. Two conflicting literals from one paper can be bound to each other as a
   first-class relation and survive materialization.
7. `scripts/test` green; the three verified small bugs each have a regression
   test.

### Target + blast radius

New library module under `src/precis/` (map/materialize/check), a new worker
pass, a migration if extraction records become a kind. Touches the findings
path at integration. Nothing in this item writes to prod, signs or publishes.
The pilot code is the reference implementation, not the port: it lives in an
untracked directory and its data must never enter a shipping commit.

### Open questions / decisions log

- ~~**Does the 4th pilot paper change item 5?**~~ **Resolved — yes, it widened
  it.** That paper (thermal catalysis, 8 shaped tables, peaks at differing
  operating points) landed on the third attempt after two infrastructure stalls.
  It contributed all 11 character-offset spans in the round, all of them exact
  interval literals, which turned item 5 from "recipe ergonomics" into a missing
  anchor granularity plus the finding that `sub_sentence_span` penalises the only
  correct encoding. It also produced 5b (categorical rows), the units-in-a-third-
  column case, and the "stated but unattributed" gap in item 6. It confirmed
  item 2 from the other direction: it kept three real preparation temperatures
  (preheat, premix tank, heating belt) in a condition set referenced by **no**
  result — a deliberate orphan the format cannot mark as intentional.
- **Is a chunk-level extraction a new kind, or a finding subtype?** Reusing the
  existing findings infrastructure is an invariant; whether the intermediate
  record needs its own kind is undecided and gates the migration question.
- **Where does the reader pass run?** In-process on melchior like the other
  claude passes, or as a queued job. Affects cost and the escalation loop.
- **Escalation loop shape.** Who adjudicates the sampled audit — a strong model
  pass, a human queue row, or both in sequence.

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
- **Sign-off is the hard part, and it does not shrink with accuracy.**
  "We can get pretty good (95+% accuracy, I think, but then someone still
  needs to … look at the thing itself. It's 'right there' but still,
  that'll be thousands of validations." 95% of ~thousands of extractions
  still leaves hundreds wrong *and* every one needs a human to confirm it
  is not in the 5%. Open: does review happen per-value (thousands of
  decisions), per-descriptor-after-normalization (far fewer, but a bad
  normalization poisons a whole variable), or by sampling with a measured
  error bar? This question probably decides whether the whole thing is
  viable, and it is not primarily a technical question.
- **How much of `material` to generalize vs. build beside.** Not decided.
- **Is the qualifier-as-descriptor model right?** It is the DRY answer
  and it is unproven here. The alternative — a typed conditions schema
  per property — is more predictable and less general, and would not
  give the "query decides which axis is the result" behaviour. Reto's
  call; see the Qualifications section.
- **Unknown vs. irrelevant qualifiers.** Must be distinguishable before
  any dedupe runs; representation undecided.
- Is the ~599 figure current? Re-measure against the corpus before
  scoping.
