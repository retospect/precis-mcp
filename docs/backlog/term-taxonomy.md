---
status: ready
title: Term taxonomy — one `taxon` ref kind holding measurands and subject classes as a multi-rooted specialises-DAG, replacing three parallel registry tables
pillar: memory-graph
prio: high
model: opus
---

# Term taxonomy — one node table for every describable class

## Motivation / why

The same registry shape exists in the schema three times. Migration 0092
built `material_properties` (24 rows in prod), 0093 built
`component_specs` (30) + `component_categories` (13), and 0157 built
`rxn_properties` (10) whose own `COMMENT ON TABLE` says it "mirrors
`material_properties`". Each has a text primary key, its own
`core`/`proposed` status, its own `canonical_unit`, and a `dimension`
column explicitly marked "descriptive, v1".

Because they are not `refs`, none can be linked, searched, embedded,
cited, or retired, and none can see the others — there is no way to say
that a `material_properties` row and a `component_specs` row mean the
same quantity. Every affordance the rest of the corpus gets free from
`refs` + `chunks` + `links` was skipped per registry.

**Consumer status, stated honestly.** No shipped spec asks for a taxon
node today: `bootstrap-roadmap-quest.md` decided to reuse
`rubric_objectives {key, sense}` with no new registry,
`knowledge-mesh.md` puts a measurand ontology out of scope and consumes a
`measurands` *table*, and `norr-her-meta.md` fixes a closed list. The
demand is a **2026-09-28 decision by Reto with `paper-extraction-15`**
that supersedes those: the registry becomes nodes, and
`measures-substrate.md` consumes it rather than building a `measurands`
table. Those three specs are updated by that item, not this one.

This ships **before** `measures-substrate.md`. The alternative order
creates a `measurands` table both trees already know is the wrong shape
and migrates it later. The window is open because the data hostage
situation is small — a 2026-09-28 prod recount found `rxn_values` at 0
rows, `material_values` at 8, `component_spec_values` at 68 — and it
closes as extraction starts landing values.

**Why Bootstrap cares** (priority rationale; nothing here is in scope).
The roadmap DAG minted 2026-09-27 (root qu453863) already carries ~20
`rubric_objectives` keys — `air_stable_hours`,
`operating_stability_hours`, `max_feature_nm` and so on — as bare
strings with no definition, dimension or dedup; whether the first two
name one axis is currently unanswerable. Typed axes also make a rung's
`produces` matchable against the next rung's `consumes`, which is the
Bootstrap premise and today a model judgement. **This item is not on
Bootstrap's critical path and must not block it**: the roadmap tick body
ships against free-text keys and gains typed axes later.

## Design

**A node is an identity, a definition, a position, and — on roots only —
a required-key contract.**

*Identity* is the `ref_id`, `kind='taxon'`, handle code **`tn`** (`tx`
is taken by `tex`, `handle_registry.py`). `title` is the print label.
Slug and aliases are mutable, exist only for resolution, need not be
globally unique, and are not identity; citation uses the ref handle. So
rename, merge and rebranch are not address changes.

*Body follows the `concept` pattern exactly* (migration 0063): text is
`"<name> — <definition>"`, the definition lands in `meta.definition`,
and the only chunk is the reused `card_combined` at `ord -1` so the node
is a vector in the corpus manifold. **There is no `ord 0` body chunk** —
concept has none, and inventing one here would diverge from the pattern
this claims to reuse. The **lede** is the definition's first sentence
and must state the whole thing alone (`knowledge-mesh.md` §5's rule);
there is no separate jingle field — that shape is unbuilt.

*Position* reuses the **existing `generalises` / `specialises`** inverse
pair (`store/types.py`, seeded in 0001) rather than minting `is-a`: `Y
specialises X` already means what `is-a` would, and reuse avoids a
relation-registry edit entirely. Each edge may carry an optional
`meta.axis` free string (`composition`, `method`, `termination`, …).
**`axis` is unvalidated in v1** — the vocabulary is not fixed, and
validating an unfixed vocabulary would block the seed.

*Contract* is `meta.contract.required_keys` on start nodes only,
inherited by every descendant. A **start node** is marked
`meta.start = true`. The seed mints exactly two: `measurand` (requires
`dimension_kind`) and `subject` (requires nothing). Integrity check:
every node reaches **≥1** start node through `specialises` — never
"exactly one", since a doped-carbon support is legitimately both a
subject and a material class.

*Node meta keys* are fixed, because the seed maps onto them:
`definition`, `aliases[]`, `status`, `start`, `contract`,
`dimension_kind`, `si_vector`, `canonical_unit`, `value_type`,
`allowed_values`, `standard_ref`, `higher_is_better`, `legacy_source`
(table + key it came from), and `applies_to_ref` (where
`component_specs.category_id` lands — a meta pointer to the seeded
subject node, **not** a `specialises` edge, which would be semantically
wrong; promoting it to a real relation is deferred).

*Dimension* is `dimension_kind` — `si | currency | count | dimensionless
| scale | categorical`, **nullable** — plus, for `si` only, `si_vector`:
the seven SI exponents as a canonical string (`"0,0,-1,0,0,0,0"`), so
comparability is a string equality test. `scale` covers non-convertible
scales (prod has `hardness (non-convertible scale)`); `categorical`
covers non-quantity `value_type`s; currency needs a code and a price
base year and so cannot be an SI vector at all. NULL means the legacy
label could not be mapped.

*Traversal is a recursive CTE*, no materialised view, no closure table,
no index. At 10²–10³ class nodes with depth under ten this is
milliseconds, and the precedent exists (`_refs_ops.py` recursive walks).
Cycle prevention is a pre-insert ancestor check. **Not `ltree`** (tree
only; cannot hold multiple parents).

*Dedup on put* is a **suggestion**, not a merge: lexical match, then
embedding nearest-neighbour above cutoff, with **dimension mismatch as a
hard gate** that suppresses the suggestion entirely. The put is refused
with the candidate named and a `dedup=False` bypass, mirroring
`put_hub`. Merge-as-redirect is deferred.

## In scope

1. **Migration (forward-only).** `taxon` kind + handle code `tn`. One
   relation-registry addition: `instance-of` / `has-instance` (ref → taxon
   membership; see §Research-mesh reconciliation). Hierarchy reuses
   `generalises`/`specialises`. No index, no materialised view.
2. **`TaxonHandler`** (`handlers/taxon.py`) — put/get/edit/link/search on
   the `concept` numeric-ref pattern (`emits_card=True`), dedup
   suggestion on put, required-key check on put and on link.
3. **Traversal helpers** (`store/_taxon_ops.py`) — ancestors,
   descendants, path-by-axis, `under=` membership, cycle check,
   reaches-≥1-start-node integrity check. Recursive CTE throughout.
4. **`view='path'`** plus facets `under=` / `axis=` / `depth=` on
   `search(kind='taxon')`. `depth=N` means **≤ N**.
5. **Seed migration** — `INSERT … SELECT` over the four legacy tables
   with a label→(`dimension_kind`, `si_vector`) lookup defined in the
   migration. Unmapped labels land NULL and are listed by a
   post-migration report; the migration does not fail on them. **Both
   legacy statuses seed `proposed`** — `core` was a curation marker, not
   a usage measurement, and carrying it over as `systematic` would
   assert a test that was never run. Rows are **not**
   merged across registries even when they look like the same quantity
   (`max_service_temperature` vs `temperature_max`) — dedup is a later
   curation pass, so the node count equals the legacy row count.
6. **Runtime docs** — `precis-taxon-help`; `precis-overview` kind table.
7. **Edge axis as a taxon (v1.5, ships after 1–6 land).** The seed mints a
   third start node `axis` with six children (`method`, `material`,
   `system`, `regime`, `quantity`, `scale` — Reto 2026-09-30, decisions
   log); `link(rel='specialises', meta={axis: X})` then
   requires `X` to resolve to a taxon slug under it, and link-side filters
   accept `axis=<slug>` widened by `under=`. Until this item ships, `axis`
   is the unvalidated free string of the Design section. This is the
   "hierarchical link tags" half of the 2026-09-29 mesh handoff.

## Explicitly NOT in scope

- **Any change to `measures` / `material_values` / `component_spec_values`
  / `rxn_values`.** This ships the node graph alone; the legacy tables
  are *read* by the seed and left standing. `measures-substrate.md` owns
  every value-table change and retires them.
- **`status` promotion machinery** — the column ships and the seed sets
  it, but there is no promotion pass, no demote verb and no never-merge
  pin here. The N≥3 usage rule belongs to the extraction tree.
  **Nothing is grandfathered**: every seeded node lands `proposed` and
  earns `systematic` only by meeting the usage test. See the decisions
  log for why the compat-view objection to this does not hold.
- Merge-as-redirect, the dedup judge, alias repointing.
- Endpoint-kind rules for relations in general (`role_spec`-style
  src/dst kind lists). The only new check is the taxon-endpoint rule on
  `specialises` (§Research-mesh reconciliation).
- Truth, validity and polysemy annotations, and the term-health read.
  Those are `finding` rows through the existing verdict/dispute path and
  need no schema here.
- Contract ranges, allowed-value narrowing, and multiple-inheritance
  union/intersection resolution. v1 is required-keys only.
- **Widening the eye ring set.** `render_eye` routes unknown kinds to
  `_render_note_eye`, whose `+1hop` follows `RING_RELATIONS` (semantic ∪
  claim) — `specialises` is not in it, so a taxon fisheye shows the node
  with an empty neighbourhood. Navigation in v1 is `view='path'`, not
  the eye ladder. Adding a `TAXON_RELATIONS` ring is a later item.
- Unit conversion, `reference_states`, `convert`.
- `view='tree'` and `view='siblings'` — siblings are
  `search(under=<parent>, depth=1)`, and a DAG tree view needs a
  fan-out cap policy this item does not define.
- Caching a rendered path into the card (reparent/rename would have to
  re-emit every descendant's card; the cascade is unsolved).

## Acceptance criteria

1. `put(kind='taxon', text='turnover frequency — the number of catalytic
   cycles per active site per unit time')` mints `status='proposed'`,
   sets `meta.definition`, emits the `ord -1` card, and the node is
   returned by `search(kind='taxon', q='how fast the catalyst cycles')`
   on definition similarity alone.
2. A near-duplicate put with the **same** `dimension_kind`/`si_vector` is
   refused, naming the existing node; `dedup=False` mints anyway. A put
   with the same name but a **different** `si_vector` is never offered as
   a match (the hard gate) and mints without a bypass.
3. `link(rel='specialises', meta={axis:'composition'})` that would create
   a cycle is refused, naming both endpoints; the same link with a
   non-taxon endpoint is refused naming the endpoint's kind.
4. A node whose reachable start node requires `dimension_kind` cannot be
   put without one; the refusal names the start node that requires it.
5. A node with parents on two axes returns two chains from
   `view='path'`, each rendering the lede of every hop. A node reaching
   no start node is listed by the integrity check.
6. `search(kind='taxon', under='taxon:<root>', axis='method', depth=2)`
   returns exactly the axis-restricted descendants at depth ≤ 2. (The
   unknown-facet refusal is already enforced by `dispatch.py`'s
   strictness gate for declared parameters — keep a test, but this is
   not new work.)
7. The seed lands **one taxon per row of the four legacy tables** (count
   equality, asserted against the live counts at migration time, not a
   hardcoded 77) plus the two named start nodes; every legacy-derived
   node reaches a start node; **every seeded node is `proposed`** (no
   `core`, and no `systematic` — status is earned post-seed); the 14
   legacy rows with a NULL `canonical_unit` seed with
   `dimension_kind` in (`categorical`, `dimensionless`) and are not
   refused for the missing unit; every node with
   `dimension_kind='si'` has a 7-slot `si_vector`; unmapped dimension
   labels are NULL and appear in the report.

## Target + blast radius

New: `handlers/taxon.py`, `store/_taxon_ops.py`, two migrations (kind +
handle code; seed), `precis-taxon-help`. Touched: the kind registry,
`handle_registry.py`, `precis-overview`.

`extent=` is **not** base-provided on `get` — `FindingHandler` wires it
through the `view=` label, and `TaxonHandler` must do the same if it
wants one at all. v1 does not.

Read-only against `material_properties` / `component_specs` /
`component_categories` / `rxn_properties`. **No existing handler or
value table changes**, so `tests/test_material*`, `tests/test_component*`
and `tests/test_rxn*` must stay green untouched — that is the
blast-radius check.

Pre-migration: re-count all five legacy tables with `scripts/prod-psql`.
The 2026-09-28 snapshot (24 / 30 / 13 / 10; values 8 / 68 / 0) is a
snapshot, not a substitute — and note the migrations seed fewer rows
than prod holds (0092 seeds 19 of 24; 0093 seeds 10 of 13), so the seed
must be `INSERT … SELECT` over live rows, never a hand-written list.

## Research-mesh handoff (2026-09-29) — reconciliation

A design handoff for an "LLM-curated research mesh" arrived 2026-09-29
(node/edge/role tables, path-addressed hierarchical tags, insert-time
constraints, a closure table, a writer→reviewer loop). Reto's framing of
the ask: existing refs get **wrapped** as mesh nodes, and link tags become
**hierarchical**. Read against this item, `knowledge-mesh.md`,
`graph-gardener.md` and `measures-substrate.md`, the handoff is not a new
store — it is this item plus three rulings. Each handoff point, what
already exists, and the ruling:

| handoff | exists | ruling |
|---|---|---|
| a `node` table for everything; kinds and roles are nodes | `refs` is that table (`refs.kind` FK → `kinds`); relations are the `relations` registry (`store/types.py::Relation`), extensible by migration | keep both registries. ~40 relations paste into a prompt, which is the handoff's own reason for keeping the role set small. Handoff Q1: `kind` stays a column. |
| tags are nodes; a bare tag is a node with no body | `tags` is flat `(namespace, value)` + `tag_embeddings`; the `tag` kind is read-only discovery (`handlers/tag.py`); `concept` and `taxon` are ref nodes with a card | **the "wrapped node"** is a `taxon` ref. The flat `tags` table stays as the cheap membership index for closed axes; anything that needs a body, a definition or a position is a taxon. |
| `tagged` membership role | no ref → taxon edge; this item had deferred `instance-of` to `measures-substrate` | **`instance-of` / `has-instance` moves into v1** (in-scope 1). Any existing ref of any kind joins the mesh by `link(rel='instance-of', target='taxon:…')` — no re-kinding, no new table. Reto confirmed this reading 2026-09-29. |
| edge = one role + free tags; traversal "from an angle" filters on an edge tag | `links.relation` + free `links.meta`; here `meta.axis` is an unvalidated string | **hierarchical link tags, staged**: v1 unchanged; v1.5 = in-scope 7 (`axis` start node, `meta.axis` resolves to a taxon under it, `under=` widening on link filters). Other `meta` keys stay free. |
| roles carry `proposed`/`core`; a recurring edge tag on one role is promoted to a role | nothing | no `proposed` relations as rows. Promotion is `graph-gardener.md`'s vocabulary-consolidation job and lands as a migration-proposal `todo` (`waiting-for:reto`), because the registry changes only by migration. |
| `specialises` is acyclic; cycle rejected at insert; endpoints any → same kind | decided above (pre-insert ancestor check) | same, plus the endpoint rule: `TaxonHandler` refuses `specialises` unless both ends are `taxon`. Other kinds' use of the relation is untouched. |
| materialised `closure` table | rejected above (recursive CTE, `_refs_ops.py` precedent) | stays rejected. Revisit trigger: > 10⁴ taxon nodes, or `view='path'` p95 > 50 ms on prod. |
| `primary_parent` + stored `canonical_path` | rejected above (reparent cascade unsolved) | stays rejected. Handoff Q2: no stored primary; the display path is the shortest chain to a start node, ties by axis name, computed on read. |
| slugs sibling-unique; `resolve(path)` | slugs non-unique, resolution-only; identity is `ref_id` | path-form ids on `get` (decisions log, 2026-09-29); ambiguity ⇒ refuse with candidates. |
| `/x/**` and `/x/*` wildcards | `under=` / `depth=` facets | `/x/**` = `under=x`; `/x/*` = `under=x, depth=1`. No glob parser. |
| per-role endpoint kind lists (`role_spec.src_kinds`) | only `guard_and_route_contradicts_disputes` | not generalised in v1; the taxon-endpoint rule above is the only new check. |
| `provenance in ('machine','human')` required on every node and edge; `session_id` | `refs.set_by` / `chunks.set_by` exist but are NULL on insert (schema comment); `agentlog` + `touched` links are the per-run write set | → `curation-gate.md` (populating `set_by`, protecting human content, the reviewer loop). The handoff's `session_id` is `agentlog_id`. |
| `status proposed/core/deprecated` | `proposed` → `systematic` earned by `taxonomy-bootstrap.md`; `retired_at` | keep. |
| one claim per node; split a sprawling note | `knowledge-mesh.md` §5 lede-first lint | no change here. |
| traversal grammar `role[:tag-path]`, `role!` (handoff Q3) | `knowledge-mesh.md` §2 eye ladder, no walk verb | ruled there: filter arguments on the `+1hop`/`+2hop` rungs. |
| qualifiers on findings so "same number" ≠ "same quantity" (handoff Q5) | `measures-substrate.md` (conditions, reference, direction; identity = taxon + reference + convention) | no change. |

Net new work from the handoff, all recorded above: in-scope 1 gains
`instance-of`; in-scope 7 (axis as taxon) is new; the taxon-endpoint rule on
`specialises` joins AC 3; the ambiguous-slug question is closed; two Reto
rows joined the decisions log. Everything else in the handoff either
already exists or was rejected here before it arrived.

## Design-notes handoff (2026-09-30) — reconciliation

Reto's "sourced knowledge graphs for materials, molecules and papers"
design notes (developed with another assistant) were transferred at the
2026-09-30 product-plan review. Read against this item,
`measures-substrate.md` and `knowledge-mesh.md`, they are this graph plus
four additions, which now live in `class-lattice-similarity-spaces-and-laws.md`
(blocked-by this item). The notes' copy in that assistant's memory is
retired; this table and that item are the record.

| design note | exists | ruling |
|---|---|---|
| `hierarchy_edge` strict is-a, one hierarchy per slot type (species, materials, process types, roles, measurand types, reaction types) | `specialises` with `meta.axis`; in-scope 7 (axis as taxon) | same; the hierarchy name *is* the axis. Widening never crosses an axis. |
| `closure` table, rebuilt by recursive CTE, versioned | rejected above (recursive CTE at 10²–10³ nodes) | stays rejected; the revisit trigger stands. |
| `node_ic` information content for Resnik similarity | nothing | new — `class-lattice-…` §2. |
| `class` = defined / primitive / facet value with canonical `constraint_set` + `constraint_hash`; `membership` yes/no/unknown; `pooled_estimate` | primitive nodes only | **new, decided 2026-09-30 (Reto): v2 of this item**, shared by scenario classes and se pocket specs — `class-lattice-…` §1. |
| `relation(subj, predicate, obj, source, confidence, qualifiers)` for paper-extracted classification claims, separate from the curated backbone | `links` + `finding` rows + `instance-of` (in-scope 1) | no separate table; an extracted classification is an `instance-of` link whose evidence is a finding hub, same as any claim. |
| `predicate` registry with symmetric/transitive/inverse_of/domain/range | `relations` registry (`store/types.py`), inverse pairs seeded | same; domain/range = the taxon-endpoint rule (AC 3). |
| `scenario` + `participation` roles; electrolyte composition-first | `experiment` kind + input measures (`measures-substrate.md` §3); no participant roles | roles are new — `class-lattice-…` §3. |
| `measurement` five-axis tuple (dimension, measurand, subject, qualifiers, method), `origin` reported/refit/derived/imputed, `measurement_arg` | `measures` (`measures-substrate.md`), `tier`, `conditions` | same record; `origin` maps onto `tier` (decisions log there); `measurement_arg` = the `subject_selector` column `se-region-property-layer.md` adds. |
| `curve` / `fit` / `law`, refit over copy, rankings in comparability buckets | nothing | new — `class-lattice-…` §4. |
| per-axis similarity, one vector per (entity, named space), overlap count always returned | one embedding per chunk | new — `class-lattice-…` §2. |
| materials/molecules: prototype + occupancy, scaffold + substituents, MMP transforms, substitution tiers, reaction rules, routes | `structure`, `rxn`, `route` kinds; retrosynthesis shipped dark (`chem-tools-integration.md`) | `materials-molecular-substitution-db.md` (idea); its join to the paper graph is parked there. |

## Open questions / decisions log

- **[decided 2026-09-28, Reto]** Ships before `measures-substrate.md`,
  which gains `blocked-by: term-taxonomy` and whose registry section is
  rewritten by that item to consume taxon refs instead of building a
  `measurands` table. Rejected: folding the node model into
  measures-substrate; filing this blocked-by it.
- **[decided]** One `taxon` kind, not `measurand`/`experiment-type`/
  `instrument` kinds — the required-key machinery is identical across
  branches, so per-kind handlers would be three copies of the validation
  code this item exists to remove.
- **[decided]** Identity is `ref_id`; slug is resolution-only. Agreed
  with `paper-extraction-15` — their merge-as-redirect design already
  presumes a stable identity beneath the slug.
- **[decided]** Reuse `generalises`/`specialises` rather than mint
  `is-a`/`has-subtype`. Same semantics, no registry edit.
- **[decided 2026-09-28]** No grandfathering; every seeded node is
  `proposed`. The objection was that `core` rows are accepted without a
  `canonical_unit` while `proposed` rows are not (0092), so demoting
  would change material/component write behaviour through the compat
  views. A prod read disproved it. Only **14 rows** across all three
  registries have a NULL `canonical_unit` (9 of them `core`), and every
  one legitimately has none: `drive_type`, `finish`, `grade`,
  `head_form`, `point_type`, `thread_size`, `crystal_structure` are
  `categorical`; `is_reinforced`, `is_magnetic` are `boolean`;
  `drive_code`, `solvent` are `text`; `poissons_ratio`,
  `relative_permittivity`, `pss_short_fraction` are dimensionless by
  definition. So there is no data to repair — the stated rule is simply
  wrong for non-quantity `value_type`s, which is also why **3 of the 7
  `proposed` `material_properties` rows violate it today**. The
  contract therefore requires `dimension_kind`, never `canonical_unit`.
  (Also corrects an earlier draft's "45 core rows": the real count is
  **55** — 17 material + 28 component + 10 rxn.)
- **[noted]** `corpus-quantitative-extraction.md` argues a measurand
  "takes arguments" and that a taxonomy breaks there. Compatible: the
  classes live here, the arguments live on the measure row.
- **[decided 2026-09-29]** Ambiguous slug ⇒ refuse and list the
  candidates. Path-form ids (`get(kind='taxon',
  id='/measurand/faradaic-efficiency')`) walk slugs from the named start
  node, so a path is ambiguous only when two siblings share a slug — the
  refusal names both and the caller re-issues with the handle.
- **[cross-ref 2026-09-30]** The six axis names are also
  `taproot/sentence_lint.py::SCOPE_KEYS`; `scope-key-vocabulary-registry.md`
  must read the taxon nodes rather than keep a second list. The generic
  endpoint rule this item wanted for `instance-of` (range = taxon) lands
  as a row of `relation-constraints.md`, not as a bespoke guard.
- **[decided 2026-09-30, Reto]** The `meta.axis` vocabulary: **seed six,
  earn the rest.** The `axis` start node (in-scope 7) is seeded with
  `method`, `material`, `system`, `regime`, `quantity`, `scale` — the same
  names the finding `scope=` keys use, so findings and taxa share one
  vocabulary. Any other axis string is accepted in v1 as a free string and
  becomes a taxon only when the graph-gardener's promotion rule earns it
  (N uses across M distinct agentlog runs, `graph-gardener.md` "Vocabulary
  consolidation rules"). `composition`/`periodic` stay computed from the
  formula parser, not seeded as axes.
- **[decided 2026-09-30, Reto]** Element symbols: **one node, the taxon
  wins.** The composition axis mints one taxon per element; the
  `norr-her-meta` campaign's element-symbol `domain_classes` ids (`pd`,
  `cu`, …) are dropped from the campaign config and the tag reader resolves
  `catalyst:pd` to the element taxon. No `same-as` pairs, no second node.
  Lands in `taxonomy-bootstrap.md` before its first promotion run.
