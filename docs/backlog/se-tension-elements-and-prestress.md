---
status: draft
title: se tension elements and prestress — unilateral constraints, springs, and why tensegrity breaks the DOF probe
prio: high
model: opus
---

# se: ropes, springs, and structures that only stand up because they're tight

Design session 2026-09-05 (Reto + agent), precious-juggling-map worktree,
from Reto's question: *"Do we understand springs and ropes
(tensegrity)?"*

**Today: no.** And the honest version of that answer is worse than a
missing vocabulary entry — the current model does not merely *lack* these
elements, it would **confidently give a wrong answer** about a structure
built from them. That is the reason this is `prio: high` rather than a
nice-to-have.

## What's actually missing, in increasing order of depth

### 1. Every kinematic class in the tree is bilateral

`joints.KINEMATIC_CLASSES` is `rigid | revolute | prismatic |
cylindrical | screw | planar | ball | compliant | captive`. Every one is
a **bilateral** (two-sided, equality) constraint: `prismatic` permits
translation along an axis and forbids everything else, symmetrically.

A rope is a **unilateral** (one-sided, inequality) constraint:
`|a − b| ≤ L`. It resists being pulled apart and does nothing at all when
pushed. There is no way to say that today, and no amount of composing
existing classes gets there — inequality is not expressible as a
conjunction of equalities.

The general concept, which is what should be added rather than "rope":

- **`tie`** — tension-only. Distance ≤ free length. Rope, cable, chain,
  belt, webbing strap, a bolt considered as a clamp.
- **`strut`** — compression-only. Distance ≥ free length. A prop, a
  bearing contact, any interface that separates rather than pulls.

`strut` matters as much as `tie` and is the one people forget: an
unpreloaded bolted joint carries compression through the *interface*, not
tension through the bolt, and "does this joint gap open under load" is a
unilateral question.

### 2. `compliant` exists but is inert, and a spring is not a flexure

`compliant` is documented as "motion with stiffness rather than freedom
(flexure)", with `params.stiffness` explicitly at the *descriptive* tier
— "advisory/descriptive until a real consumer exists". So the field
exists, nothing reads it, and the word means a living hinge.

A coil spring is a different animal in three ways:

- it has a **free length** and therefore a **preload** — the force at
  installed length is `k(L₀ − L)`, and a spring installed at free length
  does nothing;
- it has **hard limits** — solid length (coils touch, stiffness → ∞) and
  max deflection (yield). Both are hard-tier DRC facts, not penalties;
- it is a **procurable component**, with a catalogue: rate, free length,
  solid length, wire Ø, OD, ends. `component`'s seeded categories are
  `fastener | hose | pipe | profile | electronic | adhesive | seal |
  bearing | fitting | laminate` — **there is no `spring`**.

So a spring needs all three layers: a kinematic class (or a mechanism), a
component category with specs, and a consumer that reads the rate.

### 3. Tensegrity breaks the DOF probe — this is the dangerous one

A tensegrity is a structure that is rigid **only because it is
prestressed**. Discharge the pretension and it is a mechanism: it flops.
That is not an edge case, it is the definition.

The consequence for se is specific and bad. `relate.translational_dof`
walks the joint graph and reports mobility. Handed a tensegrity mast it
would report **"mobile — this is a mechanism, it will collapse"**, which
is:

- **correct** as first-order linear kinematics, and
- **wrong** as an engineering answer about a structure that is standing
  up in the room.

A tool that is confidently wrong about a real structure is worse than one
that declines to answer. At minimum the probe must learn to say *"first-
order mobile; may be prestress-stabilized — not checked"* rather than
"collapses".

The established theory is not exotic and is implementable with numpy:

- **Maxwell's rule, as extended by Calladine**: `m − s = 3j − b − c`,
  where `m` = independent inextensional mechanisms, `s` = independent
  states of self-stress, `j` = joints, `b` = members, `c` = kinematic
  constraints. A structure with `s ≥ 1` has a prestress state available.
- Both counts come from the **equilibrium matrix**: `m` and `s` are the
  dimensions of its left and right null spaces, i.e. one SVD.
- Whether a given first-order mechanism is actually **stabilized** by a
  given self-stress state is the second-order (product-force /
  geometric-stiffness) test — Pellegrini-Calladine. Also linear algebra
  once the null spaces are in hand.

That gives a real, bounded deliverable: **classify a structure as rigid /
mechanism / prestress-stabilized**, with the self-stress state reported.

### 4. Form-finding is a different problem — and is out of scope

*Checking* a declared tensegrity (given geometry and force densities, is
it in equilibrium, and is it stable?) is linear algebra, as above.
*Finding* the geometry — force-density method, dynamic relaxation — is a
nonlinear optimization and a research area. **Not in scope.** Named here
so it is not re-derived as an obvious next step: se is a specification and
checking kind, and the pattern everywhere else (cad kernel analytic, DRC
declarative) says the solver belongs outside.

## Why this is not just "add two enum entries"

Adding `tie`/`strut` to `KINEMATIC_CLASSES` is ten minutes and would be
misleading on its own, because **unilateral constraints change what every
downstream check means**:

- **DOF / mobility** — a unilateral constraint is active or inactive
  depending on the load. Mobility becomes load-dependent, which the
  current probe has no concept of.
- **Load path** — you cannot ask "what carries this force" without
  knowing which ties are taut. This is the classic slack-cable problem
  and needs a per-load-case active-set solve, not a graph walk.
- **Stack-up** — a taut tie's length is its unstretched length plus
  `F/k`, so tolerance and load stop being separable. The
  `se-feasibility-and-cost.md` stack-up chain assumes rigid links.
- **Assembly (DFA)** — this is the sharpest interaction, and it is a real
  result, not a caveat. A tensegrity **cannot be assembled by inserting
  one member at a time against a rigid partial assembly**, which is
  exactly the "assembly-order existence" check
  (`se-off-the-shelf-fabrication.md` engine 2, and rung 5 of
  `se-feasibility-and-cost.md`). Every intermediate state is a mechanism.
  It requires simultaneous tensioning, or a jig, or a deliberately
  designed tensioning sequence. **The existence check must not report
  "cannot be assembled" for a prestressed structure** — it must report
  "requires simultaneous assembly / a tensioning sequence", which is a
  *cost* (a jig, and a lot of pain), not an infeasibility. That is
  precisely Reto's "working solution but bad" tier, and it is the best
  test case the feasibility item has.

## The unification: one axial member, an asymmetric capacity pair

Reto, 2026-09-05: *"Can we improve to support compression/clamping/
tension only mechanisms? These are also translating to bond strain I
suppose?"* — yes to the first, **half** to the second, and the correction
is what makes the design small.

### Don't add `tie` and `strut` as two primitives — derive them

The temptation is two irreducible classes. The better model is **one
axial member carrying an asymmetric capacity pair**
`(tension_capacity, compression_capacity)`, plus a free length and a
rate. Everything we care about is then one parameterization:

| element | free length | rate | tension cap | compression cap |
|---|---|---|---|---|
| rope / cable | L₀ | EA/L | breaking load | **→ 0** |
| strut / contact interface | L₀ | EA/L | **0** | crush / bearing |
| coil spring | free length | k | to solid/max defl. | to solid length |
| rigid link | L₀ | → ∞ | member strength | Euler `P_cr` |
| preloaded bolt | grip | EA/L | proof load | (via the interface) |

**Tension-only is not a constitutive property — it is a slenderness
limit.** A rope carries no compression because `P_cr = π²EI/L²` goes to
zero as `L` grows. That is the same Euler formula `precis_nm` already
computes (`mechanics.euler_buckling_ceiling_nN`). So `tie` is not a new
kind of thing; it is *the limit of an axial member whose buckling
capacity is negligible against its tensile capacity*, and the model
should say that rather than hardcode a special case. `strut` is the
mirror: an interface that cannot pull.

This matters practically: it means a **slender rod** — a real case that
sits between rope and rigid link, tension-strong and compression-weak but
not zero — is representable without a third class, and the DRC can say
"this member is being asked to carry 400 N in compression against a
120 N buckling ceiling" rather than "this isn't a rope".

### Clamping is composed, not primitive — and it is the best case

A bolted joint is **tie + strut + preload**: the bolt is a stiff
tension-only member, the clamped stack is a much stiffer
compression-only one, and preload puts the first in tension and the
second in compression. External load then **shares between them by
stiffness ratio**, and the joint *gaps* when the external load overcomes
the preload. That is the classic bolted-joint diagram, and it needs no
`clamp` primitive at all — only the two unilateral members and a preload.

It is also where three backlog items meet, which is the argument for
building preload early:

- `se-off-the-shelf-fabrication.md` rung 3 computes **grip stack-up** —
  the *geometric* half of exactly this joint;
- this item supplies the **force** half (preload, sharing, separation);
- `se-feasibility-and-cost.md`'s reliability tier gets its sharpest real
  result: a correctly preloaded bolt sees almost no cyclic stress because
  the interface absorbs it, while an underpreloaded one sees the full
  range and fails in fatigue. Same geometry, same parts, different
  preload — one works and one doesn't, and nothing in the tree can
  currently tell them apart.

### Bond strain: the shared abstraction is the SPRING, not the tie

Reto's instinct connects this to the atomic scale, and the connection is
real, but it lands one entry to the left:

- A **covalent bond is bilateral and asymmetric** — a steep Pauli
  repulsive wall in compression, a softer attractive branch in tension up
  to dissociation (Morse, not Hooke). It resists *both* directions. It is
  a **spring with a rupture limit**, which is exactly the harmonic-plus-
  cutoff form MM force fields use, and exactly what
  `precis_nm.mechanics.harmonic_strain_energy_eV` already applies over
  declared-bond angle triples.
- The genuinely **unilateral** atomic-scale element is **not** the bond —
  it is **non-bonded steric / van der Waals contact**, which pushes and
  cannot pull. That maps to `strut`, cleanly.
- **Tension-only has no clean atomic analogue**, for the reason above: it
  is a slenderness artifact, and a bond is not slender.

So the shared vocabulary across the two scales is:

| se (macro) | nm (atomic) | status |
|---|---|---|
| spring / axial rate | harmonic bond + angle terms | **nm: built** (`harmonic_strain_energy_eV`) |
| tension capacity | min-cut bond-rupture ceiling | **nm: built** (`mechanics.min_cut`) |
| compression capacity | Euler buckling of a tube | **nm: built** (`euler_buckling_ceiling_nN`) |
| unilateral contact (`strut`) | steric / vdW repulsion | neither |
| preload / prestress | — (no atomic analogue in scope) | neither |

**`precis_nm` already computes the tension/compression asymmetry that se
lacks**, as two separate closed-form ceilings, with the honesty caveat
this codebase requires (pristine-lattice ceilings, not predictions). se
is the scale that is *missing* it, not the one that needs to invent it.
Whether the two share code or only share the *shape* is an open question
below — the formulas are identical, the units and the honesty tiers are
not, and `se : cad :: nm : structure` says these are siblings rather than
one thing.

## Sketch of the work

1. **The axial member with an asymmetric capacity pair** (above) —
   `tie`/`strut` as its two limits rather than two primitives, with free
   length, rate, and the two capacities. Plus the honest degradation:
   every existing check that assumes bilateral either handles it or
   *declares that it does not* (the suggestive-by-contract posture —
   report absence, never silently assume taut).
2. **Teach the DOF probe to say "may be prestress-stabilized"** instead
   of "mobile". Cheapest fix with the largest correctness gain: it
   converts a confidently-wrong answer into an honest one, and it can
   land before any of the analysis below.
3. **`spring` component category + specs** (`spring_rate` N/m,
   `free_length`, `solid_length`, `max_deflection`, `wire_diameter`;
   `outer_diameter` is already universal from migration 0152) and a
   `spring` mechanism with `demands_bom` — a spring joint with nothing to
   buy is a drawing, same rule as `bearing`. Series data: DIN 2098 /
   2095 exist for compression springs, so the
   `component_series.json` mint path applies directly.
4. **Equilibrium matrix + null spaces** → `m`, `s`, and the
   rigid / mechanism / prestress-stabilized classification. Self-contained
   numpy; the largest single piece.
5. **Preload / prestress as a declared facet** — member preloads, the
   bolted-joint load-sharing and separation check (the highest-value
   single result here, and the one that makes rung 3's grip stack-up
   mean something), and the DRC that a declared self-stress state is
   compatible with the geometry (i.e. lies in the null space, within
   tolerance).
6. **Active-set load path** for a given load case, so "which ties are
   taut" is answerable. Last, and only if a consumer wants it.

Rungs 1–3 are independently useful and none depends on 4. Rung 2 alone is
worth shipping early on the "stop being confidently wrong" argument.

## Deferred, named so they are not re-derived

Form-finding (above); cable sag / catenary under self-weight (a rope
spanning a distance is not a straight line, and pretending otherwise is
fine for a taut tie and wrong for a slack one — declare the assumption);
dynamic response and vibration of prestressed structures; creep and
stress relaxation in synthetic rope (real, material-dependent, and a
`material` question before it is an se one); belt/pulley ratios (already
deferred in `joints.py`'s unknown-key error text as a coupling se cannot
represent).

## Open questions for Reto

- **Is `tie` a kinematic class or a mechanism?** It is genuinely both —
  unilateral-distance is a *class* (what motion it permits), rope vs
  chain vs belt is a *mechanism* (how it is realized). My lean: class
  `tie`/`strut` for the constraint, mechanism `rope`/`chain`/`belt` for
  the realization, matching the existing two-axis split.

  **`belt`/`chain` collide with a coupling meaning — and the two-axis
  split is what resolves it** (raised 2026-09-05 by the cad track, whose
  design language already ships a bare `belt` *coupling* keyword,
  `gear <a> to <b> ratio:r` / `belt …`). Three senses of one word were
  in play:

  1. cad's DSL keyword `belt` — a **coupling**: two joint states linked
     by a ratio. Shipped, cad slice 3.
  2. se's *deferred* coupling — the same concept, and `joints.py`'s
     unknown-key error already says so verbatim: "couplings between
     separately-mounted parts (gear/rack/belt ratios) are not yet
     representable".
  3. this item's proposed mechanism `belt` — a flexible **tension
     member**.

  The tempting fix is to rename (3). **Don't** — that would be the wrong
  correction, because senses (1) and (2) are the *same* concept and (3)
  is genuinely different, so renaming (3) hands the good name to the
  half that doesn't need it. The right resolution is the two-axis rule,
  stated as a rule rather than left implicit: **the mechanism names the
  physical article; the class names what it does.** Then `belt` +
  `tie` is a tensioned belt used as a tie, and `belt` + a future
  `coupling` class is a belt drive — one article, two behaviours, no
  ambiguity, and a real belt drive is honestly both at once. Same for
  `chain`: a chain sling is `chain` + `tie`; a chain drive is `chain` +
  `coupling`.

  Namespaces also differ (a cad DSL keyword is not an se mechanism enum
  value), so this is a *reader* hazard rather than a collision — but it
  is worth one line wherever either vocabulary introduces the word, and
  it is the argument for se's eventual coupling class reusing cad's
  `belt`/`gear` spelling rather than coining a third.

- **Ownership, settled 2026-09-05:** `relate.translational_dof` is not
  in the cad track's scope (slices 3–5 shipped without touching it and
  no plans on it), so the "first-order mobile; may be
  prestress-stabilized — not checked" honesty fix belongs to this item.
- **Is the tensegrity classifier worth building before a real design
  needs it?** My lean: build rung 2 (the honest "may be
  prestress-stabilized") now regardless, defer rung 4 until a design
  exists — but do not let the probe keep saying "collapses" in the
  meantime.
- **Do se and nm share the axial-capacity code, or only its shape?**
  `precis_nm.mechanics` already has Euler buckling, a min-cut tension
  ceiling and harmonic strain; the formulas se needs are identical. But
  the units differ (nN/eV/Å vs N/J/m), the honesty tiers differ (nm's are
  *advisory pristine-lattice ceilings that never gate*; se's compression
  check is meant to be hard-tier DRC), and `se : cad :: nm : structure`
  frames them as siblings, not one implementation. My lean: **share the
  shape, not the module** — a common documented formula reference, two
  callers, no premature abstraction over two consumers. Revisit at three.
