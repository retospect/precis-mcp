---
status: draft
title: multiscale design architecture — one geometry currency, layered cost terms; the map of the design-space programme
prio: high
model: opus
---

# Multiscale design architecture — the map

Absorbs Reto's `multiscale-geometry-architecture.md` (2026-09-10, briefly at
the repo root; removed with this doc). That paper re-derived most of the
house architecture from first principles — which validates both — plus a
handful of subsystems nothing else owns. This doc is the **map**: the
cross-cutting vision in one place, a table locating every concept in its
owning doc/module, and the new subsystems held here until each grows a build
slice. Leg detail lives in the leg docs; nothing here re-opens their named
deferrals.

2026-09-11: Reto's detailed build spec landed as its own leg doc,
`multiscale-design-system-spec.md` (was briefly at the repo root) — data
model, constraint catalogue, optimiser, MCP inspection surface, molecular
tier. Its intake preamble routes every section: existing owners win
(se-kind ladder, feasibility-and-cost registry, blocktree plan, shipped se
op surface, decided §Units policy); the genuinely new subsystems are owned
there and listed in the table below. Slices are carved from it on
promotion, not pre-sharded.

## The core idea (and where it already lives)

One geometry currency, many layers on top. The model never switches data
structures as it refines: a tolerance box at the top and atomic coordinates
at the bottom are the same kind of object, queried the same way; what
changes between levels is **resolution and which cost terms are active**,
not the representation. Everything else — physics, manufacturing, synthesis,
assembly — is a field or a cost term over that shared geometry.

This is the shipped house architecture: the analytic-SDF cad kernel is the
currency (booleans are min/max, tolerance envelopes are level-set offsets,
gradients exist a.e.), rented by `se` as metres and `nm` as Å
(`se : cad :: nm : structure`); the abstraction ladder is se's L0–L5 IR
(interface contract preserved, interior replaced — a coarse clash check
stays valid after refinement); interface contracts are ports + joints +
measures, and they are the boundary conditions for every later solver.
The kernel's own weak spot (absolute tolerances at out-of-band scales) is
already handled by `precis_se.validate.kernel_scale`.

## The map — concept → owner → status

| concept | owner | status |
|---|---|---|
| SDF geometry currency, booleans, differentiability | `precis.cad` kernel | shipped |
| abstraction ladder / interface contracts | `se-kind.md` L0–L5; ports/joints/measures | slices 1–4 shipped |
| joints own geometry (emit negative volumes) | `se-off-the-shelf-fabrication.md` engine 2; `precis_se/fasten.py` | `screw` shipped; sheet/tube/press, weld+HAZ open |
| unilateral members, prestress, m−s stability | `structural-solution-space.md` (the model + rung ladder) | rungs 1/2/4 + prestress DRC shipped |
| form-finding (force density) | `precis/structsolve/formfind.py` | shipped |
| strut/node graph intermediate | the axial subgraph `stability`/`formfind` operate on | shipped |
| topology optimisation (SIMP), graph interpretation | `structural-solution-space.md` slice 4 | open |
| state-dependent stability, switching-pathway sweep | `structural-solution-space.md` slice 5 | blocked on blocktree slice 2 |
| process lattice / capability tags | `se-kind.md` slice 5 rows; `se-off-the-shelf-fabrication.md` engine 3; severity-vs-capability in `se-feasibility-and-cost.md` | open (§7.1's tag list is a usable row schema) |
| design-to-intersection, quantity as optimiser input | `se-feasibility-and-cost.md` (capability-resolved severity re-grades per process) | open |
| tool access / swept volumes / assembly order | `se-off-the-shelf-fabrication.md` rung 3b; `se-feasibility-and-cost.md` rungs 5–6 | open |
| DFA soft costs, objective vector | `se-feasibility-and-cost.md` | open |
| molecular fragment library, states, joining chemistry | `blocktree-library-build-plan.md` + star-schema facts | open (plan `ready`) |
| interaction-aware module placement (graded π-stack, kT thresholds, pose solve, fisheye read) | `nm-stick-placement.md` | spec ready 2026-09-11 |
| photoswitch physics, channel budget, photo-charge | `photoswitch-states-and-spectral-dof.md` | evidence gathered |
| toolpath ownership (slicer integration ladder) | **here, §Toolpath ownership** | new (Reto 2026-09-11) |
| scenarios + three-verdict rule table | **here, §Scenarios** | new |
| complementarity solver | **here, §Complementarity** | core BUILT 2026-09-12 (`structsolve/complementarity.py`: active-set solve e49fb80a + bistability probe; se bridge/validate wiring waits on the units window — `complementarity-solver.md`) |
| optimisation stack (surrogates, BO, annealing) | **here, §Optimisation** | new |
| requirement→joint matching | **here, §Joint matching** | new |
| view-dependent form | **here, §View-dependent form** | new |
| pattern groups (one prototype × transform group; orbit-deduped checks, port arrays, n× BOM) | `pattern-groups.md` (Reto 2026-09-12; composes with blocktree slice-1 instancing) | spec'd 2026-09-12 |
| scenario presets + service environment (lifetime master switch, standard load-case library) | `multiscale-design-system-spec.md` §1.3 | spec'd |
| per-number provenance; envelope revisions, CoW design state, pin→branch comparison | spec §1.4–1.5, §5.6 | spec'd |
| phase loop (topology→sizing→realisation, envelope tightening) | spec §1.6 | spec'd |
| cross-domain coupling screen | spec §2.4 | spec'd |
| optimiser refinements (Chebyshev, tempering/niching, hierarchical Pareto fronts, solve provenance) | spec §3 (invariants stay §Optimisation here) | spec'd |
| termination nodes + representation escalation ladder | spec §4.2–4.3 | spec'd |
| process repair/projection + composition + deferred commitment; lattice preference; catalogue ingestion | spec §4.4–4.7 | spec'd |
| microfluidic cards | spec §4.8 | PARKED (Reto 2026-09-12) — out of this campaign |
| radiometric transport + irradiance contract | spec §4.8 | spec'd — kept: smooth, cheap, propagating constraints (Reto 2026-09-12) |
| molecular `density_at` fidelity ladder, conformer enumeration, degradation/cleavage records | spec §4.9 | spec'd |
| inspection toolkit (cast_ray, max_stress/under_utilised, digest, bookmarks); job searching→improving status | spec §5.4–5.7 | spec'd |
| reference targets (struts-and-strings bracket; azobenzene/tensegrity flagship as architectural test) | `multiscale-design-addendum-a.md` A1 | spec'd 2026-09-12 |
| limited component DOF + meshing-controlled generated geometry (principles) | addendum A2 | spec'd 2026-09-12 |
| lifecycle situations: n-not-three, operational 4D motion, hard-stop contact row, compare-by-volume | addendum A3 (built out in `situation-rule-tables.md`; mechanics stay §Scenarios here) | spec'd 2026-09-12 |
| tolerance budgeting: variance-up/budget-down one traversal + error-source correlation tags | addendum A4 (allocation rides `margin-budget-tree.md`; chains f&c rung 4) | spec'd 2026-09-12 |
| standing simplifications (static loads, one-way FSI, handbook convection) | addendum A5 (consistent with §Physics layers here) | spec'd 2026-09-12 |
| BO as adaptive surrogate: inner-solve stand-in, contested decisions, Pareto-front extension, per-objective fitting before Chebyshev | addendum A7 (`complementarity-solver.md`; house guard stays §Optimisation here) | spec'd 2026-09-12 |
| load-sign idioms + sign-aware completeness check | addendum A6 (`complementarity-solver.md`; mechanics stay §Complementarity here) | spec'd 2026-09-12 |
| laser/die/turning processes; 2.5D sheet kernel serving three processes | addendum A8 → `se-off-the-shelf-fabrication.md` rungs 4–5 (turning new) | spec'd 2026-09-12 |
| non-bonded menu (charge/H-bond/vdW), ratchets, hysteresis caching rule, DFT spacer library, synthesis-route choice | addendum A9 → stick-placement / photoswitch / design-core states / blocktree | spec'd 2026-09-12 |
| process projection composability; reaction yield (open questions) | addendum A10 → spec §6.2 | spec'd 2026-09-12 |

## Toolpath ownership — how far into the slicer we go (new)

Reto, 2026-09-11: do we integrate the filament slicer into the design
process — generate the design layer by layer with full control over fill
patterns — or make the shape orientation-aware and hand off an STL? The
strength argument is real: FDM parts are anisotropic (along-filament ≫
across-layer), the SIMP engine *computes* the principal stress field, and
a third-party slicer never sees it — the STL handoff flattens the density
field and every load-direction fact to a surface, then the slicer
re-derives uniform infill from nothing.

The pcb precedent (own `gerber.py`, never hand off to an external router)
says owning a vendor format is house-viable when the intent would
otherwise be lost at the boundary. But a production slicer is a decade of
commodity empiricism (retraction, seams, cooling, flow math, first-layer
adhesion, per-material schedules) that carries no design intent. So: a
ladder, owning the *intent-bearing* layers and renting the commodity
ones —

1. **Shape + orientation handoff (now).** The AM filter bakes the build
   direction into the geometry; export via the existing cad routes. The
   slicer owns everything else. Cheapest, ships with slice 4's bridge.
2. **3MF with density-derived modifier volumes (the underexploited middle
   rung).** 3MF carries per-region print settings; PrusaSlicer/Orca
   honour modifier volumes. Quantize the SIMP density field into
   region-wise infill (dense → solid, sparse → light) and, where the
   lattice fill was chosen, emit the lattice as explicit geometry. The
   generative result *survives into the print* while the slicer still
   owns all commodity mechanics. Most of the strength value for a small
   fraction of the work.
3. **Stress-aligned toolpaths for the structural interior (the monster
   rung, on demand).** Filament laid along principal-stress trajectories
   from the FEA field — the thing no external slicer can do, because it
   needs the load case. Note the analytic kernel makes this cheaper than
   it looks: G-code needs only planar contours, and slicing an SDF is
   exact 2D evaluation per z-plane (no mesh, no marching cubes — the
   deferral stands); `pcb/gerber.py` is the in-tree prior art for arc
   path emission with quantization. Scope: interior infill paths first
   (inject into a slicer-generated skeleton as custom regions), full
   G-code ownership only if a real design demands end-to-end
   verifiability (owning every move is also what makes "printed without
   support" checkable rather than trusted).

Rung 3 lands at se's L5 (fabrication plan: mode + build frame + process
DRC already own that tier); the per-material flow/temperature empiricism
stays capability-row data, never code. Do not start rung 3 before a
design measurably needs stress-aligned strength — rung 2 is the default.

## Scenarios and the three-verdict rule table (new)

A part owns several volumes, not one: static occupied, assembly-swept
(shape × insertion path), tool-access-swept, motion envelope (union over
reachable poses). All are swept volumes — a screw going in, a screwdriver,
a maintenance hand, a molecular fragment rotating into place are one
mechanism. A **scenario** is a named bundle: allowed configuration set +
which volumes are active + a rule table over volume pairs. Don't hardcode
the scenario list (assembly, operation, maintenance, diagnostic access,
disassembly, inspection, thermal-expansion state, shipping).

The rule table has three verdicts, not two: **must clear** (overlap is a
violation) / **may touch** / **must contact** (hard stops, seats, preloads —
contact is *required*; a hard stop that fails to seat is as broken as one
that interferes). `must contact` is the genuinely new verdict — today's
clearance/DRC machinery can only forbid overlap, so a non-seating stop is
invisible.

Interference is 3D + sequence: two volumes may overlap safely when
separated in the assembly ordering, so ordering constraints fall out of
cross-configuration queries (*which pairs clear in every configuration* =
safe; *only in some* = ordering constraints) and the ordering itself is a
discrete optimiser variable (→ §Optimisation). Removability: sweep the part
along a candidate path, intersect against the union of the other active
statics, empty ⇒ valid; "actually out" is a free-space escape test against
a **named** bounding volume (a concave assembly's wider machine can still
trap a part that cleared its subassembly's box). Useful trick: plan
disassembly, then reverse it.

## Complementarity — the unilateral family completed (new)

One formulation: per connection, either the gap is zero and there is force,
or there is a gap and zero force — never both. Covers cables/ties
(tension-only, slack under compression), contacts/struts (compression-only),
and hard stops (§Scenarios' must-contact), with opposite signs; it is what
makes tensegrity and dry-stacked masonry the same problem. Unilateral
constraints make stiffness nonlinear (a cable going slack changes the
structure qualitatively), which is why prestressed networks need
form-finding rather than plain analysis — and why "which ties are taut
under this load case" needs an active-set/complementarity solve, not a
graph walk.

This **subsumes the structural leg's deferred rung 6** (active-set load
path — see `structural-solution-space.md`): when a consumer wants the load
path, build it as the complementarity solve from the start. With two stable
equilibria and a barrier it is also the bistable-actuation analysis
(photoswitch-driven tensegrity). Covalent bonds are *not* unilateral —
bilateral, strongly asymmetric, with a failure point (Morse, not Hooke);
the cross-scale mapping table lives in `structural-solution-space.md`.

**Load-sign idioms + the sign-aware completeness check** are Addendum A6
(`multiscale-design-addendum-a.md`): spring is a bidirectional member
with a declared rate, not a sixth idiom (already the shipped slice-1
axial-rate member); a tension-only member driven into compression by any
declared load case makes the topology **incomplete**, not merely
stressed. Build detail: `complementarity-solver.md`.

## Optimisation stack (new)

- **Soft costs, not hard gates.** The validator rules out only the
  genuinely impossible; everything else is a penalty to minimise. Already
  the house posture — this is `se-feasibility-and-cost.md`'s two tiers with
  severity resolved against capability rows; the hard/soft boundary is a
  capability number, not a property of the check. Extend that registry;
  do not invent a second boundary.
- **Hybrid search.** Gradient descent on continuous parameters (the SDF is
  differentiable a.e.); simulated annealing on discrete jumps — which
  module fills a slot, which process, which joint type, which assembly
  order. Joint choice × process × material constrain each other and belong
  in **one** coupled outer discrete search, not three independent ones —
  the most consequential loop, and cheap relative to the physics.
- **Surrogate ladder.** Closed-form estimates first (wrong by 20–30 % but
  they *rank* correctly, which is all screening needs) → fitted Gaussian
  processes (~50 real-solver samples; GPs report their own uncertainty, so
  the fallback point is principled) → full solve on the top two or three
  only. Never run the expensive physics on the whole space.
- **Bayesian outer loop.** Acquisition function (expected improvement)
  picks the next evaluation; converges in tens of samples. Degrades above
  ~20 continuous dimensions ⇒ **parameterise by feature (wall thickness,
  rib count, fillet radius), never by vertex** — already the se posture,
  now stated as an invariant. Freeze rigid fragments, allow only spacer
  torsions; fix the coarse layout before refining interiors.
- **Worst-case handling.** Cheap: perturb each uncertain input both ways,
  keep the worse direction. General: interval propagation. The worse
  *sign* is objective-dependent (lower convection is worse for a heat sink
  and better when thermal expansion closes a fit).
- **House guard.** The acquisition function chooses *where to evaluate*;
  objective weights stay human-set (`quest` frontier discipline — a solver
  may not tune its own objective). BO is a search scheduler, never a
  weight-tuner.
- **BO's three loci** (Addendum A7, `multiscale-design-addendum-a.md`):
  the inner-solve stand-in above; contested decisions (runner-up within
  ~5%, spec §3.8) queued for a refinement evaluation; adaptive Pareto-
  front extension (spec §3.4) when a query lands outside a front's
  sampled envelope. Fit the surrogate **per objective, then Chebyshev-
  scalarise** — scalarising first feeds the surrogate a non-smooth
  target.

## Requirement→joint matching (new)

Declare the joint **requirement** — load magnitude/direction, cyclic or
static, reversibility tier (permanent / serviceable / frequently opened),
alignment precision, sealing/electrical/thermal duties — and let selection
match it against the joint library's carries/demands rows (bolt+nut, screw
into tapped, rivet, weld, snap-fit, adhesive — each advertising what it
carries and what it demands: access sides, tool envelopes, material
compatibility, cure time). Extends the mechanism registry, whose entries
already carry implied demands but are designer-picked today. The choice
couples into §Optimisation's discrete search (a snap-fit implies a polymer
and mouldable geometry; a weld implies compatible metals and torch access).

## View-dependent form (new)

The object reads as one thing from one direction, another from another.
Each view is a target silhouette — a 2D distance field; project the solid
along the view direction and penalise outline mismatch. Just another cost
term over the same SDF. The **maximal solution is free**: intersect the
extrusions of all target silhouettes — empty/too-thin means the views are
incompatible *before any optimisation*, and once inside that envelope you
can carve freely for weight/structure while each silhouette's outline is
preserved. Caveats: silhouette targets fight structural and printability
terms directly (weight them, never hard-constrain); at finite viewing
distance the projection is a cone, not a cylinder.

## Units policy (decided, Reto 2026-09-11)

**Ingest any stated unit; one canonical internal representation; always
render ISO units with scale-appropriate prefixes.**

- **Input**: ops accept an explicit unit declaration ("state your units" —
  nm, Å, mm, m, km; N, kN); the MCP converts at the boundary. No implicit
  per-kind convention an agent must guess — the pre-cutover state (cad DSL
  docstring said mm, se stored m, nm stored Å, one shared grammar) was the
  anti-pattern this replaced. Explicit units kill the two observed LLM
  failure modes: zero-counting (`box:w0.000000003…`) and silent 10×
  exponent slips that validate cannot distinguish from intent.
- **Internal**: one representation, SI base (m, N), float64. Relative
  precision is scale-free, so 1.4 Å = 1.4e-10 m loses nothing down to
  sub-fm. The *actual* resolution hazard is *absolute epsilons* in
  kernels and solvers (SDF/clearance tolerances, convergence thresholds
  tuned for metre-ish magnitudes are garbage at 1e-10) — the migration
  must audit every absolute tolerance and make it relative to a
  design-scale length (e.g. bbox diagonal). `structsolve` is already
  unit-agnostic; unaffected.
- **Display (re-revised, Reto 2026-09-12): one shared neat formatter,
  scale-appropriate SI units** — `2.3 nm`, `1.2 kN`, `350 ml`. Supersedes
  the 09-11 e-notation-only decision: ingest is maximally flexible (any
  declared unit, however whimsical), internal stays SI float64, and the
  *output* formatter picks the readable SI prefix/unit. One formatter
  utility shared by MCP text views and web UI, so there is still exactly
  one prefix table. E-notation remains the fallback for out-of-prefix
  magnitudes and for raw/debug views.
- **Tolerances/ranges are order-of-magnitude-specific — relative by
  default** (Reto 2026-09-11). Every *system-supplied or unstated*
  threshold (clearance "touching" bands, SDF comparison epsilons,
  convergence criteria) is interpreted relative to a governing length
  (feature size, else bbox diagonal), and the view reports which default
  it applied. *Author-stated* tolerances are accepted as `%` (relative)
  or absolute-with-unit and never silently relativized — a press fit is
  microns regardless of diameter. *Process-capability rows stay
  absolute* and trump the relative default once `set_mode` is known
  (defaulting ladder: stated → capability row → scale-relative
  fallback); capability absolutes are what make "asked 10 µm, fdm holds
  200 µm" DRC computable (even ISO IT grades scale ~D^⅓, not linearly).
- **Angles are a fifth dimension of the same ladder** (Reto 2026-09-12,
  `units-policy-cutover.md`'s decisions log): radians internal, an
  explicit unit at ingest (`deg`/`rad` — a bare angle number is refused
  the same as a bare length), degrees on display (the shared formatter
  never SI-prefixes an angle — nobody reads femto-degrees). `se`/`nm`'s
  own `pose`/`rot` block-tree vectors are the one carve-out: both stay
  **bare-number** (metres / radians respectively, no unit token,
  not unit-string-parsed) — a deliberately different, older convention
  from `cad`'s own `rot:`/`spin:`/`limits:` text tokens, which *are*
  unit-required.
- **Two named unit enclaves stay outside this ladder, by declared rule**
  (glossary: *unit enclave*) — a package may keep a non-SI internal
  convention only if every identifier self-names its unit (a `_mm`/`_A`
  suffix) AND every cross-package API converts to SI at the boundary:
  `structure` (Å/eV-native — ASE's `Atoms`/calculators/optimizers are
  woven through it, so forcing SI would add a conversion per ASE call;
  `precis_nm/mechanics.py`'s Å/nN/eV atomistic-scale signatures are the
  same enclave, reached through explicit m↔Å seams at the nm↔structure
  boundary) and `pcb` (mm-native — gerber/Excellon/IPC/JLC's own file
  formats are mm-native at both ends of its pipe). Neither enclave is
  reachable through the ingest-any/format_quantity ladder described
  above; each has its own compliance doc
  (`structure-unit-enclave.md`).
- **Boundary: atom interactions are separate** (Reto 2026-09-11). This
  policy covers the geometry currency (lengths, forces, angles,
  tolerances) only. Interaction physics — kT thresholds, π-stack
  energies, nm mechanics capacities — keeps its own native quantities in
  its own modules (`nm-stick-placement.md`, `precis_nm/mechanics.py`) and
  is never routed through the unit-defaulting ladder: cost terms *over*
  the geometry, not lengths *in* it.

Shipped (`units-policy-cutover.md`, 2026-09-12 window): `precis/utils/
units.py` is the one shared parser/formatter; `cad`'s DSL/scene grammar,
`se`/`nm`'s handlers and ops, and their display sites all route through
it; `se`'s `pose_rot` and `nm`'s pose/envelope columns carry a
forward-only migration/wipe to the units this section describes. Cross-
kind seams (`bind_structure`, `realized-by`, formfind feeds) are trivial
now the internal rep is shared. The clearance sign-flip (gr334763) and
the box half-extent ambiguity (gr334785, `box-full-dims-cutover.md`) are
the same family and land in the same window.

## Physics layers — deferred, with the notes that shouldn't be re-derived

The structural leg's deferral list stands (FEA, dynamics, fluids, thermal;
SIMP slice 4 is the first FEA and stays advisory). Carried notes for
whoever un-defers one:

- **Dynamics:** modal analysis (do service loads excite a natural
  frequency?) or frequency-domain superposition before any time-stepping —
  but both require linearity; contact/friction/plasticity/large deflection
  force time-domain. Fatigue kills parts well below static strength, and
  applies to photoswitch cycling too.
- **Fluids:** mesh the *void* (negate the SDF); use one-way coupling
  (rigid-wall flow → pressure field → structural load) unless deflections
  demonstrably change the flow — two-way FSI is a large step up in cost.
- **Thermal:** the friendliest — steady conduction is one scalar per node
  on the structural mesh; chain temperature → thermal strain → stress.
  Convection coefficients are handbook numbers: treat as uncertain inputs
  under §Optimisation's worst-case handling.
- **Boundary conditions come from the interface contracts** (ports/joints/
  measures) — a bolted joint becomes a spring or constraint, a mating face
  a contact pair — so structural, thermal and assembly views stay
  consistent from one source of truth.

## Molecular notes, routed

Fragment library with cached per-fragment DFT signatures (compute once,
assemble combinatorially, re-run DFT only at junctions) → the blocktree
plan + star-schema facts. The non-bonded interaction menu (vdW,
electrostatic, H-bond, π-stack, cation–π, steric, hydrophobic — each a
signed want/avoid term) now has an owner for its first instance:
`nm-stick-placement.md` specs the graded π-stack pair potential
(screening tier, kT acceptance thresholds, engaged/avoid as declared
intent) with H-bond and charge as its named next terms; ratchets/
bistability (asymmetric barrier = one-way ratchet; hysteresis from
barrier vs kT) → design content for quest loops, with the physics
evidence in `photoswitch-states-and-spectral-dof.md`. Photon
interaction (absorption cross-sections, self-shadowing, wavelength
selectivity) and environment (solvent explicit or dielectric; entropy at
temperature — marginal-at-0 K is not a design) → that doc's open list.
Yield/kinetics: feasibility is not enough — activation barriers decide
rate; TS searches are expensive, so screen early by penalising strained
geometries and crowded sites, evaluate barriers only on finalists. The
switching-pathway sweep requirement landed in `structural-solution-space.md`
slice 5.

## Build order (decided with Reto, 2026-09-12)

The whole programme runs as carved slices (spec → `ready` vet → sonnet
coder → qland bursts + periodic /go), many worktrees in parallel but
**package-disjoint** (precis_se / blocktree-nm / precis_web / structsolve
/ cad) so plugin migration numbering never collides and no two agents
share a persist.py. Main loop holds seams and the owner-wins precedence
(extend the feasibility registry, one annotations vocabulary, shipped se
op surface is the surface of record). Each phase boundary is a human
checkpoint with a dogfood artifact.

Standing compute decision: long solves run on the existing worker lanes;
GPU only for genuinely-GPU work, written as Python tensors (torch) — a C
CUDA path only if a real workload ever forces it.

1. **Units cutover — exclusive window** (`units-policy-cutover.md`).
   Cross-cuts se+nm+cad, so nothing else lands on those packages while
   it's in flight.
2. **Foundations, 4 parallel tracks**: shared design-state core
   (`design-state-core.md` — scenarios, provenance, revisions, branches
   naive-copy-first, checkpoints, **and the discrete-states + stimulus
   machinery**; se deltas ride along) · blocktree slices 1–3 (slice 2
   becomes the nm *adoption* of the shared states schema, co-designed
   with the core track) · viewer round 1 (gr335242 items 1–3) ·
   inspection toolkit v1
   (cast_ray/describe/neighbours/max_stress/under_utilised/digest).

   Shared-states rationale (Reto 2026-09-12): bistability is true at
   both scales — nm photoswitches/conformers AND macro compliant
   mechanisms (Howell-style flexures, snap-through latches, hard stops).
   One states+transitions schema in the core, rented by both kinds, so
   structural slice 5's state-dependent stability serves both. Macro
   compliant/flexure blocks enter via the pseudo-rigid-body route
   (rigid links + torsion springs — maps onto the existing axial/spring
   member machinery), not large-deflection FEA.
3. **Cost spine + placement**: feasibility-and-cost rungs 1–4 ·
   off-the-shelf 2b/3b + three-verdict scenario rule table ·
   nm-stick-placement · complementarity solver · termination nodes +
   representation escalation · viewer round 2 (argue-with-points, notes,
   3D route).
4. **Optimiser + processes**: processes as first-class objects +
   deferred commitment + se slice 5 FDM + toolpath rung 2 · the
   optimiser as a long-running job (annealing outer, CMA-ES inner,
   Chebyshev, surrogate ladder, BO scheduler under the house guard,
   niching, searching→improving, incumbents in DB) · hierarchical Pareto
   fronts + solve provenance (naive-first) · photoswitch + structural
   slice 5 · molecular density_at rungs 1–2, conformers, cleavage
   records.
5. **Coupling + adjacents**: cross-domain coupling screen · catalogue
   ingestion layers + availability preference · radiometric transport +
   irradiance contract (microfluidics PARKED) · bookmarks + digest
   sensitivity + requirements-level scenario comparison · se_propose /
   nm 4b verdicts+apply.

## Open questions

- Where exactly the validator-says-no / cost-term-says-expensive boundary
  sits per check — a design decision; the existing answer is
  capability-resolved severity (`se-feasibility-and-cost.md`), extend it.
- How much of the DFT fragment cache survives junction effects.
- Is the strut-graph intermediate expressive enough for the molecular
  side, or does that need its own level?
- Tolerance stack-up is global while interface contracts are local —
  joint-graph-derived chains (`se-feasibility-and-cost.md` rung 4) are the
  planned bridge; does it suffice?
- Cost-model granularity for the process lattice: unit cost vs tooling
  amortisation vs lead time, and how they trade in the objective.
