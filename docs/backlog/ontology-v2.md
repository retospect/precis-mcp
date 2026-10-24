---
status: draft
title: Ontology v2 — grouped measurands on named axes, a method start node, campaign tag vocabularies promoted to taxa with instance-of edges, tags that are links become links, the OPEN folksonomy teardown
pillar: memory-graph
prio: high
model: opus
---

# Ontology v2

Approved by Reto 2026-10-22 (chat) from the ontology review (memory
me504796, gripe gr504795). Absorbs `open-namespace-teardown.md` (deleted
with this file; the machine-writer phase of that teardown already lives in
[greenfield-schema-review §OPEN-namespace teardown](greenfield-schema-review.md#open-namespace-teardown-was-open-namespace-teardown),
GO 2026-10-03 — this item owns the folksonomy phase only).

## Motivation / why

Prod, read-only, 2026-10-22 (`scripts/prod-psql`, session MCP):

| fact | value |
|---|---|
| taxa (`refs.kind='taxon'`, not retired) | 112; `/unrooted` = the dogfood fixture only |
| `measurand` tn460084 direct children | 96 (95 at review time + tn472841 relinked), one unnamed axis, no intermediate node |
| `subject` tn460085 direct children | 13, all `component_categories` seed nodes; no material, no reaction |
| `instance-of` edges into any taxon | **0** — papers reach measurands only through chunk `quantifies` edges (179) |
| `search(kind=paper, under=<taxon>)` | returns nothing for every node (it walks `instance-of`) |
| UPPERCASE tag namespaces live on prod | ≥38; 13 registered as closed axes (`ATTEMPT AUDIT CACHE DENSITY DREAM ORIGIN PRIO SPACE SRC STALE STATUS TAPROOT WATCH`), 9 of those in use; **28 unregistered** |
| `search(kind='paper', tags=['MATERIAL:mof'])` | refused — `unknown closed-prefix axis: 'MATERIAL'`; `view='facets'` renders the same axis and `get(kind='tag', id='MATERIAL:mof')` counts 3766 refs (3151 papers) |
| `OPEN` ref-tag rows | 219,084 total; `discovered-via:*` 41,796 rows / 5,987 values; `topic:*` 17,437 / 1,177; `interest:*` 6,985 / 1,582; `quest:*` 3,406 / 15; `catalyst:*` 1,665 / 24; `mode:*` 1,666 / 12 |
| unit inconsistency | tn504453 Binding energy `canonical_unit=eV`; tn465830 adsorption energy `canonical_unit=J`, `display_unit=eV`. `edit(kind='taxon')` cannot touch `canonical_unit` |

Two systems that never meet: taxa (hierarchy, dimension, `under=`,
`facets`) and tags (every campaign vocabulary, every categorizer axis).
Everything an agent would navigate by is on the tag side, so the taxon
affordances answer nothing, and the tag side has no hierarchy, no
definition, no dimension and 28 undocumented machine axes.

Missing nodes named by the review (none exist as taxa): measurands
overpotential, Tafel slope, turnover frequency, onset potential, band gap,
BET surface area, ECSA; reaction classes NORR, NO3RR, HER, OER, CO2RR, NRR;
the 24 `catalyst:*` classes; material classes mof, graphene, cnt, zeolite
(`MATERIAL:*` has 13 values); methods DFT, expt-electrochemical,
microkinetic, MLIP (`mode:*` has them as tags: `dft`, `expt-electrochemical`,
`microkinetic-or-ml`, `dft+expt`, `mixed`, …).

## Target shape

```text
measurand   (start, contract: dimension_kind)            — unchanged root
  ├─ energetics        [axis quantity-class]  adsorption energy, binding energy, activation barrier, step free energy, band gap
  ├─ electrochemical   [quantity-class]       overpotential, Tafel slope, onset potential, TOF, faradaic efficiency, current density, ECSA
  ├─ surface-texture   [quantity-class]       BET surface area, pore volume, …
  ├─ geometric         [quantity-class]       the component_spec seed: bore diameter, length, head height, …
  ├─ mechanical / thermal / electrical / rate … (≤12 groups total)
subject     (start)                                      — unchanged root
  ├─ component         [subject-class]        the 13 seed nodes move here
  ├─ material          [subject-class]        mof, graphene, cnt, zeolite, oxide, sulfide, … (from MATERIAL:* ∪ catalyst:*)
  └─ reaction          [subject-class]        NORR, NO3RR, HER, OER, CO2RR, NRR
method      (start, new; contract: none)
  ├─ computational     [method-class]         dft, microkinetic, mlip
  └─ experimental      [method-class]         electrochemical, photocatalytic, thermal, operando
```

Axis labels are the edge `meta.axis` strings term-taxonomy v1.5 will
validate against its `axis` start node; until v1.5 ships they are free
strings, reused verbatim here. A leaf keeps its direct `specialises` edge to
the root until path lookup accepts ancestor paths (slice 0), so
`measurand/temperature` and `property='measurand/faradaic-efficiency'`
never break.

## In scope

Each slice is one tree, one migration where marked; the migration carries
its rollback as the next forward migration (`docs/conventions/invariants.md`).

0. **Guards and lookup, no data change.** *Landed 2026-10-22 (onto-s0).*
   (a) `put(kind='taxon')` refuses a
   `canonical_unit` that is not the coherent SI unit of `si_vector` (eV on
   an energy vector → `next: canonical_unit='J', display_unit='eV'`).
   (b) `edit(kind='taxon', meta={'canonical_unit', 'display_unit'})` is
   accepted when no live `measures` row references the node, refused
   naming the count otherwise (the rebase of live rows is NOT built; see
   open questions). (c) Path lookup `a/b` resolves `b` with `a` as an
   *ancestor* (the `taxon_ancestors` walk), not only as the direct parent;
   ambiguity still refuses with candidates. (d) The closed-axis registry
   reads the categorizer axes from `data/axes/*.yaml` at import, so
   `search(tags=['MATERIAL:mof'])` and `tag(add=)` accept every axis a
   pass writes; `precis-tags` documents each with its writer pass. Fix
   tn504453 by hand through (b) once deployed.
1. **Measurand groups.** *Driver built (onto-s1):
   `scripts/onto-groups` (dry-run/apply), awaiting prod `--apply` after
   deploy.* The driver mints the **13** Q8-ruled group nodes (Q8 ruling,
   superseding the ≤12 proposal) under `measurand` as
   `dimension_kind='categorical'` navigation nodes (the start contract
   holds; a group is not a measurand) and adds a `specialises` edge from
   every live leaf to its group with
   `meta={'axis':'quantity-class','migration':'onto-groups'}`. The direct
   edge to the root stays, untouched. Mapping table is the driver's
   `GROUPS`, verbatim from the ruled member table; a leaf with no group
   lands in `measurand/other` (minted only when needed); an unresolved or
   ambiguous member, or a same-slug node outside `measurand`, refuses the
   apply. The six taxa owned by onto-repoint/-fix (tn460115 + the five
   legacy terms) are skipped. Rollback: delete links
   `WHERE meta->>'migration'='onto-groups'`, retire the group nodes.
2. **New nodes.** `method` start node; `subject/material`,
   `subject/reaction`, `subject/component` (the 13 seed nodes re-parented
   as slice 1 did); the seven missing measurands with `si_vector`,
   SI `canonical_unit`, `display_unit`, `higher_is_better`,
   `required_conditions` (overpotential: `product`, `current-density`;
   TOF: `product`). Reaction nodes carry `aliases` (`NORR` ↔ `NO reduction
   reaction`). Dedup runs with the embedder up; a refusal is read, not
   bypassed.
3. **Campaign vocabularies → instance-of.** Migration + backfill, one
   prefix per migration, dual-write first: for each `(prefix, value)` in
   `catalyst:*`, `mode:*`, `MATERIAL:*`, and the `topic:`/`interest:`
   values that map 1:1 to a node from slice 2, mint the taxon if absent and
   write `instance-of` from every tagged ref to the node with
   `meta={'migration': <id>, 'prior_tag': '<ns>:<value>'}`. The
   categorizer passes (`data/axes/*.yaml`) write the link beside the tag
   from this slice on. Tags are culled by a later migration after
   acceptance (one release of dual-write). Migration writes are exempt
   from the machine-edge structural refusal the way merge repointing is
   (`store/_links_ops.py::add_link`, `_machine_edge_admission`): a 1:1
   relabel carrying `prior_tag` is not a model judgement. Rollback:
   delete links `WHERE meta.migration=<id>`; the tags are still there.
4. **Tags that are links → links.** `discovered-via:cite:<paper>`
   (writers `workers/watch_poll.py`, `workers/stub_rank.py`) becomes a
   `derived-from`-class edge ref→paper (relation name: open question 6);
   `quest:<id>` (writers `quest/needs.py`, `quest/tagging.py`,
   `quest/pourbaix_map.py`, `quest/status.py`, `quest/loop.py`) becomes
   the existing `serves` edge the quest `view='measures'` walk already
   reads. Order per prefix: writer writes both → backfill migration →
   readers switch (`LIKE 'quest:%'` filters → link reads) → cull
   migration. Rollback at each step is the previous step.
5. **Synonym collapse and the folksonomy cull** (the OPEN teardown's
   second phase). `interest:` → `topic:` on memories (the writer is found
   and switched first); gripe bare `pcb` and `topic:pcb` → `area:pcb`
   (`area:` = product/code area on workflow kinds, `topic:` = subject
   matter on research kinds); `project:` typos as in the review
   (`nox-ammonia`/`nox-to-ammonia` → `nox_to_ammonia`, done by hand
   2026-10-22). Junk: `status:exercise-*`, shadow `prio:high/low`,
   `projects:`, opaque `kind:a1`. Exact-match deletes only:
   `namespace='OPEN' AND value LIKE '<prefix>:%'`, never
   `namespace LIKE 'OPEN%'` (eats `OPEN-QUESTION`); `severity:` ≠ `SEV`,
   `prio:` ≠ `PRIO` ≠ `refs.prio`; `sticky:` has no writer but memory TTL
   reads it — not culled.
6. **Machine axes: document or fence.** Content axes written by the
   categorizer passes (`SCALE MATERIAL TOPICCASCADE ROLE PROPERTY
   STUDYTYPE MOVE DIM SEV DOMAIN TRANSPORT OPEN-QUESTION`) are registered
   by slice 0(d) and listed in `precis-tags` with pass name and version.
   Pass bookkeeping (`*CASCADE`, `*CASCADEATTEMPT`, `CHASETRIG`,
   `BIBMARK`, `BIBPARSEATTEMPT`, `PATENT_EXAMPLE`, `ROLE3`, `NOTE`) leaves
   the tag table for `refs.meta.passes` (jsonb: pass → {version, at,
   outcome}), one forward migration per prefix, Pile A style
   (greenfield-schema-review). `get(kind='tag')` then lists only
   namespaces an agent may filter on.

## Explicitly NOT in scope

Term-taxonomy v1.5 itself (the `axis` start node and validated
`meta.axis`); re-basing live `measures` rows to a changed canonical unit;
status promotion (`proposed` → `systematic`); the machine-writer phase of
the OPEN teardown (Pile A, greenfield-schema-review); merging the
duplicate taxa the review left for Reto (tn460100/tn460101,
tn460122/tn460138, tn460129/tn460157, tn460115/tn460116 — gardener
merge proposals); the taxonomy-bootstrap measurand list (its output
lands under the slice-1 groups when it runs); any change to the
relation registry beyond what open question 6 decides.

## Acceptance criteria

- `search(kind='paper', under='subject/material/mof')` returns ≥3,000
  papers (today 0; `instance-of` into taxa = 0).
- `search(kind='paper', tags=['MATERIAL:mof'])` is accepted and, during
  dual-write, returns the same set as the `under=` form.
- `get(kind='taxon', id='measurand', view='facets')` reports instances >0
  and splits on ≥2 named axes; `get(kind='taxon', id='subject/material',
  view='facets', args={'cross': ['method-class', 'quantity-class']})`
  returns a non-empty gap table.
- `search(kind='taxon', under='measurand', depth=1)` lists the 13 ruled
  groups (+ `other` when needed; leaf bridge edges also appear at depth 1
  until their later removal); `view='path'` on every leaf shows an axis
  label on every hop of the grouped chain (the bridge hop is unlabelled
  while the bridge edge stays).
- `get(kind='taxon', id='measurand/temperature')` and
  `search(kind='measure', property='measurand/faradaic-efficiency')`
  resolve before and after slice 1.
- `get(kind='taxon', id='/unrooted')` lists the dogfood fixture only.
- `put(kind='taxon', text='x — …', meta={'dimension_kind':'si',
  'si_vector':'2,1,-2,0,0,0,0', 'canonical_unit':'eV'})` is refused naming
  `J`; tn504453 reads `canonical_unit=J`, `display_unit=eV`.
- After slice 4's cull: `get(kind='tag')` shows no `discovered-via:*` and
  no `quest:*`; the quest `view='measures'` and the stub-rank reader
  return the same rows as before the cull (counted in the migration's
  dry run).
- After slice 5: no `interest:*` on memories, no bare `pcb` on gripes;
  the junk list is gone; `sticky:*` count unchanged.
- Every UPPERCASE namespace present on prod is in `precis-tags`, or is
  not a tag namespace any more. The precis-tags claim is tested
  (`tests/`: registry ∪ `data/axes` == skill table).
- Every migration prints a dry-run count and ships with its rollback
  migration number in its header comment.

## Target + blast radius

`src/precis/taxonomy/` (`facets.py`, `nodes.py`, path lookup), the taxon
and search handlers (`under=`, `property=`), `src/precis/utils/
link_tag_ops.py` (closed-axis registry, `check_relation_constraints`),
`store/_links_ops.py::add_link` (migration exemption), the categorizer
passes under `data/axes/`, `workers/watch_poll.py`, `workers/stub_rank.py`,
`quest/{needs,tagging,pourbaix_map,status,loop}.py`, migrations (one per
slice step), skills `precis-tags`, `precis-taxon-help`,
`precis-classify-help`, `precis-measure-help`. Post-deploy look: the
acceptance queries above, run on prod read-only.

## Open questions / decisions log

**Ruled (Reto 2026-10-22): 1–7 approved as recommended. 8 approved:** 13
quantity-class groups — geometry; component form; mechanical; thermal;
electrical and dielectric; bulk and structure (+BET, ECSA); molecular and
surface (kept separate); energetics (renamed from "DFT energetics": +band
gap); electrochemical performance (+overpotential, Tafel slope, onset
potential, TOF); reaction conditions; reaction outcome; cost; generic
(count, ratio, angle, PCB routed-net count; kept). Revisit after slice 1
lands and gripes come in (todo filed). Member mapping:

| group | members (taxon titles) |
|---|---|
| geometry | across flats, bore diameter, drive size, head diameter, head height, height, inner diameter, length→overall length, outer diameter, minimum bend radius, thickness, wall thickness, width, thread pitch, head angle |
| component form | drive code, drive type, head form, point type, thread size, grade, surface finish, is reinforced |
| mechanical | Young's modulus, shear modulus, Poisson's ratio, ultimate tensile strength, tensile yield strength, elongation at break, Vickers hardness, dynamic load rating, maximum working pressure |
| thermal | melting point, maximum service temperature, specific heat capacity, thermal conductivity, coefficient of thermal expansion |
| electrical and dielectric | electrical resistivity, dielectric strength, relative permittivity, is magnetic, electric field magnitude, electrostatic potential |
| bulk and structure | density, mass, crystal structure; new: BET surface area, ECSA |
| molecular and surface | dipole moment, net partial charge, surface charge density, H-bond donor count, H-bond acceptor count, hydrophobicity index, contact angle, absorption maximum wavelength |
| energetics | adsorption energy, binding energy, reaction step free energy, activation barrier; new: band gap |
| electrochemical performance | faradaic efficiency, partial current density, total current density, product yield rate per geometric area, product yield rate per catalyst mass, limiting potential, stability duration; new: overpotential, Tafel slope, onset potential, TOF |
| reaction conditions | temperature, pressure, reaction time, solvent, scale, catalyst loading, applied electrode potential, NO partial pressure, NO feed fraction, electrolyte concentration |
| reaction outcome | yield, product selectivity, reactant conversion, reaction product, atom economy, enantiomeric excess |
| cost | unit cost, cost per unit mass (price per gram merged in) |
| generic | count, ratio, angle, PCB routed-net count |

Merged/retired before slice 1: duplicate bore diameter and maximum service
temperature, length→overall length, price per gram→cost per unit mass;
retired delta length, unit length, persistence length, thermal half life,
PSS short fraction. Driver built: `scripts/onto-repoint` (dry-run/apply) +
the 0234 `precis.allow_measurand_repoint` door — landed, awaiting
coordinator --apply. The five retire through the delete verb's tombstone
path (`Store.retire_ref`); the ruling names no re-point target for them,
so the script reports each taxon's attached-measure count and refuses to
retire while rows are attached — those resolve by hand (measures are
append-only: supersede-per-row).

1. **`method` start node vs v1.5's `axis/method`.** Recommend separate
   nodes: `axis/*` names edge angles (a label vocabulary), `method/*`
   classifies refs (instance-of targets). Same slug is fine — paths differ.
2. **Ancestor-path lookup (slice 0c) or keep the double edge forever.**
   Recommend ancestor lookup; the double edge is a one-release bridge.
   Cost: `taxon_ancestors` enumerates paths (term-taxonomy follow-up
   "unbounded path enumeration") — bound it with `UNION` + depth before
   slice 1 lands.
3. **Who writes `instance-of` for new refs after the cull.** Recommend the
   categorizer passes write the link directly (the tag was always a
   classification); `tags=['MATERIAL:mof']` stays as sugar that rewrites
   to `under=` so skills need no change.
4. **`interest:` writer.** Unknown until grepped (the review found it
   only on memories). Recommend: switch the writer to `topic:`, then
   migrate; if the writer is the dream tick, pair with gr504795.
5. **Canonical-unit rebase with live measures.** Recommend not building
   it: the slice-0 guard stops new cases, tn504453 has no live measures.
   If a case appears, it is a supersede-per-row pass under the measures
   append-only rule.
6. **Relation for `discovered-via:cite:<paper>`.** Recommend reuse of
   `derived-from` with `meta.via='cite'` over a new relation (the registry
   changes only by migration, graph-gardener-value §promotion rule). If
   `derived-from` is semantically wrong for "found while chasing", a new
   `discovered-via` relation is one migration.
7. **Reto's three from the teardown:** `level:` — recommend a column on
   todos (it is tree depth, machine-written); `internal-thought` dual
   writer — recommend drop the tag writer, the meta key stays;
   `sticky:` — recommend keep until memory TTL reads `meta.sticky`, then
   one migration.
8. **Group list for slice 1.** The ≤12 names above are a proposal; the
   migration's mapping table is the decision. Recommend Reto reads the
   table, not the code.
