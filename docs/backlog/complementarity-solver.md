---
status: ready
pillar: 3d-design
title: complementarity solver — se bridge for the shipped structsolve core (stability section, validate findings, bistability view)
prio: high
---

# Complementarity solver — active-set unilateral analysis, sign-aware completeness, bistability probe

Owner of the mechanics per `multiscale-design-architecture.md`
§Complementarity; this item is the buildable slice plan. Subsumes
`structural-solution-space.md` rung 6 ("load path — build as
complementarity"): that rung's deferral note says when a consumer
actually wants the load path, build it as the complementarity solve
from the start, not a graph walk. This is that build.

**Status — all three slices shipped.** Core e49fb80a
(`src/precis/structsolve/complementarity.py` — `solve_complementarity`,
`check_completeness`, `probe_bistability`, array-level tests in
`tests/test_structsolve_complementarity.py`) and the se side
2026-10-13 (`src/precis_se/stability.py`'s bridge; see In scope for
pointers). **Genuinely open:** the screw-clamp fastener-preload →
must-contact pair wiring in the decisions log — se has no
`must_contact` vocabulary, so the `unseated` verdict is unreachable
from se today.

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

**Core (shipped) — `src/precis/structsolve/complementarity.py`:** the
module docstring is the contract (inputs, tension-positive convention,
status vocabulary, refusal posture, the documented active-set
algorithm choice, and the completeness/bistability honesty notes).

**Slice 1 — se bridge + taut/slack report — SHIPPED 2026-10-13:**
`stability.load_path_report` consumes `stability.axial_connects`,
`objectives.fixed`, `objectives.force` and `_assemble`'s
node-per-block poses and renders the `## load path` section of
`view='stability'` (undeclared-rate members skip-and-report; axial
subgraph only).

**Slice 2 — sign-aware completeness in se validate — SHIPPED
2026-10-13:** `stability.completeness_findings` maps culprit indices
to `a.port—b.port` subjects and names the case — folded into
`view='drc'`/`validate.validate` as the warn-first `completeness`
rule (unchecked paths are the honest "not checked" line at info tier).
Load cases are `args={'load_cases': ...}` at the view layer — decision
below.

**Slice 3 — two-equilibria probe, se side — SHIPPED 2026-10-13:**
`stability.probe_member_bistability` takes a member's `{trans, cis}`
free lengths (`args={'bistable': ...}` on `view='stability'`) and
surfaces `probe_bistability`'s result with its notes verbatim.
Endpoint verification only; the full switching-pathway sweep stays in
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

- (Shipped, core) the four array fixtures, units-agnosticism, refusal
  paths, completeness verdicts incl. culprit naming and "re-sign clears
  it", bistability probe — `tests/test_structsolve_complementarity.py`.
- Slice 1 bridge: `view='stability'` carries the taut/slack section
  with the honesty scoping notes; undeclared-rate members skip-and-report.
- Slice 2: an se design with a tie that any declared load case drives
  into compression yields a structured finding naming member subject and
  case; removing the case or re-signing the member clears it.
- Slice 3: a two-state member assignment with two stable equilibria
  reports both plus a barrier estimate; a monostable assignment reports
  one, honestly.
- No solver tunes its own objective; no silent least-squares answer
  where the complementary problem is infeasible — refusal with cause.

## Target + blast radius

Touched: `precis_se/stability.py` (bridge +
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
separation check is the two-member special case.

Still open:
- **Screw-clamp must-contact wiring — the explicit remainder.** The
  se bridge does NOT yet wire fastener preload (`joints.py` preload, N
  tension-positive) into the fastener-tie + face-stop pair above: se
  has no `must_contact` vocabulary (no axial param selects the idiom),
  no `screw`/`press` connect is decomposed into the member pair, and
  the `unseated` verdict is unreachable from se today. That wiring is
  what makes the general solve and the closed-form check agree by
  construction — still to build.
- **Load-case vocabulary — RESOLVED 2026-10-13 (built):** the
  view-layer list-of-cases argument, no objectives-schema change and no
  migration. `args={'load_cases': [{'name':..., 'loads': {'<block>':
  [x,y,z]}}]}` on `view='stability'|'drc'|'validate'` layers named cases
  on top of the implicit `'declared'` case the stored
  `objectives.force` field (block and connect) always makes; a named
  case's `loads` map is that case's complete per-block force field.
  Cases are read-time, so a case list is re-declared per call — a
  standard load-case library (spec §1.3) remains its own item.
- **Algorithm — RESOLVED in-code:** iterative active-set over linear
  solves with a visited-set cycling guard (module docstring); revisit
  only if a consumer needs Lemke-style pivoting.
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
