---
status: draft
pillar: memory-graph
title: class lattice, per-axis similarity spaces, participant roles, curves/fits/laws — term-taxonomy v2, from Reto's sourced-knowledge-graph design notes
prio: high
model: opus
blocked-by: term-taxonomy
---

# Class lattice, similarity spaces, roles, curves and laws (term-taxonomy v2)

## Motivation / why

Reto's design notes for "sourced knowledge graphs for materials, molecules
and papers" (developed with another assistant; transferred 2026-09-30 at
the product-plan review; **this file is now the source of truth and the
copy in that assistant's memory is retired** under the pillar-1
retirement rule in `docs/roadmap.md`) describe the same graph
`term-taxonomy.md`, `measures-substrate.md` and `knowledge-mesh.md`
specify, plus four things the repo lacks. The reconciliation table lives
in `term-taxonomy.md` §"Design-notes handoff (2026-09-30)"; this item owns
the four additions.

The property layer for designs (`se-region-property-layer.md`) needs the
first two on day one: a pocket spec is a constraint set, and pick-and-join
is "near on size, far on charge". The paper graph needs all four for the
20-paper hand-extraction test.

Rule carried over verbatim: **embeddings for recall, graph for precision,
and every claim must trace to a sourced edge.**

## In scope

**1. Defined classes.** A `class` is a taxon node whose definition is a
canonicalised constraint set, not prose: `meta.constraint_set` (jsonb,
canonical key order and unit normalisation), `meta.constraint_hash`
(unique; the identity for dedup), `kind ∈ {defined, primitive, facet
value}` in `meta.class_kind`. Primitive nodes are what `term-taxonomy.md`
v1 seeds. The lattice is generated from facets; **only strict `specialises`
edges are used for widening, inheritance and pooling** (no leaking across
axes — the edge's `meta.axis` is the hierarchy name). `membership` rows:
`(subject_ref, class, status yes|no|unknown, derived_from)` — a
three-valued fact, never a tag; `unknown` is the default and is a
statement. First subjects: experiments/scenarios (paper graph) and
se pocket specs' candidate groups (design). `pooled_estimate`:
`(class, parameter, model, generator_version, estimate,
between_group_variance, levels_climbed, n)` — a versioned cache rebuilt
when the lattice changes, never edited.

**2. Per-axis similarity spaces.** One vector row per `(entity, named
space)`: geometric · electronic · magnetic · thermal · mechanical ·
structural, each a standardised property vector over that subspace's
measurands (from `measures`), stored beside `chunk_embeddings` as
`entity_space_vectors(ref_id, space, vec, n_dims_present)`. Query: near on
space A, far on space B; **every result reports the overlap count** (how
many dims both entities had) so a near-by-absence never reads as near.
`node_ic` (information content from corpus frequency per taxon node) for
Resnik-weighted similarity over the lattice. Indexes: pgvector HNSW per
space; btree_gist for scalar KNN.

**3. Participant roles.** `participation(experiment_or_scenario_ref, role
taxon, entity_ref, amount, unit)` — roles (reactant, product, electrode,
catalyst, electrolyte, poison, support …) are taxon nodes under a `role`
root, bound to participants **not tags**. This is a different axis from
`measures.direction` (input/output/covariate) and from the condition
`role` enum (context/preparation/model) in `measures-substrate.md`; all
three coexist on one experiment. Electrolytes are modelled
composition-first: `electrolyte_composition` rows (species, concentration,
solvent, T, reported pH) with pH, ionic strength and dominant proton donor
**derived by speciation** into a versioned `electrolyte_derived` cache.
`scenario.class_hash` links a run to its defined class.

**4. Curves, fits and laws.** `curve(subject_ref, x_measurand, y_measurand,
points, source: SI | digitised figure)`; `fit(curve, model, params,
covariance, residuals, window, corrections)`; `law(x_measurand,
y_measurand, form, params, validity_scope: a class constraint set,
fit_stats, source, tier proposed|core)` — scaling relations, BEP, volcano
plots as **first-class sourced objects used for convention checking**
(a reported number outside every applicable law's band is flagged, never
rejected). Model parameters take the model as an argument (`j₀` is
`j₀(Butler–Volmer, …)`, not `j₀`); **prefer refitting extracted curves
over copying reported values** — a refit is a `measures` row with
`tier='computed'`, `derived_from` the curve, `origin` recorded per
`measures-substrate.md` (`reported` → `measured`, `refit` → `computed`,
`derived`, `imputed` → `derived` with generator provenance). Rankings run
**within comparability buckets** (same measurand, reference state,
convention) and report both best-reported and best-supported values.

**5. The hand-extraction test** (Reto's still-to-do, kept here so it is
not lost): 20 papers by hand, three slices — count (measurand, reference
state, convention) triples; count same-species-different-role cases;
compare reported `j₀` against refits. Its result decides how much of 3 and
4 ships in v2 versus later.

## Explicitly NOT in scope

- The primitive taxon node model, seeds, dedup-on-put, path ids —
  `term-taxonomy.md` v1.
- The `measures` table itself and the `experiment` kind —
  `measures-substrate.md`.
- The materials/molecular substitution database that consumes the
  similarity spaces (`materials-molecular-substitution-db.md`); how that
  database joins the paper graph is **parked for discussion** there.
- A closure table (rejected in `term-taxonomy.md` with a revisit trigger;
  the design notes' `closure` table is the same structure, and the
  trigger stands).
- Any UI beyond `view='members'` on a class node and `view='near'` on an
  entity.

## Acceptance criteria

1. Emitting the same constraint set twice yields one class node; a
   differently ordered but equal set hashes identically.
2. `membership` accepts only `yes|no|unknown`; a subject with no row reads
   as `unknown` in `view='members'`.
3. A widening query from a defined class walks `specialises` edges on one
   axis only (test: a node that specialises on two axes is not pooled
   across them).
4. `view='near'` on an entity with `space='geometric', far='magnetic'`
   returns candidates ordered by the combined score, each with both
   overlap counts; an entity missing the whole `magnetic` space is
   reported as `unknown`, not far.
5. A refit `j₀` row carries `derived_from` = the curve row and
   `method` = the model name; the reported value stays as its own
   `measured` row; both appear in one comparability bucket.
6. A law with a validity scope flags an out-of-band measure as a finding
   (`convention_check`), never rejects the write.
7. `pooled_estimate` rows carry `generator_version` and are rebuilt, not
   updated, when a class gains a member.

## Target + blast radius

`src/precis/handlers/taxon.py` (once `term-taxonomy` ships): defined
classes, `view='members'`; new store ops for `membership`, `participation`,
`curve`/`fit`/`law`, `entity_space_vectors`; migrations forward-only,
numbered against the prod ledger after `measures-substrate`; `precis-taxon`
and `precis-measure` skills; `knowledge-mesh.md` §2 gains a `near`/`far`
eye argument. Consumers: `se-region-property-layer.md` (emits classes),
`se-intent-to-realize-loop.md` (queries membership + near/far),
`norr-her-meta.md` (first paper-graph consumer), the catalysis quests
(`knowledge-mesh.md` §6).

## Open questions / decisions log

- **[decided 2026-09-30, Reto]** Defined classes go into term-taxonomy as
  a v2 section, shared by scenario classes and pocket specs.
- **[decided 2026-09-30, Reto]** The design notes' source of truth moves
  into the repo; the other assistant's copy is retired.
- **open** — `membership` as a table versus `instance-of` links with
  `meta.status`. Links reuse the registry and the eye ladder; a table gets
  the unique constraint for free. Lean: links, with a partial unique index
  on `(src, dst, relation)`.
- **open** — which subspaces are seeded for *designs* (se) versus
  *materials*; the six above are the materials set. Designs likely need
  `geometric` + `electronic` first.
- **open** — where `electrolyte_composition` lives: as `measures` rows
  with `direction='input'` (measures-substrate's rule that a set condition
  is a measure) or as its own table for the speciation solver. Lean:
  measures rows in, derived cache out.
