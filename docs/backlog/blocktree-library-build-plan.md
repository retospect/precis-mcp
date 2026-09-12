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

## Slice 1 — cross-design instancing (the library blocker)

**Why first.** Measured: `nm`/`se` `instance_block` resolves `template` to a
block *in the same design*. There is no cross-design path. Until there is, a
catalogued part cannot be placed and there is no library.

**Shape.** A template reference becomes *qualified*: either a bare local block
name (unchanged) or `<design-slug>#<block-name>`. Placement stays **by
reference** — an instance keeps resolving envelope/ports/dof from its template
at read time, so fixing a library part fixes every design using it. Do **not**
add an import-and-flatten; that is the thing that makes libraries rot.

**Touches.** `precis/blocktree/types.py` (template ref parsing),
`ops.py` (`_instance_shared`, `_op_instance_block`, `effective_ports`,
`effective_envelope`, `_find_instance_cycle`), plus one migration per plugin
namespace for the widened template column.

**The hard part, and the test that matters.** `_find_instance_cycle` currently
walks one design. Cross-design cycles are real (A instances B, B instances A)
and a naive resolver infinite-loops. **Write that test first**: two designs
that instance each other must be rejected with a message naming both, not hang.

**Also needs:** a resolver read-path decision — resolving a template now
requires loading another design. Cache per request; do not re-query per port.

**Done when:** a one-block design can be instanced into a second design, its
ports resolve, editing the source updates the consumer, and a cross-design
cycle is refused.

---

## Slice 2 — discrete block states + stimulus-labelled transitions

**Why here.** It is the one mechanism serving both photoswitches (`--[λ]-->`)
and assembly (`--[rxn]-->`). Everything downstream assumes it.

**Shape.**

    state:       (block, name, envelope?, port_pose_overrides?)
    transition:  (block, from_state, to_state, driver_kind, driver_ref, params)

`driver_kind` ∈ {`light`, `reaction`, `redox`, `ph`, `thermal`}. `driver_ref`
points at the thing that drives it — a `rxn` slug for a reaction, a wavelength
+ params for light. A block with no declared states has exactly one implicit
state, so **nothing existing changes shape**.

*Shared-states ruling (Reto 2026-09-12, → `design-state-core.md`):*
bistability is true macro AND nano (Howell-style compliant latches, hard
stops · photoswitches, conformers), so this slice's state/transition
tables land in the SHARED design-core home (`src/precis/design/`), not
nm-locally — this track builds them there as first consumer, schema
exactly as above plus `mechanical` added to `driver_kind` by migration
for the macro adopters. Per-block current state, no design-level
pointer. `design-state-core.md` verifies the macro rental fits; do not
add nm-specific columns. Slice-level ordering: slice 1 (instancing) is
free to go once units lands; THIS slice waits for design-core's package
scaffold (`src/precis/design/` + its core-migration chain) so the
states tables have their home — don't create the package from here.

**Copy cad's posing surface, do not invent one.** `cad` already has
`get(..., args={"state": {...}})` and `view='sweep'` ("does anything collide
anywhere in the travel"). The block analogue is `view='sweep'` over declared
*states* rather than a continuous joint range — same question, discrete domain.

**Schema.** *Revised 2026-09-07, before starting:* land slice 2 in its **own
migration**, separate from slice 1's. The original advice here was to share
one migration since the block table is reshaped either way — that is a false
economy. Migrations are forward-only and cheap; one change per migration is
far easier to verify and to reason about later, and slices 1 and 2 fail in
completely different ways. Two migrations.

**Done when:** a block can declare `{loaded, bonded}` or `{trans, cis}`, be
posed in either, probed for clash in each, and swept across all states.

---

## Slice 3 — complementary port roles

**Shape.** A role may declare a complementary partner:
`azide ↔ alkyne` both affording `CuAAC` in opposite senses. The connect gate
changes from *set intersection* ("both afford X") to *complementary halves*
("A affords the donor half, B the acceptor half"). Azide–azide becomes
illegal, which today it is not.

**Do not change the trust model.** It stays declared-intent labelling, never
chemistry proof. Keep the existing behaviour of naming the port's actual roles
in the rejection.

**Reuse the vocabulary already specified** for faces in
`nm-face-codes-and-scale.md`: "complementarity is elementwise
(donor↔acceptor, bump↔hole, +↔−)". Ports should use that, not a parallel one.

**Done when:** azide+alkyne connects, azide+azide is refused naming both roles,
and the existing `covalent`+`covalent` symmetric case still works.

---

## Slice 4 — ranked library search

**The surface.** Every attribute optional; results ranked by match count;
**per-attribute match/miss with the actual value** in the response. A bare
score is unusable — the judgement is *which compromise can I live with*.

    search(kind='nm', stimulus='light', bistable=True,
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

Slices 1+2 share a migration; land them together but as separate commits.
3–6 are independently shippable. **Do not bundle 7–9 into any of them** — a
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
