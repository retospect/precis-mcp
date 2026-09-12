---
status: draft
title: multiscale design system — the detailed build spec (data model, constraint catalogue, optimiser, MCP surface, molecular tier)
prio: high
model: opus
---

# Ownership routing (added on intake, 2026-09-11)

Reto's system spec, taken in verbatim below (was briefly at the repo
root). This is the *detailed* leg doc under the map
`multiscale-design-architecture.md`; the map's concept→owner table stays
the router. Precedence: where a concept already has a shipped or spec'd
owner, **that owner wins** — the section here is background plus the
named deltas; never re-derive or fork. Build slices are carved from here
when promoted (house pattern), not pre-sharded into backlog items.

Owned elsewhere (read the owner first):

- **§1.1–1.2, §1.6 identity / core objects / phases** → the shipped `se`
  block tree + L0–L5 ladder (`se-kind.md`). Deltas held here:
  six-component contract with absent = *unknown* never zero,
  `load_provenance`, `insertion_direction`, bounds-with-reason,
  DB-minted IDs (se uses caller-chosen names today).
- **§2 constraint catalogue** — rows with existing owners: soft-cost /
  severity registry `se-feasibility-and-cost.md` (extend it; never a
  second hard/soft boundary), tool access + fasteners
  `se-off-the-shelf-fabrication.md`, swept-volume scenarios + the
  three-verdict rule table map §Scenarios, unilateral members map
  §Complementarity + `structural-solution-space.md`.
- **§2.5 margins** — allocation mechanics → `margin-budget-tree.md`
  (rides the estimate kind); this doc owns *where* margins apply and
  `margin_origin` provenance.
- **§3 optimiser** — map §Optimisation stack owns the invariants,
  including the house guard (objective weights human-set; BO schedules
  evaluations, never tunes weights — §3.3's internal-weighting risk note
  is subordinate to that). Held here: the refinements (Chebyshev,
  tempering/niching, hierarchical Pareto fronts §3.4, solve provenance
  §3.8).
- **Units/tolerances** — map §Units policy is DECIDED (ingest-any → SI
  internal → e-notation display; relative-default tolerances); §3.4's
  three tolerance meanings are consistent with it.
- **§4.5 joins** → map §Requirement→joint matching +
  `se-off-the-shelf-fabrication.md` (aligned, no conflict).
- **§4.8 PCB** → the `pcb-*` docs. Deltas here: metal-core boards,
  closed-loop multiphysics escape hatch.
- **§4.9 molecular tier** — fragment library/states →
  `blocktree-library-build-plan.md` (adopt this doc's brick/linker
  vocabulary there), π systems + interaction terms →
  `nm-stick-placement.md`, switching physics →
  `photoswitch-states-and-spectral-dof.md`. Held here: the `density_at`
  fidelity ladder, conformer enumeration, degradation/cleavage records,
  strain-as-utilisation language.
- **§5.3 verb surface** — the shipped se op surface (`put`/`edit` +
  `add_block`/`connect`/`set_load`/…) is the surface of record; read
  §5.3 as the *capability list* (pin→branch, checkpoint/restore,
  structured Rejection + retry budget, named attachment points), not a
  rename.
- **§5.8 viewer** → gr335242 (se/nm web reader spec + 5 addenda) is the
  build item; this section's additions (three-cad-viewer, exploded view,
  drawn connectivity, linked mermaid panel, stick-figure checkpoint) are
  merged there by comment.

Second intake (2026-09-12): the summary-derived material once integrated
here as *(added 2026-09-12)* sections is superseded verbatim by
`multiscale-design-addendum-a.md` (A1–A10) — reconciled 2026-09-12; its
preamble carries the routing. Removed from this file: §0.1.1
(→ A1), principles 11–12 (→ A2), the §1.3 lifecycle-situation paragraph
(→ A3), §2.6–2.7 (→ A4, A5), the §3.1 BO paragraph (→ A7), §4.2.1 (→ A6),
the §4.4 process additions (→ A8), the §4.9 non-bonded/ratchet/hysteresis
block (→ A9), two §6.2 items (→ A10).

Owned HERE (new subsystems; the map's table points at these): scenario
presets + service environment incl. the lifetime master switch and the
standard load-case library (§1.3) · per-number provenance (§1.4) ·
envelope revisions / copy-on-write design state / pin→branch comparison
(§1.5, §5.6) · the phase loop with envelope tightening (§1.6) ·
cross-domain coupling screen (§2.4) · termination nodes + representation
escalation (§4.2–4.3) · process repair/projection, composition, deferred
commitment (§4.4) · lattice-placement preference (§4.6) · catalogue
ingestion layers + availability preference (§4.7) · microfluidic cards +
radiometric transport (§4.8) · inspection toolkit — cast_ray /
max_stress / under_utilised / digest / bookmarks (§5.4–5.5) ·
build-vs-inspect hint asymmetry (§5.2) · job `searching`→`improving`
status (§5.7).

---

# Multiscale Geometry & Optimisation Model — Build Specification

**Status:** architecture complete, physics partial. Intended to be sliced into buildable tranches.
**Scope:** one representation and one solver stack spanning coarse functional blocks down to atomic detail, serving both manufactured mechanical assemblies and molecular design.

---

## 0. Scope and principles

### 0.1 What this is

A design system in which a human or an LLM states *intent* — requirements, loads, service environment — and a solver produces a manufacturable (or synthesisable) design, which can then be inspected, argued with, and re-solved under pinned constraints.

The unifying claim is that the mechanical and molecular scales are the same problem: a graph of blocks joined by interfaces, each block terminating at some level of physical detail, each interface carrying a contract that must be satisfied from both sides.

### 0.2 Standing principles

These recur throughout and should be treated as invariants.

1. **Everything is constrainable, nothing is required.** Any parameter the solver would choose can be pinned by user or LLM, with a stored reason. Control level is per-parameter, from "make me a unicycle" to a fixed tube diameter.
2. **Requirements at the boundary.** Blocks declare what they need *of* their neighbours through the interface contract, not how it is achieved. Realisation is deferred as long as it is affordable to defer.
3. **Earliest evaluable phase.** Every constraint is evaluated in the earliest phase where enough is known. Early constraints prune branches; late constraints only reject finished candidates.
4. **Terminate aggressively.** Most branches stop at bulk properties. A termination node carries effective properties, a validity range, and the statistical character of what lies beneath it.
5. **Termination is per physics domain.** The same lens terminates at an opaque solid with bounds for structural and thermal, and at the thin-lens relation for beam transport. No ray is traced through the glass.
6. **Provenance on every number.** Which solver, what fidelity, under which assumptions, from which library version, user-stated or LLM-assumed.
7. **Layer, do not overwrite.** A higher-fidelity result is cached alongside the cheap one. The query returns best-available; the disagreement between rungs is a calibration signal.
8. **Soft gates, not hard ones.** Feasibility is a signed, graded violation field — steepest outside spec, flattening inside — with a boolean illegality flag used only as a cheap early-out to skip expensive physics.
9. **Reject with a structured reason.** Never a bare `invalid`. The reason names the missing or violated item so the caller can resubmit or the search can retarget.
10. **No test programme.** Generous engineering margins substitute, justified by well-characterised processes and by buying (not fabricating) precision-critical parts. Every margin is tagged with its origin so a margin audit is a query.

Principles 11–12 (limited component DOF; meshing-controlled generated
geometry) are Addendum A2 (`multiscale-design-addendum-a.md`).

### 0.3 Vocabulary

Distinct physics had converged on shared names. These are disambiguated deliberately:

| Use | Not |
|---|---|
| radiometric transport (lens, beam, irradiance) | "optics" for anything non-ray-traceable |
| dipole coupling / energy transfer (near-field) | "optical coupling" |
| electromagnetic field / signed-distance field / strain field — always qualified | bare "field" |
| bond breaking by light, isomerisation, light-induced ageing, beam transport | the "photo-" prefix |
| mechanical stress vs. service conditions | macro/micro prefixes |
| bond deformation vs. continuum strain | "strain" unqualified across scales |
| cross-domain screening vs. chemical reaction | "interaction" |
| applied force vs. compute cost | "load" unqualified |
| **brick** (substantive moiety), **linker** (small connector), **attachment point** | reusing "block"/"interface contract" for chemistry |

"Interface contract" stays reserved for the mechanical sense, which carries stiffness, load and tolerance. A chemical attachment point carries none of those and is named differently.

---

## 1. Data model

### 1.1 Identity

- IDs are **minted by the database**, never supplied by a caller. An LLM submits a human label (`"saddle post"`) and receives the minted ID back.
- A stable ID runs parallel to the transform array with bidirectional lookup. Index order is never load-bearing.
- The same minted ID is used as the leaf name in the viewer's path scheme (`/frame/fork/dropout_left`), so a viewer pick returns the ID directly with no lookup table to drift.

### 1.2 Core objects

```
Design
  id, label, parent_branch_id?, scenario_id, created_at
  blocks[]           -> Block
  interfaces[]       -> Interface
  phase              enum{topology, sizing, realisation}
  envelope_revision  int          # see §1.5
  status             enum{searching, improving, complete, failed}

Block
  id, label, type
  params_declared{}    # LLM/user intent, e.g. tube length
  params_solved{}      # physics-chosen, e.g. diameter, wall thickness
  bounds[]             # optional min/max on any solved param + reason + origin
  material_class       enum{metal, plastic, printed, elastomer, ...} | committed material
  representation       enum{solid_primitive, infilled_shell, branch_tree, mesh, terminated}
  termination[]        -> TerminationNode      # one per physics domain
  attachment_points[]  # named, not coordinates
  insertion_direction  # permitted assembly direction cone
  library_ref?         # catalogue part + pinned version

Interface
  id, block_a, attach_a, block_b, attach_b
  joint_kind        enum{rigid, revolute, prismatic, snap, ...}   # topology-level
  realisation?      enum{screw, clip, rivet, weld, adhesive, press}  # phase 3, pinnable
  contract          -> Contract
  dof_constrained   int 0..6
  demountable       bool
  may_be_destroyed  bool          # end-of-life disassembly

Contract
  loads{Fx,Fy,Fz,Mx,My,Mz}   # ALL SIX. absent = unknown, never zero
  load_provenance            enum{user_stated, llm_assumed, derived_by_statics}
  stiffness, tolerance_band
  thermal{}, electrical{}, chemical_compatibility{}   # domain blocks as applicable
  validity_range
```

Blocks and their fillings are **separate records** linked by the interface contract, so requirements live at the boundary and recurse down the hierarchy.

### 1.3 Scenario and service environment

A single named top-level scenario sets objective weights and quantity, rather than weights being hand-tuned per run.

```
Scenario
  name           enum{prototype, small_batch, mass_production}   # extensible
  quantity
  objective_weights{}
  service_environment -> ServiceEnvironment
  load_cases[]        -> LoadCase

ServiceEnvironment
  expected_lifetime          # MASTER SWITCH — scales whole families of terms
  temperature_range, pressure, humidity
  vibration_spectrum
  chemical_exposure[]        # salt, solvents, oil, UV
  cycle_count                # distinct from calendar time
  duty_cycle
```

`expected_lifetime` is the master switch: a week-long prototype drops corrosion, creep and fatigue entirely; fifty years subsea makes them dominant. `prototype` and `subsea_50yr` are presets that reconfigure which physics runs at all.

A **standard load-case library** (shock, vibration, off-axis loading) is applied by default and must be explicitly exempted, so designs that only work statically are caught.

Lifecycle situations (n-not-three, operational 4D motion, deliberate
hard-stop contact, compare-by-volume) are Addendum A3
(`multiscale-design-addendum-a.md`), built out in
`situation-rule-tables.md` — §1.3's `Scenario` object above is the
production context, a different thing from a *situation* in the ruled
vocabulary.

### 1.4 Provenance

Every stored number carries:

```
Provenance
  source      enum{user_stated, llm_assumed, derived, solver, library, measured}
  fidelity    enum{template, analytic, surrogate, full_solve, dft, high_method}
  solver_id, assumptions[], library_version?
  margin_origin?   # which margin rule contributed
```

This makes the margin audit, the fidelity ladder and the library-update notification all queries rather than features.

### 1.5 Versioning

Two independent axes, both needed.

**Envelope revisions.** The phase loop tightens envelopes when a late failure invalidates an early assumption. Each tightening mints a new `envelope_revision`. Every scored candidate records the revision it was scored under, so stale results are detectable rather than silently trusted.

**Design state.** Copy-on-write with structural sharing (Git-style), so an edit costs tree depth rather than tree size. Fat pose/coordinate arrays are shared at chunk granularity. Full history of design states is kept; evaluation results are cached by version hash and evicted oldest-first.

**Branches.** A pin creates a branch, never an overwrite. Each branch carries an ID, a one-line description of what was pinned and why, and headline numbers (mass, worst utilisation, cost). Branches are diffable block-by-block because they share structure.

**Scenario comparison is not branch diffing.** Different scenarios may produce structurally different designs (mass production picks moulding and a different block decomposition from the printed prototype), so there is no common structure to line up. Scenario comparison happens at the **requirements/interface level**: same functions, same external contracts, here is how each scenario solved them.

**Library versioning.** Designs pin the library version they were solved against. A later check surfaces when better data exists for a pinned part (an improved bearing model) — notification, not auto-upgrade, scoped by which blocks depended on it and whether the change matters given current utilisation.

### 1.6 Phases

| Phase | Decides | Knows | Does not know |
|---|---|---|---|
| **1. Topology** | blocks, joins, load paths, function coverage | graph, contracts, kinematic joint kinds | sizes, materials, geometry |
| **2. Sizing** | material classes, dimensions against envelopes, tolerance loops | loads, envelopes, stack-up | geometry, process |
| **3. Realisation** | SDF geometry, detailed physics, process commitment, fastener realisation | everything | — |

Failures loop back to whichever phase owns the broken assumption and **permanently tighten that envelope**, so the loop converges by learning rather than by retrying. Phase 1 is human-checkpointed (~10 blocks, cheap to fix); phases 2–3 run unattended.

---

## 2. Constraint catalogue

Each constraint is tagged with its earliest evaluable phase, its kind, and how it is evaluated. Kind is one of:
**H** hard (illegality flag, early-out) · **S** soft (signed differentiable violation field) · **D** discrete (lives in the annealing layer) · **X** exact algebraic (determines a variable rather than penalising it)

### 2.1 Phase 1 — topology

| Constraint | Kind | Evaluation |
|---|---|---|
| No unreacted load anywhere | H | Every block's six load components resolve; ground contact is one reaction case. Nanostructures must be internally self-equilibrating. |
| Function coverage | H | Every stated requirement is claimed by some block. |
| Contract completeness | H | All six load components present; absent means *unknown*, not zero. Rejection names the missing component. |
| Equilibrium residual | S | Self-check on statics propagation; non-zero residual is a bug signal. |
| Single-direction assembly | H | Intersect per-block insertion-direction cones; empty intersection rejects early. Catches sets of individually-satisfiable perpendicular clips. |
| Closed-loop DOF count | S | Per loop, sum `dof_constrained` against 6. Above 6 is a design smell requiring justification, not an automatic reject. |
| Recyclability aggregate | S | Graph query over join demountability and material separability. Adhesive penalised as permanent and contaminating. |
| Galvanic adjacency | S | Same graph query as recyclability, over material adjacency. |
| Commonality | D | Fixed cost per unique BOM line and per unique tool, so reusing a size beats introducing one. |

### 2.2 Phase 2 — sizing

| Constraint | Kind | Evaluation |
|---|---|---|
| Capability envelope per material class | S | Required forces vs. stiffness range, strength, mass, anisotropy (printed parts weak across layers). Blocks well inside defer process commitment; blocks near the limit get pinned early and constrain neighbours. |
| Tolerance stack-up (chains) | S | **Worst-case arithmetic by default** — the design has plenty of DOF to absorb it. Statistical/RSS field retained for parts that turn out pinched. |
| Systematic error | S | Thermal expansion and similar go into the arithmetic sum regardless of stack-up mode. |
| Tolerance loops (closed) | D | Deliberately relax one interface in the direction the error lands (slot instead of round hole); *which* interface to loosen is a discrete optimiser choice. |
| Thermal expansion mismatch | S | Assembly-level, over interfaces — loads fasteners in shear. |
| Assembly resonance | S | Global mode vs. motor speed. Assembly-level, not a sum of per-part scores. |
| Solved-param bounds | H | User/LLM min/max with stored reason (e.g. minimum tube diameter for grip — super-alloy might be strong enough at 2 mm but you cannot hold it). Kept **separate from safety factor** so it survives a material change. |
| Reliability risk rank | S | Relative ranking of which block is likeliest to be the failure point, from utilisation + service environment + lifetime. No absolute MTBF claimed. |

### 2.3 Phase 3 — realisation

| Constraint | Kind | Evaluation |
|---|---|---|
| Process capability | S | Callable returning a signed differentiable violation field, paired with a **repair/projection function** returning the nearest feasible shape. Projections from different processes must compose. Reports when it cannot fix a violation without breaking another. |
| Tool access | S | Swept volume per fastener; penalise the number of *distinct* access directions so fasteners share a face. |
| Cutter reachability (milling) | S/X | Model the tool, not the path: cutter diameter is the design variable → no internal radius below half diameter, no pocket deeper than reach. Holder collision as swept volume. Cost term favours the shortest, fattest cutter that reaches. |
| Pull direction / undercut (moulding) | S+D | One geometric test: pick a pull direction, score the area of surfaces whose normals face away. Draft is the same test against an angle threshold. Pull direction is an optimiser variable. Each remaining undercut is a discrete side-action cost vs. reshaping. |
| Draft | S | Preferably unviolatable via a moulding-aware primitive library (tapered bosses, ribs, holes carrying pull direction and angle); global domain-warp fallback for freeform surfaces — displace the evaluation point sideways by height·tan(angle), opposite warps meeting continuously at the parting line. **Warp stretches distances: needs a Lipschitz bound and conservative sphere-tracing steps.** |
| Build orientation (printing) | D+S | Optimiser variable. Overhang angle, wall thickness, teardrop cross-sections oriented against build direction for support-free printing, branch tangent angle as a self-support cost term. |
| Clamping / fixturing | S | Clamping faces and datums as a scenario row with swept volume plus verdict rule. |
| Test/probe access | S | Same scenario-row mechanism. |
| Packaging | S | Nesting, drop survival, flat-pack; packing volume differentiable. |
| Marking surface | H | Minimum-size flat patch on an outer face that survives the process. |
| Material separability | D | Graph question over what is bonded to what. |
| End-of-life disassembly | S | Reuses assembly swept volumes reversed; differs only by the per-joint `may_be_destroyed` flag. |
| Beam-path clearance | S | Optical path is a cone/frustum that must stay clear, like a tool access volume. Vignetting scored as blocked fraction. |
| Thin-lens relation | X | Focal length / object distance / image distance — makes sensor position a *determined* dimension once lens and working distance are chosen, not a free variable. |
| Cross-domain coupling screen | S | See §2.4. |

### 2.4 Cross-domain screening

Every pair of physics domains carries a **cheap conservative screening bound** (e.g. absorbed power × rough thermal resistance → temperature rise) that decides whether the expensive coupled solve runs. Re-screened when the design moves substantially.

Run **all** pairs — even fifteen are negligible against one FEA solve — because this is what surfaces the couplings nobody thought to check: thermal expansion shifting a beam path, vibration heating a damper.

Report the full matrix of checked / active / dismissed with margins, as provenance. To the LLM, show only the **delta**: couplings that newly became active.

Screening runs inside every evaluate call with a **fidelity argument** — cheap during search, expensive before committing.

### 2.5 Margins

- One margin applied at the top on external loads, propagated by statics.
- Additional margins only at termination nodes where the model itself is soft.
- Each margin tagged with its origin (`margin_origin` in provenance) so a margin audit is a query.
- Margins are **not** a substitute for the solved-param bounds, which encode intent (ergonomics) rather than uncertainty.

Tolerance budgeting (variance-up/budget-down one traversal,
error-source correlation tags) is Addendum A4; standing simplifications
(static loads, one-way FSI, handbook convection) are Addendum A5 — both
in `multiscale-design-addendum-a.md`.

---

## 3. Optimiser

### 3.1 Structure

Nested, memetic / basin-hopping in shape:

```
outer:  simulated annealing over discrete topology
          moves: add block, merge blocks, split block, change joint kind,
                 swap material class, change process, step lattice index,
                 change pull/build direction, choose which interface to loosen
        energy of a candidate = result of the inner solve

inner:  continuous sizing
          CMA-ES, or gradient where derivatives exist
          cheap surrogate early; full relaxation only for promising candidates
```

**Move design matters more than the cooling schedule.** Mix move scales: small parameter perturbations alongside structural moves. Multiple seeded restarts beat one slow cool.

The move generator **proposes from preferred series by default** rather than generating uniformly and penalising afterwards (see §4.6 on lattices).

Bayesian optimisation as the named adaptive-surrogate layer (three
places it earns its keep: inner-solve stand-in, contested decisions,
Pareto-front extension; per-objective surrogate fitting before Chebyshev
scalarisation) is Addendum A7 (`multiscale-design-addendum-a.md`),
correcting the two-point version briefly integrated here. The house
guard stands regardless: BO schedules evaluations; objective weights
stay human-set.

### 3.2 Annealing refinements

- **Parallel tempering** — multiple chains at different temperatures with periodic swaps.
- **Adaptive step sizing** — hold acceptance rate around 20–40%.
- **Reheating on stall.**
- **Niching** — maintain several structurally distinct incumbents rather than polishing one.

### 3.3 Objectives

**Chebyshev scalarisation.** Reaches non-convex Pareto regions, at the cost of being non-smooth. Weighted sum, epsilon-constraint and achievement scalarising functions are available alternatives but Chebyshev is the default.

Design-variable count is effectively unlimited; the binding limit is the **number of objectives** — roughly 4–5 before the front explodes. Mitigation: **hierarchical/composite objectives mirroring the block structure**, so only 3–4 true trade-offs remain at the top.

> **Risk:** bad internal weighting silently discards good designs. Group correlated things; periodically re-run with one group unpacked to check nothing is being hidden.

Penalties must be **normalised against the objective's scale**, or a soft feasibility gate pins everything to the boundary.

### 3.4 Hierarchical Pareto fronts

1. Optimise each block against its **interface contract** to build a cheap per-block Pareto front.
2. The system optimiser then searches over those fronts as surrogates.
3. The interface itself is a system-level decision variable; each block's front is **parameterised by the contract**.

Fronts are range-sampled (interface loads run roughly 100–500 N) and **extended adaptively** when queried outside the envelope, rather than by a manual threshold. Log which dimensions keep triggering extensions — that is a direct signal of real inter-block coupling.

**Build the naive invalidate-and-recompute version first and measure** before adding tolerance bands to the cache. This mirrors existing MDO frameworks; the genuinely messy part in practice is stale-front bookkeeping, which is what the envelope revision in §1.5 exists to make visible.

Note the three distinct meanings of "tolerance", kept separate in the schema:
1. **design tolerance = zero** — nominal geometry is exact;
2. **manufacturing tolerance** — real, e.g. 200–400 µm clearance on screw holes;
3. **numerical rebuild threshold** on cached fronts — a solver parameter, not a physical quantity.

### 3.5 Search flow

Place-and-route in shape: place/select components, then **rip up and retry**, with each failure carrying a reason so retries target the implicated blocks rather than restarting blind.

Resolution order: **interfaces before interiors**, with defaults proposed rather than upfront questions. Rejection messages hint at the revision scope, with a retry budget before escalating to the user.

### 3.6 Iteration strategy

- Keep the first working design as **incumbent**; the status flips from `searching` to `improving`.
- Restart topology search from multiple seeds, carrying over tightened envelopes and cached fronts.
- Maintain several structurally distinct incumbents (niching).
- After a working design exists, run a **ranked refinement pass**: per block, what mass/recycling gain does each representation escalation buy? Work down the ranking until gains stop paying for compute.

### 3.7 Fidelity as a per-block choice

- Blocks well inside their envelope: cheap analytic estimate.
- Blocks near the limit: full solve.
- Same escalation logic as representation (§4.2) and as the molecular fidelity ladder (§4.9).

### 3.8 Solve provenance

Logged during the run, for later interrogation:

- **Per discrete choice:** the best *rejected* alternative and its score. ("Titanium at 800 g; aluminium was runner-up at 940 g.") This is what lets the system answer *why this?* rather than only *what*.
- **Per block:** a coarse churn count — how often its value changed late in the run — and which constraint was active when it settled. High churn means contested.

Both are logging, not new machinery.

**Presentation:** the digest carries only **contested** decisions (runner-up within ~5%); everything else is summarised in one line as uncontested. Full history of a single decision is a separate call. The summary **advertises what is worth opening** — "4 contested decisions, IDs …" — so the LLM pulls rather than guesses. (This is deliberately the opposite of the no-hints rule during topology building: building wants commitment, inspection wants exploration.)

---

## 4. Block library and representations

### 4.1 Geometry — the SDF layer

- Cartesian frames and 4×4 transforms; stable IDs parallel to the transform array.
- Primitive function library; construction tree with boolean nodes evaluated per query.
- Rounding by constant offset — note where it compounds.
- BVH / sparse caching for pruning.
- Surface extraction by marching cubes with decimation, or dual contouring.
- **Swept generalised cylinder** along a 3D Bézier/spline with varying radius, for organic branching structures. Teardrop cross-sections oriented against the build direction give support-free printing.
- **Fluid domain** is taken by negating the SDF, with refinement zones defined by the same primitives — the void is never remodelled.

### 4.2 Representation escalation

Per block, a reversible ladder:

```
solid primitive  ->  infilled shell  ->  organic branch tree  ->  imported mesh
```

Blocks start trivial and are promoted only when mass-critical or near their envelope. Promotion is reversible.

**Branch tree representation:** control points plus radius profile and parent link per segment. Load propagates from tips down the tree in a single traversal, yielding required radius and self-support angle together. Forks choose between tangent-sharing and angled departure. Evaluation is deterministic with seeded stochastic search.

**Soft / declarative shapes** (a saddle, a grip): described by a parameterised blob with min/max knobs plus mounting interface, mass, bounding envelope and pass-through loads — not modelled by hand in a mesh tool. Springs and mild flex are ordinary linear physics with a softer stiffness. **Rubber is not** — large deformation plus near-incompressibility is nonlinear and needs a different, much slower solver, so elastomer blocks terminate at a stiffness-per-area figure and a maximum deflection unless explicitly promoted.

View-dependent silhouette targets are supported for macro/organic structures: the shape reads as one thing from one direction and something else from another.

Load-sign idioms (tension-only/compression-only/bidirectional/spring/
tensegrity) and the sign-aware completeness check are Addendum A6
(`multiscale-design-addendum-a.md`), owned mechanically by map
§Complementarity.

### 4.3 Termination nodes

A termination node carries **three** things:

1. **Effective properties** (per physics domain),
2. **Validity range**,
3. **Statistical character of what is below it** — Gaussian averaging vs. extreme-value/Weibull for defect-driven modes like fracture and dielectric breakdown.

Terminations in the standing catalogue:

| Thing | Terminates at | Notes |
|---|---|---|
| Printed/milled structure | bulk material properties | most branches stop here |
| PCB | board outline, thickness, mounting holes, keepout heights, connector positions, per-component thermal load | schematic and layout stay upstream in EDA |
| CAM / toolpath | time and cost | except where toolpath *is* the design variable (fibre placement, deposition direction driving strength) — then it is structural |
| Lumped component (fan) | pressure–flow curve | keeps its static and assembly volumes |
| Electromagnetics | bulk permittivity / permeability / conductivity | |
| Lens (structural, thermal) | opaque solid with bounds | |
| Lens (beam transport) | thin-lens relation | far higher up the stack |
| Microfluidic channel | 2.5D uniform-height extrusion | a 2D layout problem per layer, plus vias |
| Valve / membrane | characterised library primitive (e.g. leakage) | not simulated |
| Chromophore | absorption spectrum with per-wavelength branching | into isomerisation, heat, and bond cleavage |
| Catalogue part | supplier-published ratings | see §4.7 |

Solver-to-solver seams are themselves treated as interface contracts.

### 4.4 Processes as first-class objects

A process declares:

- **capability tags** (declarative, so they can be filtered and ranked),
- **scenario demands** — which scenarios it brings into existence (milling demands clamping faces and a datum; printing demands build orientation and support with no clamping; moulding demands draft and ejector access),
- **cost model** — fixed setup plus per unit,
- **material set**,
- **negative volumes it emits**, exactly as joints do (ejector marks, clamping material, removable support),
- **validity check** — callable, signed differentiable violation field,
- **repair/projection function** — nearest feasible shape, composable with other processes' projections.

Processes **compose as a sequence** (cast then machine): capability is the union, cost is the sum.

Because changing the process changes the *constraint set*, there is an argument for choosing it early — but:

**Deferred commitment.** Process stays an open discrete choice as long as it is affordable. Start at the **intersection of the candidate processes' capability envelopes** so the part remains makeable by any of them; commit only when a cost term forces it. At block level the envelope is *performance* (stiffness range, strength, mass, anisotropy), not geometry rules (wall thickness, draft), which appear only at realisation.

Designing to the printed-plus-production intersection is a **per-part toggle, not a global rule** — it costs strength at one end and adds unneeded constraints at the other.

**The mould is another milled part.** Negate the component, embed in a block, split at the parting line. The part's external corners therefore inherit a minimum radius from the cavity's cutter, and mould milling time enters the cost model as setup amortised over quantity.

Toolpath depth: go deep enough for cost and feasibility only. Chip load, feed and speed are a table lookup that terminates. G-code is left to mature CAM.

The process catalogue extension (laser cutting, die cutting, turning;
the shared 2.5D sheet kernel; pair-by-pair composability validation) is
Addendum A8 (`multiscale-design-addendum-a.md`); laser/stock-cut modes
live in `se-off-the-shelf-fabrication.md` rungs 4–5, turning new there.

### 4.5 Joins and fasteners

Joins are **blocks with envelopes** like anything else.

- At topology level an edge declares only `rigid` (these two do not move relative to each other) or a kinematic kind (`revolute`, `prismatic`). Only the kinematic ones need distinguishing early, because they change the topology's *behaviour* rather than its construction.
- The join declares **requirements**: load magnitude and direction, whether it must come apart, demountability.
- Each **realisation** declares what it emits:
  - *screw* — holes in both parts, BOM line, tool access volume;
  - *clip* — added geometry to both parts, flex constraint, deflection sweep along the mating path (harder, because its swept volume is coupled to the whole part's insertion trajectory);
  - *rivet, weld, adhesive, press fit* — similarly.
- A realisation can be **pinned** by the user or LLM with a reason, exactly like any other parameter; the solver then works around it and reports if the chosen fastener cannot carry the load.
- Fastener size/count is left unquantised in the outer discrete search, since each candidate must emit its negative volumes and be rechecked.

### 4.6 Standard sizes and lattices

**Fastener sizes are a fixed enumeration** — you can only buy what exists — so no penalty term is needed.

**Grid/lattice placement gets a soft preference instead**: a ranked table over lattice type × pitch (square beats triangular; coarse beats fine — e.g. 5 cm square > 5 cm triangular > 2.5 cm triangular > 3.8 mm triangular). The optimiser picks an index, and one move steps to the next-preferred lattice.

### 4.7 Catalogue parts and library ingestion

**Buy-versus-make.** Critical parts are purchased, not fabricated — buy ball bearings, do not print them. They arrive as catalogue terminated blocks with supplier-published ratings. This reinforces the no-test-programme choice: precision lives in bought parts, generous margins live in the forgiving printed/milled structure. The buy-versus-make rule belongs in the **discoverable skills** alongside termination-level and joint-from-load, so the LLM brings judgement about what is subtle or precision-critical while the system filters the catalogue against numbers.

**Two ingestion layers:**

1. **Standards layer** — ISO/DIN fastener geometry and property classes give *computed* engineering data (M3 holding capacity, max torque by driver type).
2. **Commercial layer** — ingested from suppliers (McMaster, JLCPCB) for CAD/STEP models, availability and price at quantity.

A missing supplier therefore blocks *procurement*, not *design*.

**Availability outweighs price.** Prefer parts that are heavily in stock — the same reasoning used for PCB component selection, that widely-used parts are the safer choice. Implemented as a soft preference in the cost function, not a hard filter.

Vendor STEP models are reduced to **envelope plus interface frames**, with the original kept for export. Standard parts are parameterised from designation rather than by processing geometry. Component curation from datasheets is semi-automatic: confirmed once, then cached as a library part with provenance.

Functional placeholder blocks (e.g. "rotating joint") are matched to catalogue parts that terminate immediately.

### 4.8 Adjacent pipelines

**PCB.** A separate EDA pipeline treated as its own terminated block, same pattern as CAM. The mechanical side sees only the interface. Electrical contracts (net current, impedance targets, keepouts) sit alongside the mechanical ones **on the same interface object**. The layout tool lives in the same MCP structure; full integration of the geometry model with the schematic/layout/trace side is an explicit outstanding work item.

*Multiphysics escape hatch:* drop the PCB termination when signal integrity or current density matters, bringing in copper geometry and layer stackup for SPICE and field solves, and feed mechanical/thermal results back into the layout tool as a slow outer optimisation. Layout is no longer human-in-the-loop (autorouting, ML placement), so the loop can be fully closed.

*Metal-core PCBs* (copper or aluminium) are an option: single-layer routing constraint, much better thermal properties, usable as **heaters** rather than merely as heat sources.

**Microfluidic cards.** A parallel case to PCBs: planar laminated layer stackups modelled as shapes, with deflectable membranes acting as pumps and valves. Terminates high, like a PCB without SPICE.

- *Manufacturing routes* are the prototype-vs-production intersection case: laminate stackup (prototype) vs. injection-moulded cartridge with a sheet bonded on top (production), designed so the card migrates without redesign.
- *Bonding method is a variable coupled to chemistry*: some materials allow heat welding but release PCR inhibitors when hot, so adhesive is preferred there, while COC avoids the problem. Material, bonding process and assay compatibility are decided **together**.
- *Assay requirements arrive in stages*, like a service-environment preset: proof-of-concept with food colouring (contamination irrelevant), then the real assay brings contamination/leachables, autofluorescence, and thermal transfer plus thermal mass for PCR (driving cycle time).
- *Isothermal amplification* is an alternative to PCR with no thermal cycling, so the amplification method is a design input that reconfigures which physics matters — cycling fatigue and ramp-rate constraints largely drop away.

**Radiometric transport.** At the stock-component level (catalogue lenses, ESP32-class camera modules, lasers) this adds no new geometry machinery: the path is a cone/frustum that must stay clear, vignetting is the blocked fraction, and the lens mount is an interface contract of thread plus flange distance. The material model generalises to a **bundle of properties keyed by which physics is asking** — stiffness for structural, conductivity for thermal, refractive index and opacity for beam transport — over one shared set of SDFs.

**Irradiance for switching** is the loosest case: cast from the source with wavelength-dependent attenuation (transparent is a *coefficient*, not an obstacle) to give one scalar crossing a contract into the molecular solve as a switching rate. Open couplings around it: absorbed light as a thermal source; thermal back-switching making steady state a ratio; switch fatigue over cycle count; and self-shadowing at density, which turns the contract into a **depth profile** rather than a single number.

### 4.9 Molecular tier

**Representation.** Bricks (substantive moieties) and linkers (small connectors), each a rigid parameterised fragment with typed **attachment points** — position plus direction, angles set by hybridisation (four at tetrahedral angles for sp³, three coplanar for sp²). The attachment point's type states what may bond there (single, double, aromatic); this is the enumerable chemical compatibility field on the contract, and it constrains a neighbour's chemistry in a way a screw does not.

**Storage.** The bond graph is the invariant, with per-state pose arrays — *n* independent solves, not *n²*. Fragment identity lives in the graph; **conformer** lives in the state. Postgres schema keeps relational metadata normalised and coordinates as a binary array/blob: normalise what you query, blob what you load whole.

**Strain.** Spring and torsion-spring deviation from equilibrium bond length and angle, solved as a sparse stiffness matrix exactly like the mechanical side. Strain is reported in the **same utilisation language** as mechanical stress, as a percentage of bond dissociation energy with a plain label. Bond strain is **not** derived from the density field — density is for visualisation and interaction terms.

Over-constraint counting works identically to the mechanical closed-loop check, but at this scale the surplus DOF count means **strain rather than assembly failure** — checked against dissociation energy, and possibly desirable for bistable mechanisms.

**Geometry reuse.** Atoms and bonds use the existing transform/primitive machinery; a bond is a degenerate swept cylinder. Atom rotation matters only where directional orbitals are attached; atomic scale is a per-element lookup (covalent radius), not a free parameter.

**Delocalised π systems are first-class objects**, with handles, contributing atoms, electron count, and a ray-intersectable isosurface — the benzene lobes above and below the ring. This shares a representation with the π-stacking energy term, so the geometric query and the energy term agree by construction. Hybridisation, by contrast, is a *bookkeeping* attribute on the atom driving bond angles and valence — real density has no sp³ lobes in it — and is never ray-traversed.

**Fidelity ladder**, behind one interface (`density_at(point, fidelity)`):

```
1. per-hybridisation template field   (cheap, everywhere, composable by summation)
2. ML density model                   (coefficients of atom-centred basis functions
                                       predicted from the local environment)
3. learned delta correction           (trained on rungs 1-2 vs 4 disagreements;
                                       small smooth residual, so little data needed —
                                       but will not extrapolate to uncomputed chemistry)
4. DFT                                (junctions, switching barriers)
5. higher methods
```

Every result carries its fidelity as provenance; promotion is lazy. Results layer rather than overwrite, and cross-rung disagreement is the calibration signal that feeds rung 3.

**Conformers.** Enumerate rather than sample: generate the few distinct low-energy conformers per fragment and store them; the state references which one it is in. Bistability then becomes a **path question** — does a low-energy route exist between conformer A and conformer B — which is discrete, rather than a molecular-dynamics run. The same state-and-barrier machinery serves **catalysis**: a catalyst opens a lower-energy path between two states; the differences are that the state space includes the catalyst and that bonds break rather than merely rotate.

**Validity screening** is GPU-friendly: cheap distance checks then angle checks, flag-and-compact rather than branching, preserving the failure reason.

**Two-sided solve** between top-down block requirements and bottom-up achievable fragment geometry.

**Degradation and end-of-life.**
- Weakest-bond identification (lowest dissociation energy) gives the likely cleavage point and fragments.
- But the weakest bond is not necessarily the one attacked: the risk ranking combines **intrinsic bond strength with susceptibility** (accessibility, local chemistry), driven by light-induced ageing, oxidation, hydrolysis and switch fatigue.
- Bond breaking by light is handled at block level as a **characterised library property**, not solved: the chromophore declares an absorption spectrum with per-wavelength branching into isomerisation, heat and cleavage, feeding the irradiance contract so it returns switching rate, heat load and a cleavage rate.
- **Designed cleavability:** deliberately cleavable linkers triggered by high UV or by chemistry. Each linker carries a **cleavage record** per trigger naming the resulting products, each itself a characterised block — so the recyclability check is a graph walk over cleavage edges to known fragments and their environmental fate.
- Full breakdown to CO₂ under UV is a **lightly-weighted stretch goal**, not a hard constraint, accepting that some fragments (azobenzene between carbon chains) remain stable waste products that may or may not be acceptable.

**Library ingestion.** From existing chemistry sources rather than from scratch: compound databases (PubChem/Enamine-style) plus a fragmentation step. Only the **characterisation layer** is novel. A planned integration point connects the building-block library to a papers/literature database for library search.

**Synthesisability** is carried both as click chemistry and as an atomic assembly arm; both routes are wanted — and *(added 2026-09-12)* the route is an explicit **discrete optimiser choice** per design, not a global setting, since each route constrains linker chemistry differently.

The non-bonded interaction menu (charge patterns, hydrogen bonding, vdW
alongside π-stacking), the ratchet/bistable-snap barrier-graph reading,
the hysteresis cache-keying rule, and the DFT-computed spacer library
are Addendum A9 (`multiscale-design-addendum-a.md`); instances route to
`nm-stick-placement.md`, `photoswitch-states-and-spectral-dof.md`,
`design-state-core.md` and `blocktree-library-build-plan.md`
respectively.

**Not modelled: solvent.** Everything is currently in vacuum. This matters a great deal for π-stacking. See §6.
---

## 5. Interfaces

### 5.1 Division of labour

| Owner | Responsibility |
|---|---|
| **LLM** | World knowledge. Proposes the decomposition, the outer boundary contracts (rider mass, wheel, saddle, pedals, ground contact), and its assumptions. |
| **System** | Numbers. Propagates internal forces by statics, filters the catalogue against numeric requirements, proposes joints and standard components, solves. |
| **User** | Reviews the topology/contract stage, pins what they care about, inspects the result. |

The LLM owns phase 1 only. Internal contracts (fork-to-tube) are **derived by statics, never guessed** — so the MCP only ever rejects at the *boundary* contracts the LLM introduces. Equilibrium residual is the self-check.

World-knowledge numbers are written **directly into the contracts**, each with a `load_provenance` field marking LLM-assumed vs. user-stated. There is deliberately no separate assumptions list.

### 5.2 MCP design rules

- **Declarative:** the agent states intent (load requirements), it does not place fasteners.
- **Structured diagnostic failures**, never bare `invalid`.
- **Capability tags and termination options are explicit enumerations in the schema**, so they are discoverable and filterable.
- **Discoverable skills** matching the real decision points: termination level, joint from load, buy-versus-make, process branch, failure diagnosis, datasheet extraction.
- **No hints while building.** Malformed errors (bad reference, duplicate ID) are reported immediately; semantic gaps only at the explicit validate step. Completeness is a whole-graph property.
- **Hints while inspecting.** Summaries advertise what is worth expanding.
- Sits alongside the existing `retospect/precis-mcp` verb surface (put/get/search, tune tables, token efficiency, discoverable skills).

### 5.3 Verb surface — build phase

```
create_design(label, scenario)                       -> design_id
create_block(design, label, type, params_declared)   -> block_id, attachment_points[]
```
Block creation returns **named attachment points** ("top face", "bottom face", "side at 0.4") rather than coordinates, keeping the LLM in topology and names and out of geometry.

```
connect(block_a, attach_a, block_b, attach_b, joint_kind, contract) -> interface_id
set_bound(block, param, min?, max?, reason)          -> ok
pin(target, value, reason)                           -> branch_id       # §5.6
validate_topology(design)                            -> ok | Rejection[]
solve(design, phase, fidelity)                       -> job_id
checkpoint(design, label)                            -> checkpoint_id   # explicit backtracking
restore(checkpoint_id)                               -> design_id
```

**Rejection** is structured:
```
Rejection { code, subject_id, missing_field?, violated_constraint?,
            revision_scope_hint, retry_budget_remaining }
```
A contract missing `Mz` is rejected naming `Mz`, so the LLM resubmits rather than guessing. After the retry budget is exhausted, escalate to the user.

Backtracking is an **explicit verb**, never implicit.

### 5.4 Inspection toolkit

Three modes of getting around, between which nearly any question can be answered without sending geometry:

- **rays** — what is physically there
- **handles** — what is this thing
- **graph walking** — what is it connected to

```
cast_ray(design, origin, direction, fields[]) -> Hit[]
  Hit { block_id, material, entry_distance, exit_distance,
        strain_profile?{max, min, locations, along_ray, shear_across},
        basin_transitions?[{atom_id, charge}],
        density_profile?, reliability_risk? }
```

A ray is an origin, a direction and a list of hits — completely textual, and trivially cheap against an SDF since that is exactly what SDFs are for. It is the LLM's sense of touch: *can I poke through here?* Fanning a few parallel rays and comparing hit distances answers *is this surface flat or bent* as a **number** rather than a picture.

Requested fields that do not exist at that point are **silently omitted**, not errored. Availability is a property of the block and the view degrades gracefully. At molecular scale a ray through vacuum is mostly empty, so the useful form is a ray through a *field*; contact has no clean analogue, so material boundaries become **atomic basin transitions** (Bader-style gradient partitioning) with charge per basin, and interface load becomes interaction energy.

```
describe_block(block_id)  -> material, mass, params_solved, bounds + reasons,
                             attachment_points_used + to_what, utilisation,
                             representation, termination[], provenance
neighbours(block_id)      -> [{interface_id, other_block, joint_kind, contract}]
```

Walking beats dumping the whole graph past a few hundred blocks.

```
max_stress(scope = design | block | subtree)
  -> value, location, governing_scenario, block_id
under_utilised(scope, threshold)
  -> [{block_id, utilisation, shrinkable_mass_estimate}]
```

`max_stress` is the natural entry point: ask where the worst is, get a coordinate, then fire a ray through it to see what surrounds it. `under_utilised` is the same machinery at the opposite end — it finds free mass.

**Digest** (the post-solve summary returned to the LLM — never geometry):

```
per block: material, key dimensions, mass,
           utilisation % of limit + label{critical, tight, comfortable, oversized},
           worst-case location, governing scenario,
           reliability risk rank
per interface: load carried vs. contract promise
global: mass, cost, worst utilisation, top-3 mass drivers,
        first-to-fail block and its margin,
        contested decisions (ids), newly-active couplings (delta only)
sensitivity: per dimension, effect of +10% on mass and on stress
```

Everything is **pre-normalised to percentages and labels** so the LLM never performs arithmetic. Sensitivity and the ranked lists are included precisely because they are the things that cannot be eyeballed.

### 5.5 Bookmarks

A bookmark stores a **query, not an answer** — a ray, a block parameter, a max-stress scope — so the saved set can be re-evaluated against any branch or design and you see what moved. Together they form a reusable fisheye status view.

Validity: a bookmark references block IDs and is valid wherever those IDs survive; if a branch deleted or replaced the block, that entry reports `unavailable` rather than breaking the view. Rays are the interesting case — pure coordinates, so they stay valid even when the geometry changes underneath them, which is exactly the desired behaviour.

### 5.6 Branches

A pin creates a branch. The response is a **comparison, not just a new design**:

```
pin(...) -> BranchResult {
  branch_id, one_line_reason,
  headline { mass, worst_utilisation, cost },
  delta_vs_parent { mass +4%, block_x utilisation 78% -> 92%, ... },
  infeasible? { what_broke, which_constraint }
}

list_branches()            -> [{id, reason, headline}]
diff_branches(a, b)        -> per-block deltas
switch_branch(id)          -> ok
```

Both candidates are kept, so a pin is always reversible. Deliberately **no tree browser**: one line per branch keeps context cost trivial; it only gets heavy if full digests are cached per branch.

### 5.7 Long-running jobs

**One uniform status interface across every long job** — the annealer, DFT, an ML potential run — so the kill-or-wait decision is learned once rather than three times. (DFT and ML-potential runs already suffer from hours of sparse output; this is the same problem.)

```
job_status(job_id) -> {
  phase,
  state: searching_for_feasible | improving_incumbent,
  current_best { headline numbers },
  time_since_last_improvement,
  estimated_remaining?,
  incumbent_design_id?
}
```

The `searching` → `improving` transition is the honest answer to *am I done?* — those are qualitatively different states and the caller should be told which one it is in.

**Intermediates are written to the database.** The incumbent is then just another design, inspectable with the entire toolkit above — rays, handles, utilisation — with no special live-viewing machinery. So draft *n* can be examined while draft *n+1* is still solving, and the run aborted early if it is already good enough.

### 5.8 Viewer

**Three.js route.** Assembled view, assembly steps, hierarchical tree with three-state visibility toggles, click-to-select interfaces. No CAD manipulation required. `bernhard-42/three-cad-viewer` already provides the hierarchical assembly tree, path-style node IDs and picking filtered to vertex/edge/face/solid; `react-three-fiber` if this is embedded in a larger React app. Marching-cubes output feeds the browser mesh directly, and minted IDs are used as leaf names in the path scheme so a pick returns the ID with no lookup table.

**The pre-solve stick-figure sketch is a first-class artefact**, not a byproduct — it is the cheapest human checkpoint in the whole system. If the LLM has put the pedals on the frame, that is visible in two seconds, before anything has been spent.

**Connectivity must be drawn, not inferred.** Coincident geometry hides topology — a wheel, pedals and frame all meeting at one axis look identical whatever the graph says. So:

- draw each connection explicitly as a coloured link between block centres, offset from the geometry;
- make selection **propagate** — click the wheel and everything connected to it highlights;
- offer an **exploded view**, pulling parts apart along their attachment directions, which makes true connectivity unmistakable.

**Mermaid-style topology graph alongside the 3D view**, edges labelled with joint kind (rigid, revolute, snap). Pure topology with no geometry to confuse things, cheap to emit since the graph already exists. Side by side with the 3D view, with **linked selection**.

---

## 6. Open items

Carried forward deliberately. The architecture holds them; the physics is not written.

### 6.1 Physics not yet modelled, in priority order

1. **Time-dependent degradation** — creep, corrosion, UV/polymer ageing.
2. **Wear and tribology.**
3. **Electromagnetics** — only when a motor or PCB is present.
4. **Solvent effects** (molecular) — everything is currently in vacuum; matters a great deal for π-stacking.
5. **Reaction kinetics and yield** (molecular) — raised, not resolved.
6. **Acoustics and optics beyond stock components** — parked.

### 6.2 Design questions still open

- **Supply-chain constraints** — lead times, stockable fasteners, minimum order quantities. Availability preference (§4.7) is a partial answer only.
- **System-level failure-consequence ranking** beyond per-block safety factors. The relative reliability risk rank (§2.2) ranks *likelihood*, not *consequence*.
- **Full integration of the geometry model with the PCB schematic/layout/trace side** when the whole system comes together.
- **Assay/chemistry compatibility** as a first-class constraint set rather than a microfluidics special case.

Process projection composability and reaction yield are Addendum A10
(`multiscale-design-addendum-a.md`), appended to this list.

### 6.3 Explicitly out of scope

- A test and validation programme. Generous margins plus bought precision parts substitute; see §0.2 (10) and §2.5.
- CAD manipulation in the viewer.
- G-code generation.
- Human-in-the-loop PCB layout.
