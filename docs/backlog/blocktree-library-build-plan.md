---
status: ready
title: build plan — cross-design instancing, block states, complementary ports, ranked library search
prio: high
model: opus
---

# Build plan

The block-library leg of the multiscale programme
(`multiscale-design-architecture.md` is the map). **Merged 2026-09-11:**
this doc absorbed `functional-block-library-and-assembly-states.md` (the
what and why, now §Why below) and the residuals of
`nm-se-shared-blocktree-core.md` (the spine extraction, done — §Settled
below). Physics stays in `photoswitch-states-and-spectral-dof.md`.

**Preconditions that make this cheap.** Every `nm`/`se`/`pcb` table is empty
in prod (measured 2026-09-07), Reto has lifted backward compatibility, and the
shared spine `precis.blocktree` already exists (commits `28877919`,
`96690d37`) so each change below lands **once** and serves both kinds.

**2026-09-14 (map amendment, window ran same day):** `nm-se-merge.md`
folded the `nm` kind into `se`'s atomic mode *before* this plan
dispatches, as planned — "both kinds" below is now "se's two modes"
(non-atomic and atomic), and every `nm`-facing hook in these slices
targets se atomic mode. The blocktree spine was already kind-agnostic,
so the slices' content is unchanged; only the adopter's name is. The
"Why" table's `nm` column and the "Settled" section's "two kinds" framing
below are left as the historical record of when the spine was built
(2026-09-07, predates the merge) — read `nm` there as "what became se
atomic mode."

## Why — the three-level chain and the library query

Reto, 2026-09-07: a searchable library ("*opto deform bistable 1nm lengthen
with copper click chemistry*"), box-type abstractions to check interfaces
and DRC **before atoms**, and "loaded for click chem" / "virtual bond is
now here" states. Three views of one thing, each with an existing home:

| level | what it is | kind | why that one |
|---|---|---|---|
| **box** | envelope + typed ports + states — what you assemble and DRC | `nm` block | intent-over-atoms; envelopes/ports are its point |
| **molecule** | the purchasable reagent (supplier, price, purity) | `component` | it is **bought**; `component` is the procurement store |
| **atoms** | the realised structure, relaxed | `structure` | L5 fill; `nm` mints and binds these |

Edges exist for both hops (`realized-by`; nm's `bound_design`), so "expand
to atoms" is a traversal. This also resolves the bought-vs-made tension: a
click-chemistry building block is *both* — the block is the abstraction,
the component is the bottle on the shelf.

The query decomposes: opto deform = actuation stimulus `light`; bistable =
P-type (both states thermally stable); 1 nm lengthen = Δ(end-to-end) ≈
10 Å; copper click = ports' joining chemistry CuAAC. The first three are
**sourced facts** (star schema — Δdistance in solution and in a rigid
scaffold are different numbers, both true); the fourth is a port role
(slice 3). The response shape that slice 4 must deliver — ranked partial
match, never a strict filter:

    query: opto-deform · bistable · Δ 1.0 nm · CuAAC

    azo-CuAAC-01     opto ✓  bistable ✗ (T-type, τ½ 2 d)  Δ 0.34 nm  CuAAC ✓   3/4
    dae-alkyne-04    opto ✓  bistable ✓                   Δ 0.42 nm  CuAAC ✓   3/4
    dae-long-11      opto ✓  bistable ✓                   Δ 0.9 nm   NHS ✗     3/4

None is a hit; all three are decisions ("half the throw — use two in
series"; "right throw, wrong handle — re-functionalise"). A strict AND
returns an empty set and teaches nothing.

**DRC before atoms**, once slices 1–5 exist: interface check (complementary
halves of a declared joining chemistry), precedent check (yield spread
quoted), fit check in the `bonded` state (clash + purchasable-leaf
termination), and envelope arithmetic — does the switch have room to move
(a 1 nm switch in a cage with 1.0 nm vertex-band clearance is the measured
boxel failure mode, and the check is arithmetic on numbers we already
produce).

**What NOT to alter** (each is tempting): no deformable envelopes (bistable
= two rigid states + a transition; deformation would cost cad's analytic
exactness); no "part library" kind (parts are single-block designs;
procurement is `component`; properties are the star schema); no `nm`/`se`
kind merge (settled — see §Settled); the capability gate stays declared
intent, and `rxn` precedent stays evidence — neither ever claims a
reaction will work.

**Named consumers riding this critical path:** `nm-stick-placement.md`
(per-state interaction features on ports need slice 2's states; its
library seeding/joining dogfood needs slices 1 and 4) and the structural
leg's slice 5, state-dependent stability
(`structural-solution-space.md`, blocked on slice 2).

## Critical path

    1 cross-design instancing  ──▶  2 block states  ──▶  3 complementary ports
                                            │                      │
                                            └──────────┬───────────┘
                                                       ▼
                                            4 ranked library search
                                                       │
                                        ┌──────────────┴──────────────┐
                                        ▼                             ▼
                              5 rxn-driven transitions      6 realized-by → component
                                   + precedent DRC              ("what do I order")

Items 7–9 were independent of the slices; all three are done.

---

## Slice 1 — cross-design instancing (the library blocker) — **SHIPPED**

Landed 2026-09-07 in `a5efc341`, in the same commit that wrote this plan —
which is why the section read as open for a week afterwards. The heading
stays (numbered, because slices 2–6 and three other docs cite these
numbers); the spec body is gone per delete-on-ship. What exists now:

- A `template` is *qualified* — a bare local block name, or
  `<design-slug>#<block-name>` (`precis.blocktree.types.parse_template_ref`,
  `TEMPLATE_SEP`; `'#'` is reserved out of block names at both places one is
  minted). Placement is **by reference**: `effective_envelope`/
  `effective_ports`/`effective_dof` resolve through
  `blocktree.ops.resolve_template` on every read, so editing the library
  design updates every consumer with no re-save.
- `_find_instance_cycle` walks `(design slug, block name)` nodes across
  designs and reports every hop qualified the moment more than one design is
  involved, so an A→B→A cycle is refused naming both.
- Read-path resolver: `precis_se.persist.foreign_resolver`, memoized per
  closure and wired onto every tree `load_tree` returns — one
  `get_ref`+`load_tree` per distinct foreign slug, never one per port, and
  no read path (handler, web reader, jobs) can forget to wire one.
  `SeHandler` replaces it with a call-scoped resolver so one put/edit/get
  shares the cache across trees.
- Storage: `se_blocks.template_ref`, name-keyed text (migration
  `0004_se_template_ref.sql`) — never a row-id FK, since ids are rebuilt on
  every save.

**Deferred, on purpose:** a cross-design template stays *name*-keyed. The
uid cutover (`0009_se_block_uid.sql`) gave local references a
`template_uid` that survives a relabel; a foreign one has no uid→design
index to walk, so that conversion is its own slice
(`precis_se/persist.py`'s module docstring marks the spot).

---

## Slice 2 — discrete block states + stimulus-labelled transitions — **SHIPPED**

Storage landed 2026-09-14 in `7bb28f55` (design-core round 1); the se
consumer side landed 2026-09-15 in `7f7f3a75`. The heading stays (numbered,
because slices 3–6 and other docs cite these numbers); the spec body is gone
per delete-on-ship. What exists now:

- **Storage is shared, not se-local**, per the 2026-09-12 ruling: states,
  transitions and per-block current state live in `precis.design.states`
  (migration `0162_design_core.sql`), with `driver_kind` a closed enum
  (`light`, `reaction`, `redox`, `ph`, `thermal`, `mechanical`). The
  "two migrations" plan here was overtaken — the state tables rode
  design-core's own migration, so **slice 2 added none**.
- `declare_states` / `declare_transitions` / `set_current_state` ops on se
  (`precis_se.ops`), materialized after `persist.save_tree` mints block uids
  because the shared tables key on uid.
- `get(..., args={"state": {...}})` poses transiently on `view='tree'`,
  `'block'` and `'clearance'`; `set_current_state` is the persistent pose.
  The two are deliberately distinct.
- `view='sweep'` over the cross product of state-carrying blocks' states,
  bounded on **both** axes — a combination cap and one shared wall-clock
  budget — each reporting what went unchecked, so a timed-out sweep can
  never read as a clean one.
- Transitions are directed edges: forward and reverse are separate rows, so
  a ratchet's differing barriers stay expressible.

gr342026 (direction-only `port_pose_overrides`) is settled: ports carry a
nullable, provenance-tagged pose slot (`Port.pose`/`rot`/`pose_source`,
se migration `0011_se_port_pose.sql`) and a state override is a rigid
delta `{port: {'direction'?, 'pose'?, 'rot'?}}` — see
`port-pose-and-composition-search.md` §Decision 1.

---

## Slice 3 — complementary port roles — **SHIPPED**

Landed 2026-09-16. The heading stays (numbered, because slices 4–6 cite
it); the spec body is gone per delete-on-ship. What exists now:

- `precis_se.atomic.vocab.COMPLEMENTARY_ROLES` — the pairs, reusing the
  face-code alphabet of `nm-face-codes-and-scale.md` (donor↔acceptor,
  bump↔hole, +↔−, ASCII `-`) plus `azide↔alkyne`; `JOINING_HALVES` maps a
  joining's own name (`CuAAC`) onto its pair, so a bond gates on either
  half or on the chemistry. `role_halves(role)` resolves all three;
  `None` means a symmetric role, gated as before (`covalent`+`covalent`
  unchanged).
- One rule, `bond_capability_offences`, feeds both the write-time gate
  (`check_bond_capability`, connect op) and the stored-data re-check
  (`port_capability` in `precis_se.atomic.validate`), so they cannot
  drift. Azide+azide is refused naming both ports' roles and the pair it
  needs; a port with neither half is told its partner's complement.
- Trust model unchanged: labels compared to labels, never chemistry
  proof. Adding a joining is one tuple (plus one `JOINING_HALVES` entry
  when it has a name). Roles match exactly, no case folding.

Not touched: gr342026 (direction-only `port_pose_overrides`) — slice 3
reads roles only and bakes in no port-pose semantics.

---

## Slice 4 — ranked library search

**The surface.** Every attribute optional; results ranked by match count;
**per-attribute match/miss with the actual value** in the response. A bare
score is unusable — the judgement is *which compromise can I live with*.

    search(kind='se', stimulus='light', bistable=True,
           delta_length_nm=1.0, joining='CuAAC')

**Where the attributes live.** A part's functional properties are *sourced
facts* (λ, Δdistance, τ½, quantum yield, fatigue) and belong in the star
schema — `material`-shaped rows with conditions + citation, since Δdistance in
solution and in a rigid scaffold are different numbers and both true. So the
search **joins**: block → `realized-by`/`made-of` → the entity carrying the
value rows. Do not denormalise the facts onto the block; that loses provenance
and re-invents a store.

**Reuse `quest`'s selection machinery, do not write a second one.**
`meta.rubric_objectives` (Pareto front) and `meta.rubric_composite` (weighted)
already exist for exactly this. Its discipline carries: weights are
**human-set**, an agent may not tune its own objective.

**Done when:** a query with four attributes returns near-misses ranked, each
row showing which attributes matched and the actual value of those that did
not — and an empty result set is impossible unless the library is empty.

**SHIPPED** (2026-09-17): `search(kind='se', wants={...})` —
`src/precis_se/library.py` (candidate walk, attribute resolution, ranking,
rendering), `SeHandler.search`'s dispatch, one new `wants: dict[str, Any] |
None` kwarg on the `search` verb (`src/precis/tools/core.py`). `wants` is
one dict, not a flat kwarg per attribute — the plan's own sketch
(`stimulus=`, `bistable=`, `delta_length_nm=`, `joining=`) was a code-level
enumeration of a vocabulary that is star-schema *data*
(`material_properties.prop_id` / `component_specs.spec_id`), which would
have baked every proposed-tier property/spec into the verb signature. Each
`wants` value is a scalar target (10% relative tolerance for numbers, exact
for str/bool), a `[lo, hi]` interval (band-overlap, `material_search_
values`'s own rule), or an explicit `{'target'?, 'min'?, 'max'?, 'tol'?,
'weight'?}` — `weight` is the only place a weight comes from, human-set,
per the quest rubric discipline. `q=` becomes optional and pre-narrows the
candidate *designs* via the existing card search; a narrow to zero falls
back to the whole library and says so, never reading as "no matches".

Star-schema join order (first hit wins): a `component`-bound block's own
current spec values, then that component's `made-of` material's property
values, then the design's own `made-of` material (scoped to one block via
the link's `meta.block`, else every block). Ranking reuses
`precis.quest.frontier.pareto_split` for the tie-break over numeric keys
rather than a second dominance rule, exactly as directed.

**Known gap, left deliberately:** a **structure-bound** atomic block (its
L3 realization is chemistry, not a `component`) carries no value rows of
its own in this round — the star schema only reaches it via a design-level
`made-of` link, and a miss on such a block says so explicitly ("bound to
structure X: no value rows") rather than silently reading as absent data.
Wiring atomic-mode facts through `realized-by`/generated-structure
provenance is unscoped work for a later round.

`made-of` between an `se` design/component and a `material` needed no
relations-registry change — the generic `link()` door has no source/target
kind-pair gate (only the relation name is validated), so the edge was
already legal; the spec's contingency ("allow it there minimally") did not
apply.

---

## Slice 5 — rxn-driven transitions + precedent DRC — **SHIPPED**

The `bonded` state's geometry is the *product of a reaction*. Point the
transition's `driver_ref` at a `rxn` slug, then the precedent read shipped
today answers "does this joining chemistry work on substrates like mine, and
at what yield": `search(kind='rxn', property='yield', reaction_class=…)`.

**New DRC check:** a connect whose declared joining reaction has **no
precedent** is flagged — the same "unprecedented step" signal
`reaction-kind-and-synthesis-cost.md` already defines for routes. Not an
error; a flag with the evidence count.

**SHIPPED 2026-09-18** (4971b13e, gated with slice 6 in 6d1cf0b3):
`src/precis_se/precedent.py`, `rxn_precedent_count`, write-time rxn
resolution in the handler flush; tests `tests/test_se_precedent.py`.

**Spec (2026-09-18).** Two vocabularies meet here and are NOT the same
string: a port pair's joining *name* (`CuAAC`, `precis_se.atomic.vocab.
JOINING_HALVES`) and an `rxn` record's `reaction_class` (an RXNO id such
as `RXNO:0000024`, `refs.meta`). The bridge is the transition: a block's
`driver_kind='reaction'` transition names the `rxn` by slug, and that
record carries the class. No table maps joining names to RXNO ids in this
slice — the name stays a label, the rxn is the claim.

*Rxn-driven transitions (write half).* In `SeHandler`'s pending-transitions
flush (the one place with a store), a `driver_kind='reaction'` entry whose
`driver_ref` does not resolve through `store.get_ref(kind='rxn', id=slug)`
fails the edit with `BadInput` naming the op and the slug (`put(kind='rxn',
…)` first) — the whole edit rolls back like any op error. A missing
`driver_ref` on a reaction transition is the same refusal. Other driver
kinds are untouched (a wavelength is not a ref). The `## transitions`
render shows the resolved rxn as `rxn:<slug>` in the `driver_ref` column,
unchanged text otherwise.

*Precedent DRC (read half).* New module `src/precis_se/precedent.py`,
`findings(store, tree, ref_id) -> list[ValidationIssue]`, appended by the
handler's `view='drc'` path (`_render_drc`) AFTER `se_drc.drc(tree)` —
`drc()` itself stays store-free by contract (its docstring says so), the
way the governing-scenario line is already resolved by the handler. The
same findings join `view='validate'` only if that view already merges drc
findings; otherwise drc only. For every live connect (`tree.connects`,
endpoints looked up by name as `geometry_plausibility.findings` does):

- Transitions are template-owned (`declare_transitions` goes through
  `_template_owned`; an instance/array node's own uid never carries
  `design_transitions` rows). So for each endpoint, when `node.template
  is not None` resolve to `tree.blocks[node.template]` (foreign templates
  through `tree.foreign`, as `resolve_template` does) and read
  `design_states.transitions_for(store, ref_id, <template uid>)` — for a
  foreign template, that template's own design ref id. Collect the
  `driver_kind='reaction'` transitions of BOTH endpoints, dedupe by rxn
  slug.
- None, and the port pair is a known joining (`vocab.role_halves` /
  `JOINING_HALVES`): `joining_unnamed`, severity `info` — "joining CuAAC
  declared by roles only; no reaction transition names an rxn — declare
  one with `driver_ref=<rxn slug>` for a precedent read". None and no
  joining either: nothing (a plain mechanical connect).
- For each rxn: read `reaction_class` off the ref meta. Absent →
  `joining_class_unknown`, `warn`: "rxn <slug> has no reaction_class;
  precedent read impossible — `edit(kind='rxn', id=<slug>,
  reaction_class='RXNO:…')`". Present → count precedent through a new
  store helper `rxn_precedent_count(reaction_class) -> tuple[int, int]`
  (yield rows, distinct rxn refs) in `_rxn_ops.py` — one SQL COUNT over
  the same join `rxn_search_values` uses with `property_id='yield'`, no
  `limit`. Zero rows → `joining_unprecedented`, `warn`: "no precedent: 0
  yield rows for <class> (rxn <slug>) — unprecedented step, see
  `search(kind='rxn', property='yield', reaction_class='<class>')`". N>0
  → `joining_precedent`, `info`: "<n> yield row(s) across <m> rxn(s) for
  <class> (rxn <slug>)". The evidence count is always on the row; the
  header's error/warn counts are unchanged by info rows (the `info`
  severity already exists in `validate.py`).
- One connect, one finding per rxn; subject is the connect's
  `blockA.port↔blockB.port` label already used by
  `connect_envelope_disjoint`.

*Not in this slice:* a joining-name→RXNO map, substrate/functional-group
filtering of the precedent read (the rxn precedent verb has no such
facet yet), and slice 6's component wiring.

*Tests.* `tests/test_se_block_states.py`: reaction transition with an
unknown slug refused with op name + slug and the edit rolls back; with a
minted rxn (`RxnHandler(hub=Hub(store=store)).put(id=..., rxn_smiles=...)`
as `tests/test_rxn.py` does) it round-trips and renders `rxn:<slug>`.
New `tests/test_se_precedent.py`: the four findings (unnamed / class
unknown / unprecedented / precedent with counts) over a two-block
connect, the same finding when one endpoint is an INSTANCE of a template
that declares the reaction transition (the template-resolution rule
above — the adversarial test for this slice), the
plain-mechanical-connect silence, and the drc header counts
ignoring info rows; `tests/test_rxn.py`: `rxn_precedent_count` on 0, 1
and 2 rxns of one class. Skill `precis-se-help`: the
`declare_transitions` bullet says a `reaction` driver_ref must be an
existing rxn slug; the DRC section lists the four rules in one paragraph.

---

## Slice 6 — `realized-by` → `component` — **SHIPPED**

Block → the purchasable thing. The edge already exists (`se` uses it), so this
is wiring plus a `view` that answers "what do I order" by walking the
instanced tree to purchasable leaves. Mirrors the BOM rollup, including its
honesty line ("priced: N of M").

**SHIPPED 2026-09-18** (6d1cf0b3, remote gate green): `src/precis_se/order.py`,
`view='order'`, tests `tests/test_se_order.py`. Review-driven rulings now
in the code: the honesty line counts templates while `priced` counts
lines; a priced line with unresolved qty is unpriced; to-make lists leaf
templates only.

**Spec (2026-09-18, revised after the readiness pass).** `view='order'`
on `get(kind='se')`, new module `src/precis_se/order.py`,
`rollup(store, tree, ref_id) -> OrderReport` — takes the plain `store`
and calls `store.component_current_spec_value(ref_id, spec_id)` itself
(the handler's `_spec_number` is a bound method; importing it would cycle
handler ↔ order). Rendered by the handler next to `_render_bom`; the
unknown-view help text that enumerates every view grows `order` too. The
`realized-by` link is already derived on every save
(`persist.sync_realized_by`, component bindings only), so nothing new is
written; the walk reads the binding that link mirrors,
`block.bound_kind`/`block.bound`, never the link table.

*The walk — the `_render_fab` algorithm (handler.py), not a per-node
scan.* `bound_kind`, `bound` and `mode` live ONLY on ordinary/template
blocks (`_template_owned` refuses `set_binding`/`set_mode` on instance
and array nodes), and `bom.design_occurrences(tree)` already folds every
instance's and array's count into its template's total. So: iterate the
live blocks, `continue` past any node with `node.template is not None`
(exactly as `_render_fab` does), read binding/mode off the template, and
take `qty = design_occurrences[template_name]`. Cross-design: a
foreign-templated instance (`<slug>#<block>`) has no local template
block — resolve it through `resolve_template`/`tree.foreign` to the
foreign design's block for its binding and mode, while the occurrence
count stays the LOCAL instance's (its own array multiplicity along its
parents); `design_occurrences` never touches `tree.foreign`, so the
foreign leg of the count is order.py's own small addition, keyed on the
qualified template name. A template is a **leaf** when no live local
block has it as `parent`.

- `bound_kind='component'` → **purchasable**: one order line per distinct
  component slug, `qty` = summed occurrences, `used by` = the template
  names (deduped, sorted), `unit_cost` / `mass` via
  `store.component_current_spec_value(<component ref_id>, 'unit_cost' |
  'mass')` with the ref resolved by `store.get_ref(kind='component',
  id=slug)` (canonical store value, never copied), plus `category` and
  `mpn` from the component ref's `meta` when present.
- `bound_kind='part'` → **purchasable**, LCSC/JLC C-number line (`bound`
  holds the C-number), cost and mass `—` (no store value exists for
  parts; the honesty line says so rather than pricing it as zero).
- explicit `tree.bom` lines → included through the existing
  `bom.rollup(tree)` so fasteners declared as BOM lines appear once, on
  the same table, tagged `via: bom line`; a bom line and a bound leaf
  naming the same slug merge into one line (qty summed, both provenances
  shown), never two.
- anything else (`cad`, `structure`, unbound) → **to make**: a second
  table `block · mode · qty`, with the template's `mode` when declared,
  else `—`.

A non-leaf template that is itself bound to a component (an assembly
bought whole) is purchasable and its subtree is NOT walked — its
descendants are covered by the purchase; the line says `covers N
block(s)`.

*Honesty lines*, same shape as bom's: `purchasable: P of L leaf
template(s) · to make: M` then `priced: N of P line(s)` / `massed: …`;
the total cost prints only when every purchasable line is priced,
otherwise `total: ≥ <sum of priced> (partial, N of P)`. Empty cases have
their own wording, not bom's: no live blocks → `(no blocks yet —
unfilled)` (the `view='tree'` line); blocks but nothing purchasable and
no bom lines → `(nothing to order yet — bind a block to a component or
part, or add_bom)`.

*Not in this slice:* supplier/lead-time fields (the component kind has
none), a purchase-order export, and pricing of `part` items.

*Tests.* New `tests/test_se_order.py` (mint components with
`ComponentHandler(hub=hub).put(id=…, category=…, spec='unit_cost',
value=…, unit=…)` as `tests/test_component.py` does): a leaf template bound to a
component, instanced through an array, appears ONCE with the
array-multiplied qty (instance nodes are skipped, not classified); two blocks bound to the
same slug merge into one line with both names; a bought assembly hides
its children and says `covers N`; a `part` line is unpriced and the
honesty line says so; the partial-total rule; a foreign template's bound
component is counted for the borrowing design (adversarial test — the
cross-design walk); an explicit bom line and a bound leaf for the same
slug do NOT double count. Skill `precis-se-help`: one paragraph under the
BOM section for `view='order'`.

---

## Independent, unblocked, any time — **all three done**

7. **Unblock `structure` authoring** — gripe 330034, fixed by another
   session (in review 2026-09-19).
8. **`cad` unit declaration** — shipped as the units-policy cutover
   (c4b5292c, 2026-09-12): every length in the cad DSL carries its unit.
9. **`cad` intended-overlap declaration** — **SHIPPED 2026-09-19** as the
   `weld` source line (`precis.cad.scene` module docstring, "weld"
   bullet): undeclared penetration warns, a declared weld collapses to a
   count, an air weld warns.

---

## How to verify each slice

Same discipline that worked for `rxn` today, in order of what actually caught
things:

1. **Write the adversarial test first** where one exists — the cross-design
   cycle in slice 1 is the clearest.
2. **Diff-coverage gate**, no threshold override. On `rxn` it sat at 83 % and
   was *correct*: every `BadInput` guard was written and only some tested.
3. **Mutation pass** (`scripts/mutate-diff`). It found three survivors in
   `rxn` that review had verified *by reading* and were right — the code was
   correct but unpinned. Positional/index-mapping code especially.
4. **`--impacted` before ship.** On `rxn` it caught three real failures in
   files the change never touched.

## Sequencing note

Slices 1 and 2 are shipped; in the event slice 1 took its own migration and
slice 2 took none, its tables having ridden design-core's. 3–6 are
independently shippable (all six shipped as of 2026-09-18). **Do not bundle 7–9 into any of them** — a
refactor or schema change that also alters behaviour cannot be verified by
"the tests still pass", which is the whole reason the earlier blocktree work
stayed behaviour-neutral.

## Settled: the shared spine (absorbed from nm-se-shared-blocktree-core.md)

Both phases done 2026-09-07 (`28877919` se, `96690d37` nm): `nm` and `se`
ops now delegate to one `precis.blocktree` spine (tree, instancing + cycle
guards, ports, connects, envelope validation over the cad SDF kernel).
Line count went *up* ~150 and that is the honest result — the win is that
the spine exists once, not fewer lines. What survives as durable law:

- **What this is NOT — a kind merge.** `nm` and `se` stay two kinds, two
  tables, two dark-gate settings: same *IR*, different *domains*
  (bought-vs-made and Å-vs-m distinctions `nm-kind.md` refused to give
  up). The superset was of the implementation, never the vocabulary. nm's
  `expected_element`/`expected_hybridization`/`bound_design`/`bound_atom`
  deliberately stayed typed fields rather than folding into the core's
  `annotations` dict — a refactor that changes semantics cannot be
  verified by "the tests still pass".
- **Doctrine:** deduplication is justified by duplication you can measure;
  parameterisation is justified by users you can name. The
  unit-and-binding-parameterised superset stays unbuilt until a second
  *real* user exists; the parameterisation points (unit · binding
  provider · L2 vocabulary) are identified for that day. First named
  second-user datum: `nm-stick-placement.md` — its nm formfind bridge
  copies `precis_se/formfind.py`'s contract verbatim, and its
  features-as-fat-ports lean extends the spine's port vocabulary.
  Evidence toward the superset, not yet a decision for it.
- **`pcb` stays out** (Reto 2026-09-07) — it carries a circuit vocabulary
  (nets, copper, footprints, 11+ tables) and imports nothing from
  `precis.cad`; an instancing rhyme is too thin a thread. Do not
  re-litigate.
- **Naming:** the core type is `BlockNode`, not `Block` —
  `tests/test_vocab_lint.py` reserves the bare name.
- **Known wart, left deliberately:** `BlockNode.ports` is plain
  `dict[str, Port]`, not generic over its port type; nm's narrowing
  carries a scoped `# type: ignore[assignment]`. Widen to
  `BlockNode[TPort: Port]` only if a third domain needs its own port
  fields (`precis_nm/ops.py` cites this as "the phase 2 note").
- **Open gap, unowned:** containment is expressed two incompatible ways
  across the family — `cad`/`component` use `links` rows
  (`contains`/`part-of`), `nm`/`se`/`pcb` use an intra-ref FK
  (`parent_block_id`), so there is no single "what contains what" query
  and block trees are invisible to the links graph. Possibly the right
  trade (a links row per block is heavy), but it is undocumented —
  `docs/codebase.md` says nothing about this kind family.

## Open questions / decisions log

- 2026-09-18 readiness pass on slice 5: blocker — the precedent walk read
  transitions off the connect endpoint's own uid, which is empty for any
  instanced/array endpoint (transitions are template-owned); fixed above
  (resolve `node.template` first, adversarial test added). Verified clean:
  `store.get_ref` returns None for a missing rxn slug; `_render_drc` has one
  caller; validate does not merge drc findings; `info` severity exists and
  the drc header counts only error/warn; `'yield'` is the seeded prop_id;
  no import cycle for `precedent.py`.
- 2026-09-18 readiness pass on slice 6: three blockers, all folded into
  the revised spec — the walk must skip instance/array nodes and read
  binding/mode off templates with `design_occurrences` for qty (the
  `_render_fab` algorithm), resolving foreign templates through
  `tree.foreign` for the binding while keeping the local count;
  `order.rollup` takes the plain store (handler ↔ order would cycle);
  the empty cases get their own wording (bom's "nothing bought yet" line
  is gated on explicit bom lines, a different condition). Verified clean:
  `bound_kind`/`bound`/`mode` field names, component `category`/`mpn` in
  ref meta, `sync_realized_by` component-only, `bom.rollup(tree)`
  single-arg, occurrence helpers never touch `tree.foreign`.
