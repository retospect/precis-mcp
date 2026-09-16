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

Slices 7–9 are independent and can go any time by anyone.

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

Known gap, tracked in **gr342026**: `port_pose_overrides` is direction-only
(`{port: {'direction': [x,y,z]}}`), because `Port`/`PortSpec` has no
absolute position field. A state can re-aim a port but not move it. Settle
this before a consumer bakes in direction-only semantics.

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

---

## Slice 5 — rxn-driven transitions + precedent DRC

The `bonded` state's geometry is the *product of a reaction*. Point the
transition's `driver_ref` at a `rxn` slug, then the precedent read shipped
today answers "does this joining chemistry work on substrates like mine, and
at what yield": `search(kind='rxn', property='yield', reaction_class=…)`.

**New DRC check:** a connect whose declared joining reaction has **no
precedent** is flagged — the same "unprecedented step" signal
`reaction-kind-and-synthesis-cost.md` already defines for routes. Not an
error; a flag with the evidence count.

---

## Slice 6 — `realized-by` → `component`

Block → the purchasable thing. The edge already exists (`se` uses it), so this
is wiring plus a `view` that answers "what do I order" by walking the
instanced tree to purchasable leaves. Mirrors the BOM rollup, including its
honesty line ("priced: N of M").

---

## Independent, unblocked, any time

7. **Unblock `structure` authoring** — gripe 330034. The whole atoms leg is
   unreachable from the MCP client until fixed. Infrastructure, not design,
   but it gates the bottom of the three-level chain.
8. **`cad` unit declaration** — removes the "1 mm = 1 nm" fiction and makes
   probe output self-describing.
9. **`cad` intended-overlap declaration** — a welded 14-part cage emits 36
   interference warnings, all intended, drowning any real one. (Gripe 330182
   covers the doc half.)

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
independently shippable. **Do not bundle 7–9 into any of them** — a
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
