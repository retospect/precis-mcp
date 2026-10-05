---
status: draft
title: Term taxonomy v1.5 — edge axis as a taxon, plus the v1 follow-ups
pillar: memory-graph
prio: high
model: opus
---

# Term taxonomy v1.5 — the edge axis becomes a taxon

v1 shipped (2026-10-01, worktree `knowledge-mesh`): the `taxon` kind
(migration 0173), the two-start-node seed over the four legacy registries
(0174), the guarded `specialises` hierarchy, `view='path'`, `/unrooted`,
`/unmapped`, the `under=`/`axis=`/`depth=` facets, dimension-gated dedup and
path-form ids. Truth for all of that lives in `handlers/taxon.py`,
`store/_taxon_ops.py` and `taxonomy/nodes.py` docstrings and the
`precis-taxon-help` skill. The legacy tables stay standing;
`measures-substrate.md` retires them.

## In scope (v1.5)

1. **Edge axis as a taxon.** The seed mints a third start node `axis` with
   six children (`method`, `material`, `system`, `regime`, `quantity`,
   `scale` — Reto 2026-09-30, decisions log);
   `link(rel='specialises', meta={axis: X})` then requires `X` to resolve to
   a taxon slug under it, and link-side filters accept `axis=<slug>` widened
   by `under=`. Until this ships `axis` is an unvalidated free string. This
   is the "hierarchical link tags" half of the 2026-09-29 mesh handoff.
   Waits on graph-gardener-promoted axes.

## v1 follow-ups (unscheduled, each small)

- **Dedup cutoff.** `DEDUP_MAX_DISTANCE = 0.25` was picked with no real
  embeddings; calibrate against the 79 seeded nodes once prod has embedded
  their cards.
- **`put(link=)` path form.** `get`, `under=` and `link(target=)` resolve
  `measurand/temperature`; create-time `put(link=)` still needs `tn<id>`,
  and so does `link(target='taxon:a/b')` from a non-taxon source
  (`instance-of`): only `TaxonHandler.link` resolves paths, other kinds
  fall to `parse_link_target`, which wants an integer.
- **Fresh-DB bootstrap.** A DB built from the baseline marks 0174 applied
  without running it, so it has no start nodes (same class as the
  baseline-skips-seed-INSERTs gap). Lazily minting the two start nodes on
  first taxon put would close it.
- **`/unmapped` scope.** It excludes `component_categories`-derived subject
  nodes, which carry no dimension by design; the literal v1 rule would list
  all 13.
- **Legacy keys as aliases.** The seed adds each legacy key that differs
  from the lowercased name as an alias; two registries sharing a key make a
  path lookup on it ambiguous (refused with candidates, not wrong).

- **Unbounded path enumeration.** `taxon_ancestors`/`taxon_descendants`/
  `_taxon_chains` carry a path array (cycle-safe) but enumerate every
  distinct path before `DISTINCT`/`LIMIT`, so a diamond-rich lattice costs
  exponential work. Harmless on the flat seed; switch the set-valued walks
  to `UNION` with a depth column before multi-parent nodes accumulate.
- **Unmapped seed nodes vs the measurand contract.** A legacy row whose
  label is missing from 0174's lookup seeds under `measurand` without
  `dimension_kind` (the seed bypasses the guard); any later link touching
  it as a child is refused until its meta is set. Prod maps all 24 labels
  today, so this bites only on a new label.
- **SQL vs Python slug on non-ASCII names.** 0174's `lower()`/regex follow
  the DB locale; Python's are Unicode-aware. Tested equal on every current
  name, unproven for e.g. `Å`.

## Explicitly NOT in scope

Status promotion machinery (the N≥3 usage rule belongs to the extraction
tree), merge-as-redirect, the dedup judge, alias repointing, contract
ranges, a `TAXON_RELATIONS` eye ring, `view='tree'`/`view='siblings'`
(siblings are `search(under=<parent>, depth=1)`), unit conversion, and any
change to the legacy value tables (`measures-substrate.md`).

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
| `tagged` membership role | no ref → taxon edge; this item had deferred `instance-of` to `measures-substrate` | **`instance-of` / `has-instance` moves into v1** (shipped in v1, migration 0173). Any existing ref of any kind joins the mesh by `link(rel='instance-of', target='taxon:…')` — no re-kinding, no new table. Reto confirmed this reading 2026-09-29. |
| edge = one role + free tags; traversal "from an angle" filters on an edge tag | `links.relation` + free `links.meta`; here `meta.axis` is an unvalidated string | **hierarchical link tags, staged**: v1 unchanged; v1.5 = v1.5 in-scope 1 (`axis` start node, `meta.axis` resolves to a taxon under it, `under=` widening on link filters). Other `meta` keys stay free. |
| roles carry `proposed`/`core`; a recurring edge tag on one role is promoted to a role | nothing | no `proposed` relations as rows. Promotion is `graph-gardener.md`'s vocabulary-consolidation job and lands as a migration-proposal `todo` (`waiting-for:reto`), because the registry changes only by migration. |
| `specialises` is acyclic; cycle rejected at insert; endpoints any → same kind | shipped in v1 (pre-insert ancestor check) | same, plus the endpoint rule: `TaxonHandler` refuses `specialises` unless both ends are `taxon`. Other kinds' use of the relation is untouched. |
| materialised `closure` table | rejected in v1 (`store/_taxon_ops.py`) (recursive CTE, `_refs_ops.py` precedent) | stays rejected. Revisit trigger: > 10⁴ taxon nodes, or `view='path'` p95 > 50 ms on prod. |
| `primary_parent` + stored `canonical_path` | rejected in v1 (`store/_taxon_ops.py`) (reparent cascade unsolved) | stays rejected. Handoff Q2: no stored primary; the display path is the shortest chain to a start node, ties by axis name, computed on read. |
| slugs sibling-unique; `resolve(path)` | slugs non-unique, resolution-only; identity is `ref_id` | path-form ids on `get` (decisions log, 2026-09-29); ambiguity ⇒ refuse with candidates. |
| `/x/**` and `/x/*` wildcards | `under=` / `depth=` facets | `/x/**` = `under=x`; `/x/*` = `under=x, depth=1`. No glob parser. |
| per-role endpoint kind lists (`role_spec.src_kinds`) | only `guard_and_route_contradicts_disputes` | not generalised in v1; the taxon-endpoint rule above is the only new check. |
| `provenance in ('machine','human')` required on every node and edge; `session_id` | `refs.set_by` / `chunks.set_by` exist but are NULL on insert (schema comment); `agentlog` + `touched` links are the per-run write set | → `curation-gate.md` (populating `set_by`, protecting human content, the reviewer loop). The handoff's `session_id` is `agentlog_id`. |
| `status proposed/core/deprecated` | `proposed` → `systematic` earned by `taxonomy-bootstrap.md`; `retired_at` | keep. |
| one claim per node; split a sprawling note | `knowledge-mesh.md` §5 lede-first lint | no change here. |
| traversal grammar `role[:tag-path]`, `role!` (handoff Q3) | `knowledge-mesh.md` §2 eye ladder, no walk verb | ruled there: filter arguments on the `+1hop`/`+2hop` rungs. |
| qualifiers on findings so "same number" ≠ "same quantity" (handoff Q5) | `measures-substrate.md` (conditions, reference, direction; identity = taxon + reference + convention) | no change. |

Net new work from the handoff, all recorded above: v1 gained
`instance-of`; v1.5 in-scope 1 (axis as taxon) is new; the taxon-endpoint rule on
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
| `hierarchy_edge` strict is-a, one hierarchy per slot type (species, materials, process types, roles, measurand types, reaction types) | `specialises` with `meta.axis`; v1.5 in-scope 1 (axis as taxon) | same; the hierarchy name *is* the axis. Widening never crosses an axis. |
| `closure` table, rebuilt by recursive CTE, versioned | rejected in v1 (`store/_taxon_ops.py`) (recursive CTE at 10²–10³ nodes) | stays rejected; the revisit trigger stands. |
| `node_ic` information content for Resnik similarity | nothing | new — `class-lattice-…` §2. |
| `class` = defined / primitive / facet value with canonical `constraint_set` + `constraint_hash`; `membership` yes/no/unknown; `pooled_estimate` | primitive nodes only | **new, decided 2026-09-30 (Reto): v2 of this item**, shared by scenario classes and se pocket specs — `class-lattice-…` §1. |
| `relation(subj, predicate, obj, source, confidence, qualifiers)` for paper-extracted classification claims, separate from the curated backbone | `links` + `finding` rows + `instance-of` (shipped in v1, migration 0173) | no separate table; an extracted classification is an `instance-of` link whose evidence is a finding hub, same as any claim. |
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
  as a `range_kinds` value on the `relations` row (the columns shipped in
  migration 0180), not as a bespoke guard.
- **[decided 2026-09-30, Reto]** The `meta.axis` vocabulary: **seed six,
  earn the rest.** The `axis` start node (v1.5 in-scope 1) is seeded with
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
