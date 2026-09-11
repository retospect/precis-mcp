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
| complementarity solver | **here, §Complementarity** | new |
| optimisation stack (surrogates, BO, annealing) | **here, §Optimisation** | new |
| requirement→joint matching | **here, §Joint matching** | new |
| view-dependent form | **here, §View-dependent form** | new |

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
  per-kind convention an agent must guess — the current state (cad DSL
  docstring says mm, se stores m, nm stores Å, one shared grammar) is the
  anti-pattern this replaces. Explicit units kill the two observed LLM
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
- **Display (revised, Reto 2026-09-11): the underlying canonical,
  e-notation, unit named in headers** — `2.3e-9 m`, `1.2e3 N`. One format
  at every scale, cross-scale comparisons need no prefix arithmetic, no
  prefix table in renderers, and the MCP does exactly ONE conversion
  (inbound). The zero-counting hazard was an *input* problem; ingest-any
  solves that side, and `3e-9` on output is unambiguous. ISO-prefix
  prettification (2.3 nm) is a web-UI concern only, if ever.
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
- **Boundary: atom interactions are separate** (Reto 2026-09-11). This
  policy covers the geometry currency (lengths, forces, tolerances)
  only. Interaction physics — kT thresholds, π-stack energies, nm
  mechanics capacities — keeps its own native quantities in its own
  modules (`nm-stick-placement.md`, `precis_nm/mechanics.py`) and is
  never routed through the unit-defaulting ladder: cost terms *over* the
  geometry, not lengths *in* it.

Owner: se/nm handlers + cad DSL docstring; lands with the dogfood-fix
cycle. Cross-kind seams (`bind_structure`, `realized-by`, formfind feeds)
become trivial once internal rep is shared. The clearance sign-flip
(gr334763) and the box half-extent ambiguity (gr334785 — decision
pending) are the same family and should land in that cycle.

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
