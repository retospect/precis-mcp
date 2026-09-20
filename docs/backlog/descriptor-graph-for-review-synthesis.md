---
status: draft
title: Descriptor graph over a paper corpus — extract numbers once, query them, then synthesize review-grade comparisons
prio: normal
---

# Descriptor graph over a paper corpus, and review-grade synthesis on top

## Motivation / why

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

## Prior art in-tree — start here, don't restart

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

## Problem summary — what is actually hard

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

## Qualifications — the modelling crux

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

## In scope (when this graduates from draft)

Layer 1 first and alone. Layer 2 is the acceptance test for layer 1, not
a parallel workstream.

## Explicitly NOT in scope

- Writing the Coordination Chemistry Reviews paper. That is the
  demonstration, and it needs Reto as author, not the backlog.
- Replacing `material`. If the generalized store subsumes it, that is a
  later migration with its own item.
- Mechanism classification as a *required* field — see open questions.

## Acceptance criteria

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

## Target + blast radius

New extraction pass (worker), a generalized descriptor/value store
adjacent to or generalizing `material_values` + the property registry, a
normalization/dedupe engine over terminology and units, and a query
surface. Touches the paper corpus read path and whatever kind ends up
owning descriptors. Large; slice it at `ready` time.

## Open questions / decisions log

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
