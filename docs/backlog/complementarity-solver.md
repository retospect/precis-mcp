---
status: ready
title: complementarity solver — active-set unilateral analysis, sign-aware completeness, bistability probe
prio: high
---

# Complementarity solver — active-set unilateral analysis, sign-aware completeness, bistability probe

Owner of the mechanics per `multiscale-design-architecture.md`
§Complementarity; this item is the buildable slice plan. Subsumes
`structural-solution-space.md` rung 6 ("load path — build as
complementarity"): that rung's deferral note says when a consumer
actually wants the load path, build it as the complementarity solve
from the start, not a graph walk. This is that build.

**Units-window split (2026-09-12):** slice 1's CORE (the pure
structsolve module + array tests) is outside the units-cutover
exclusive window and may build immediately; everything touching
`precis_se/*` (the bridge, views, slice 2) waits for
`units-policy-cutover` to land.

## Motivation / why

One formulation completes the unilateral family: per connection, either
the gap is zero and there is force, or there is a gap and zero force —
never both. Sign per load idiom (`multiscale-design-addendum-a.md` A6):
tension-only members (cable/tie — slack under compression),
compression-only (strut/contact — separates under tension), bidirectional
(the default, both signs legal), spring (bidirectional with a declared
rate — already the shipped slice-1 core's per-member axial rate, not a
new idiom), and hard stops (§Scenarios' must-contact — force required, a
stop that fails to seat is broken). A6's `slack_check` (verdict per load
case for a tension-only member) is what slice 2 below builds; overturning
and contact-patch-escape on compression-only stacks exceed this axial
model and stay future work (see Explicitly NOT in scope). Unilateral
constraints make stiffness *nonlinear in which members participate*: a
cable going slack changes the structure qualitatively, so "which ties
are taut under this load case" is an active-set solve over trial support
sets, not a graph walk and not one linear solve.

This complements, never replaces, the shipped machinery:
`precis_se/stability.py` (Pellegrino–Calladine `m − s` + second-order
test, `prestress_report`) answers "can prestress stabilize this
topology"; the complementarity solve answers "under *this* load case,
which members carry, and does every member stay on its legal sign".
The azobenzene tensegrity flagship (addendum A1) is the architectural
test: sign-blind completeness is one of the named wrong simplifications,
and the two-equilibria form of this solve is the bistable-actuation
analysis the flagship needs.

## In scope

**Core: `src/precis/structsolve/complementarity.py`** — house rules of
the package docstring: numpy/scipy only, pure functions over passed-in
arrays, no store access, unit-agnostic (se feeds metres/newtons, nm
feeds Å/nN; the numbers never know). Sign convention tension-positive,
matching `formfind.py` and `stability.py`. Inputs: node coords, member
incidence, per-member axial rate + free length (hence prestress
`k(L₀ − L)`), the capacity-pair sign idiom (tension-only /
compression-only / bidirectional / must-contact), per-coordinate fixed
mask (the `formfind` prescription convention), nodal load vector.
Output: equilibrium displacements, per-member force, and a per-member
status row — `taut` / `slack` / `bearing` / `separated` / `seated` /
`unseated` — plus a residual the caller can assert on, and a loud
refusal (the `FormFindError` posture) when no complementary equilibrium
exists in the small-displacement model.

**Slice 1 — active-set solve + taut/slack report on the existing axial
subgraph.** The se bridge consumes what already exists: member rows via
`stability.axial_connects` (role from the asymmetric capacity pair —
tie/strut/rod derived, never a third class), supports via
`objectives.fixed`, loads via `objectives.force`, node-per-block poses
exactly as `stability._assemble` scopes them. Report surfaces as a
section of `view='stability'` with the same honesty scoping notes:
axial subgraph only, non-axial connects and envelope contact not
modelled. Nonlinear stiffness is inherent: the solve iterates active
sets until the working set is sign-consistent (slack members carry zero
and have non-negative gap; active members carry legal-sign force), so
members dropping out is the mechanism, not a post-hoc filter.

**Slice 2 — sign-aware completeness wired into se validate.** Addendum
A6's delta: a tension-only member driven into compression (i.e.
required to carry compression for equilibrium to exist, or slack in a
way that leaves the remaining bilateral+active structure a mechanism)
under *any* declared load case makes the structure **incomplete — a
topology error, not a stressed member**. Structured rejection (principle
9): the finding names the member (`a.port—b.port` subject format) and
the load case. Lands as findings folded into `view='drc'` /
`validate.validate` the same way `preload_findings` and
`capacity_findings` already fold in. Tripwire contract respected: any
path that cannot run the solve emits the honest "not checked" line,
never a bare verdict.

**Slice 3 — two-equilibria / bistability probe.** Given two candidate
prestress/geometry states (in practice: the two free-length assignments
a photoswitch's `{trans, cis}` Δ(end-to-end) induces on one member),
verify each is a stable complementary equilibrium (solve converges,
second-order stabilized, all signs legal) and report an energy-barrier
estimate along a path between them. This is the bridge to photoswitch
tensegrity: two stable equilibria + a barrier = bistable actuation.
Endpoint verification here; the full switching-pathway sweep stays in
structural-solution-space slice 5.

## Explicitly NOT in scope

- Dynamics/vibration, friction, sliding or rolling contact — normal-gap
  complementarity only.
- Cable sag/catenary for slack members (already deliberately deferred);
  a slack member is simply force-free here.
- Rung 5's bolted-joint separation check stays closed-form two-member
  (its own bullet says: don't grow it into a private active-set solver —
  it may *later* call this one, not the reverse).
- 3D geometric contact detection for must-contact hard stops — cad owns
  gap geometry; this solver takes a declared gap/interface as an axial
  member with a compression-only idiom.
- Overturning and contact-patch-escape on compression-only stacks
  (addendum A6) — the load path staying inside the contact patch is a
  distributed-bearing question the axial model does not represent.
- nm consumption (state-dependent stability, slice 5 of the structural
  doc) — the solver must be *callable* from nm (unit-agnostic), but no
  nm bridge ships here.
- Masonry / multi-block frictional assemblies, large-displacement
  re-meshing, any store schema for a load-case library beyond what the
  open question below resolves.

## Acceptance criteria

- Pure-array unit tests, no store: (a) two-cable + strut triangle where
  one cable goes slack under a lateral load — status rows flip
  taut→slack and forces match hand calc; (b) compression-only contact
  that separates under uplift; (c) a must-contact stop reported
  `unseated` when preload is insufficient; (d) a classic tensegrity
  (e.g. 3-strut prism from `prestress_report`'s test fixtures) where all
  ties stay taut under a small service load.
- Same problem fed in metres/newtons and Å/nanonewtons produces
  identical status rows (unit-agnosticism is tested, not asserted).
- Slice 2: an se design with a tie that any declared load case drives
  into compression yields a structured finding naming member subject and
  case; removing the case or re-signing the member clears it.
- Slice 3: a two-state member assignment with two stable equilibria
  reports both plus a barrier estimate; a monostable assignment reports
  one, honestly.
- No solver tunes its own objective; no silent least-squares answer
  where the complementary problem is infeasible — refusal with cause.

## Target + blast radius

New: `src/precis/structsolve/complementarity.py` (+ `__init__` exports).
Touched (post-units-window only): `precis_se/stability.py` (bridge +
view section), `precis_se/validate.py` / `precis_se/drc.py` (slice-2
findings), `precis_se/handler.py` (view plumbing), package docstrings,
and the two owning backlog docs (architecture §Complementarity gains a
"built" status; structural-solution-space rung 6 closes).

## Open questions / decisions log

Ruled 2026-09-12 (main loop): slice-1 report lives as a section of
`view='stability'` (no new view); members with undeclared rate are
skip-and-report (stability's posture, honesty note included); slice-2
rollout starts warn-first like `prestress_state`, hardening to error
once the flagship dogfood exercises it.

Slice-1 shipped-core note (review, 2026-09-12): the solver's `residual`
certifies the linear solve only — it is NOT an independent physics
check (a systematic assembly-sign bug would still show residual ≈ 0;
self-disclosed in the module docstring). The se-bridge honesty note
MUST NOT present `residual < eps` as "physics verified".

Screw-clamp mapping (Reto Q 2026-09-12, answered): a screwed/bolted
clamp is the canonical must-contact producer — the fastener is a
tension member carrying preload, the clamped faces are its
complementary compression-only/must-contact pair carrying the
balancing compression. `seated` while service tension < preload;
service load exceeding preload drives the face force to zero →
`unseated` = joint separation. The shipped closed-form rung-5
separation check is the two-member special case; the se bridge wires
fastener preload (`joints.py` preload, N tension-positive) into
exactly this pair so the general solve and the closed-form check agree
by construction.

Still open (non-blocking for slice-1 core):
- **Load-case vocabulary.** `objectives.force` today is one load per
  block — "under any load case" needs named cases (shock included).
  Extend the objectives vocabulary, or a list-of-cases argument at the
  view/op layer only? Blocker for slice 2, not slice 1.
- **Algorithm.** Iterative active-set over linear solves vs. posing the
  LCP/QP to `scipy.optimize`; cycling guards. Decide in-code with a
  documented choice; flag if Lemke-style pivoting is wanted.
- **Slice-3 barrier estimate — RESOLVED 2026-09-12 (built, reviewed):**
  linear interpolation of free lengths + endpoint displacements, with a
  proven consequence disclosed in the code's notes tuple: the summed
  unilateral energy is convex in the path parameter, so in this
  small-displacement model the estimate is *always exactly* |E_a − E_b| —
  interior samples add nothing. The flagship must NOT read it as a
  transition-difficulty screen; a real snap-through barrier needs the
  geometrically nonlinear sweep (`structural-solution-space.md` slice 5).
  Second-order stability check discriminates from mere solve
  convergence (eigvalsh of reduced tangent stiffness, scale-relative);
  `ComplementarityInputError` propagates caller bugs while genuine
  no-equilibrium refusals absorb into the per-state result.
