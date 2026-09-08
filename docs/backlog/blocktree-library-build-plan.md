---
status: ready
title: build plan — cross-design instancing, block states, complementary ports, ranked library search
prio: high
model: opus
---

# Build plan

Companion to `functional-block-library-and-assembly-states.md` (the what and
why) and `photoswitch-states-and-spectral-dof.md` (the physics). This is the
how: slices, order, and what each one has to prove.

**Preconditions that make this cheap.** Every `nm`/`se`/`pcb` table is empty
in prod (measured 2026-09-07), Reto has lifted backward compatibility, and the
shared spine `precis.blocktree` already exists (commits `28877919`,
`96690d37`) so each change below lands **once** and serves both kinds.

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
