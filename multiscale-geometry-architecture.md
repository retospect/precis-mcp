# Multiscale Geometry & Optimisation Model

A single representation and optimisation framework spanning coarse functional
blocks down to atomic detail, usable for both manufactured mechanical parts and
molecular design.

---

## 1. Core idea

One geometry currency, many layers on top.

The model does not switch data structures as it refines. A tolerance box at the
top and a set of atomic coordinates at the bottom are the same kind of object,
queried the same way. What changes between levels is resolution and which cost
terms are active, not the representation.

Everything else — physics, manufacturing, synthesis, assembly — is a *field* or
a *cost term* defined over that shared geometry.

---

## 2. Geometry representation

### 2.1 Signed distance fields

The base primitive is a signed distance function: for any point in space, how
far to the nearest surface, negative inside, positive outside.

Why this and not meshes or B-reps:

- **Tolerances are free.** Offsetting the zero level set by ±δ gives you the
  tolerance envelope directly. No re-modelling.
- **Booleans are trivial.** Union is `min`, intersection is `max`, subtraction
  is `max(a, -b)`. Critical, because the whole joint system below depends on
  cheap subtraction.
- **Differentiable almost everywhere.** Gradient descent on positions,
  orientations and blend weights works without special-casing.
- **Inside/outside is one sign flip.** Matters for fluids (§6.3), where you
  need the void rather than the solid.

Known weak spot: meshing from implicit surfaces is fiddly at thin features and
sharp corners. Mitigated in §7.4 — since we *generate* geometry rather than
import it, we can make meshability a cost term.

### 2.2 Objects and transforms

Each object is a local shape function plus a transform matrix (position,
rotation, optionally scale). Queries transform the point into the object's local
frame and evaluate there.

Collision and overlap follow a two-stage pattern:

1. **Broad phase** — cheap bounding-volume tests to reject most pairs.
2. **Narrow phase** — exact distance-field evaluation on survivors only.

This is what keeps the cost manageable as object counts grow into the thousands
(molecular fragments especially).

### 2.3 Abstraction levels

Descent through levels of detail preserves the *interface contract* and replaces
the *interior*:

| Level | Mechanical | Molecular |
|---|---|---|
| Functional block | Box with envelope + tolerances | Fragment slot with attachment points |
| Typed block | "Bracket, load-bearing, this envelope" | "Rigid aromatic core here" |
| Intermediate | Strut/node graph (§7.3) | Fragment identity chosen |
| Concrete | Filleted solid, process-legal | Atomic coordinates from DFT |

At every level the block still exposes the same contract to its neighbours, so
a coarse-level clash check remains valid after refinement. This is the property
that makes the whole thing tractable: you never have to re-solve the top when
you refine the bottom.

### 2.4 Interface contracts

Where two blocks meet there is an explicit contract, not just adjacency:

- Mechanical: mating surface, its normal, permitted relative motion, load path.
- Molecular: bond site, bond type, valence, permitted torsion range.

Contracts are what make coarse-level reasoning sound, and they become boundary
conditions for the physics solvers later (§6.5).

---

## 3. Spatial reasoning: volumes and scenarios

### 3.1 Parts own several volumes, not one

A part is not a single solid. It carries a bundle of volumes:

- **Static occupied volume** — where the material is in the assembled state.
- **Assembly swept volume** — the shape swept along its insertion trajectory.
- **Tool access volume** — the space a driver, wrench, torch or manipulator
  needs, itself swept.
- **Motion envelope** — union over the reachable pose set, for anything that
  moves in service.

All of these are swept volumes. A screw going in is not a shape, it is a shape
swept along a path. So is a screwdriver, a robot arm, a hand reaching in for
maintenance, and a molecular fragment rotating into place. One mechanism covers
all of them.

### 3.2 Configurations vs. paths

A configuration is a pose vector. An envelope is the union over some set of
configurations.

- **Continuous sweep** — union over a path. Needed for insertion, for motion.
- **Discrete sampling** — union over a handful of poses. Usually enough for
  maintenance access, and far cheaper.

Cross-configuration queries are the useful ones: *which pairs clear in every
configuration* (safe), *which clear only in some* (ordering constraints).

### 3.3 Scenarios

Not three phases — **n**. Don't hardcode the list. Each scenario is a named
bundle of:

- allowed motions / reachable configuration set,
- which volumes are active,
- a rule table over volume pairs.

Scenarios worth having: assembly, operation, maintenance, diagnostic access,
disassembly, inspection, thermal expansion state, shipping/packed state.

The rule table has three verdicts, not two:

| Verdict | Meaning |
|---|---|
| **Must clear** | Overlap is a violation |
| **May touch** | Contact permitted, not required |
| **Must contact** | Contact is *required* — hard stops, seats, preloads |

"Must contact" is genuinely different from permission and is easy to forget. A
hard stop that fails to contact is as broken as one that interferes.

### 3.4 The time dimension

Interference is 3D + sequence. Two volumes may overlap safely if they are
separated in the assembly ordering — the screwdriver's volume and the adjacent
part's volume can share space if the part goes on afterwards.

So assembly-order constraints fall out of the cross-configuration analysis in
§3.2, and the ordering itself becomes a discrete variable in the optimiser.

### 3.5 Removability and free space

Test for "can this part come out":

1. Sweep the part's volume along the candidate path.
2. Intersect against the union of all other active static volumes.
3. Empty intersection ⇒ path valid.

For "is it actually out", use a free-space connectivity test: the part must
reach the unbounded exterior. In practice, escape from the bounding volume of
the relevant subassembly.

**Caveat:** concave assemblies. Being outside the bounding box of the
subassembly you care about does not mean being clear of the wider machine. Be
explicit about *which* bounding volume counts as free.

**Useful trick:** plan disassembly, then reverse it. Removal is generally an
easier search than insertion.

---

## 4. Constraints: the unilateral family

Several apparently different things are one formulation.

### 4.1 Complementarity

For each connection: either the gap is zero and there is force, or there is a
gap and zero force. Never both.

This single form covers:

- **Cables / ties** — tension only, go slack under compression.
- **Contacts / block stacks** — compression only, no tension.
- **Hard stops** — the "must contact" verdict from §3.3.

Same maths, opposite signs for the first two. This is what makes tensegrity and
dry-stacked masonry the same problem.

### 4.2 Form-finding

Unilateral constraints make stiffness nonlinear — a cable going slack changes
the structure's behaviour qualitatively. So tensegrity and prestressed networks
need form-finding (solving for the equilibrium prestress state), not plain
analysis.

### 4.3 Where bonds differ

A covalent bond resists both tension and compression, so it is *not* unilateral
— but it is strongly asymmetric: compression stiffens sharply (electron clouds
repelling), tension softens and then fails. Morse potential, not a symmetric
spring. A nonlinear two-sided constraint with a failure point.

Non-bonded contacts, by contrast, *are* the compression-only case, and slot
straight into §4.1.

---

## 5. Optimisation architecture

### 5.1 Soft costs, not hard gates

The validator only rules out the genuinely impossible. Everything else is a
penalty term to be minimised. The goal is a *good* solution, not merely a valid
one.

### 5.2 Hybrid search

- **Gradient descent** on continuous parameters — positions, orientations,
  blend weights, wall thicknesses. Works because the SDF is differentiable.
- **Simulated annealing** on discrete jumps — which module fills a slot, which
  process, which joint type, which assembly order.

This split is standard in molecular docking and transfers directly.

### 5.3 Surrogates and screening

Never run the expensive physics on the whole search space.

1. **Closed-form estimates first.** Beam deflection, thermal resistance
   networks, crude stress formulas. Wrong by 20–30%, but they *rank* candidates
   correctly, which is all screening needs.
2. **Fitted surrogates next.** Sample ~50 designs with the real solver, fit a
   Gaussian process. GPs are preferred because they report their own
   uncertainty, so you know when to fall back to the real solver.
3. **Full solve last**, on the top two or three candidates only.

### 5.4 Bayesian optimisation

The adaptive layer. Maintain a probabilistic model of the objective; choose the
next design to evaluate via an acquisition function (expected improvement is
the usual one) balancing predicted quality against model uncertainty.

Right tool when evaluations are expensive — converges in tens of samples rather
than thousands.

**Limit:** degrades above roughly 20 continuous dimensions. So parameterise each
branch *tightly*. Do not expose every vertex.

### 5.5 Limiting degrees of freedom

This is the main tractability lever, and it applies at every level:

- Freeze rigid fragments; allow only torsions at spacers. Thousands of
  coordinates collapse to tens.
- Parameterise mechanical parts by feature (wall thickness, rib count, fillet
  radius), not by surface control points.
- Fix the coarse layout before refining interiors.

### 5.6 Worst-case handling

For uncertain inputs (convection coefficients from handbook tables, friction,
material scatter):

- **Cheap:** perturb up and down, keep whichever direction worsens the
  objective, one extra solve.
- **General:** interval arithmetic — propagate the whole range, take the
  worst-case output.

Note the sign is objective-dependent. Lower convection coefficient is worse for
heat sinking, but *hotter* may be exactly what you want if you are using thermal
expansion to close a fit.

---

## 6. Physics layers

All defined over the same geometry. The SDF gives the domain; the analysis mesh
is generated from it; material properties are per-region attributes.

### 6.1 Static structural

Finite elements, tetrahedral mesh from the SDF. Stress, deflection and safety
factor become cost terms.

### 6.2 Dynamics

Full time-domain stepping is expensive and accumulates error, and needs mass and
damping, not just stiffness. Two cheaper routes:

- **Modal analysis** — extract natural frequencies once, check nothing in
  service excites them. Usually sufficient.
- **Frequency-domain / harmonic response** — transform the load into
  frequencies, solve each independently, superpose. Enormously faster.

**Restriction:** frequency-domain requires linearity. Contact, friction,
plasticity or large deflections break superposition and force time-stepping.

**Also:** fatigue. Cyclic loads kill parts well below static strength, and this
applies to photoswitch cycling too (§8.5).

### 6.3 Fluids

Needs a mesh of the *void*, with boundary layers — thin cells hugging walls. The
SDF gives the void for free (negate it), but the mesher and solver do not share
with the structural side.

**Use one-way coupling:** solve flow with rigid walls, take the pressure field,
apply it as a structural load. No iteration. Valid while deflections are small
enough not to change the flow meaningfully — nearly always true for stiff metal
parts. Two-way fluid-structure interaction is a large step up in cost and pain;
avoid unless forced.

### 6.4 Thermal

The friendliest. Steady-state conduction is one scalar per node versus three for
elasticity, and reuses the structural mesh directly.

Natural chain: solve temperature → feed in as thermal strain → thermal stresses.
Clean and one-way.

Fiddly bit is convection boundary conditions. Handbook correlations are fine
(natural convection off a vertical plate, forced air over a heat sink), treated
as uncertain per §5.6.

### 6.5 Boundary conditions

These come from the interface contracts of §2.4. A bolted joint becomes a
constraint or a spring in the model; a mating face becomes a contact pair. The
contract is the single source of truth, so the structural, thermal and assembly
views stay consistent.

---

## 7. Manufacturing

### 7.1 A lattice, not a hierarchy

Processes overlap partially and do not nest. 3D printing, laser cutting, die
cutting, CNC milling, turning, injection moulding, plus chemical apparatus for
the molecular side.

So do not target a named process. Describe each process by **capability tags**
and match requirements against them:

- working dimensionality (2.5D profile vs. full 3D)
- minimum feature size, maximum part size
- internal voids? undercuts? closed cavities?
- draft angle requirement, wall thickness uniformity
- overhang angle limit / support requirement
- achievable tolerance and surface finish
- material compatibility
- cost curve vs. quantity

The design then carries *requirements*, and process selection is a matching
problem.

### 7.2 Intersection and migration

Because capabilities are sets, you can design to the **intersection** of
several, and the same geometry is legal for all of them.

This is the most useful single consequence of the lattice: *"printable now,
mouldable later"* becomes an explicit requirement. Prototype on a printer, move
to tooling at volume, no redesign. You pay some design freedom for it, and that
trade should be exposed as a knob rather than assumed.

Quantity is therefore an *input* to the optimiser, since cost curves cross —
printing wins at ten pieces, moulding at ten thousand.

### 7.3 Intermediate representations

Not just coarse and final. The useful middle layer is a **strut/node graph** —
the "stick figure" level.

Topology optimisation naturally produces organic, material-where-needed shapes:
a bracket as a web of struts rather than a solid block. Represent it as a graph,
then interpret it downstream:

- thicken struts into a solid for printing,
- reinterpret as tubes or as sheet ribs,
- collapse to a simple prism if the process demands it.

The graph is also a good level at which to run the cheap structural surrogates
of §5.3 — beam elements are fast.

### 7.4 Design-for-analysis as a cost term

Because geometry is generated, not imported, meshability can be optimised for:

- penalise features thinner than a threshold,
- enforce a minimum fillet radius everywhere.

Conveniently, the manufacturing and analysis constraints agree here: sharp
internal corners are stress concentrators, so fillets are wanted regardless.

---

## 8. Joints and fasteners

### 8.1 Joints are first-class objects that own geometry

Do not model holes as features of parts. Declare a joint between two faces; the
joint instance then **emits negative volumes** into every part it passes
through:

- clearance hole in the near part,
- tapped or through hole below,
- counterbore or countersink,
- driver access cone,
- nut or wrench envelope,
- for welds: torch access volume and heat-affected zone.

The parts do not own those holes — the joint does. Swapping M4 bolt for snap-fit
regenerates all affected geometry automatically.

### 8.2 Declare requirements, not fasteners

You know the load, its direction, and whether the joint must be reversible. You
should not have to pick the fastener.

Joint requirement:

- load magnitude and direction (and cyclic or static)
- reversibility: permanent / serviceable / frequently opened
- alignment precision needed
- sealing, electrical continuity, thermal path if relevant

Each joint type in the library advertises what it can carry and what it demands:

| Joint | Carries | Demands |
|---|---|---|
| Bolt + nut | High, reversible | Two-sided access, tool envelopes |
| Screw into tapped hole | Medium, reversible | One-sided access, thread depth |
| Rivet | Medium, permanent | Two-sided access at set time |
| Weld | High, permanent | Compatible materials, torch access |
| Snap-fit | Low–medium, reversible | Flexible polymer, mouldable geometry |
| Adhesive | Distributed, permanent | Surface prep, cure time, bond area |

### 8.3 Coupled discrete choice

Joint choice, process choice and material choice constrain each other — a
snap-fit implies a polymer and a moulding-compatible geometry; a weld implies
compatible metals and access. So they belong in **one** outer discrete search,
not three independent ones.

This outer loop is the most consequential part of the system: it is where the
big cost differences live, and it is cheap to explore relative to the physics.

### 8.4 Synthesis as a pluggable back-end

Same architecture, molecular side. The geometry and physics layers are
unchanged; what swaps is the cost function scoring *"can this be made"*:

- **Standard chemistry** — retrosynthesis search: decompose the target into
  commercially available starting materials via known reactions. Click chemistry
  is attractive precisely because it restricts you to a small reliable reaction
  set, which keeps the search tractable.
- **Atomic assembly arm** — no retrosynthesis. Instead it is the tool-access
  problem from §3.1, one abstraction level down: can the manipulator reach the
  site, along a collision-free path, without disturbing what is already placed.

The pleasing symmetry: the assembly-arm case reuses the screwdriver machinery
verbatim.

### 8.5 Yield and kinetics

Thermodynamic feasibility is not enough — a reaction that works once per litre
per millennium is useless. Yield and rate come from **activation barriers**, not
product energies.

- Transition-state searches in DFT are considerably more expensive than
  ground-state ones, so evaluate only on final candidates.
- Early screen: penalise strained geometries and crowded reaction sites, which
  are what usually kill yield in practice.

---

## 9. Molecular instantiation

Everything above applies; this section is what is specific.

### 9.1 Fragment library

Building blocks with:

- attachment points (the §2.4 contract: bond type, valence, torsion range),
- precomputed DFT signature — geometry, energies, for switchables *both*
  isomer states,
- spacers with known length and flexibility (e.g. C₂H₄ units),
- functional units (e.g. azobenzene photoswitches).

**Key move:** fragment properties are computed once, independently, and cached.
Assembly then becomes combinatorial rather than quantum. Re-run DFT only at
junctions, where the electronics actually couple.

### 9.2 Non-bonded interaction menu

Each of these can be a *want* or an *avoid*, with a sign on the term:

| Interaction | Character | Notes |
|---|---|---|
| van der Waals / dispersion | Weak, universal, short range | Sets packing density |
| Electrostatic | Charge and dipole patterns | Long range; patterns can be designed to interfere or not |
| Hydrogen bonding | Directional | Strong geometric preference, good for programmed registry |
| π-stacking | Face-to-face, ~3.4 Å optimum | Distance *and* orientation term |
| Cation–π | Charge against aromatic face | Often overlooked |
| Steric hindrance | Pure repulsion | The barrier-shaping term |
| Hydrophobic effect | Solvent-mediated | Only meaningful with explicit environment |

These are standard force-field terms; the design content is in choosing the
signs and weights.

### 9.3 Ratchets and bistability

Combine the above with steric hindrance and you get an **asymmetric energy
barrier** — which is exactly a one-way ratchet. Molecular bistability is the
same construction: two minima separated by a barrier, with the asymmetry
determining directionality.

Hysteresis follows directly from barrier height relative to thermal energy.

### 9.4 Photoswitchable actuation

For a bistable azobenzene driving a tensegrity mechanism:

1. Solve form-finding (§4.2) **twice**, once per isomer state.
2. Actuation stroke is the difference between the two equilibria.
3. Bistability comes from the network having two stable equilibria with a
   barrier between them — handled by the complementarity solver of §4.1.
4. **Check the switching pathway.** Sweep through intermediate configurations
   and verify nothing collides or over-strains on the way. This is the
   assembly-path problem of §3.5, one level down.

### 9.5 Photon interaction

Another field over the same geometry:

- absorption cross-sections per fragment, from DFT,
- light penetration and self-shadowing if the structure is optically thick,
- wavelength selectivity between the two switching directions.

### 9.6 Environment

Molecular behaviour differs completely in water versus vacuum. Solvent must be
an explicit part of the model — either implicit (dielectric continuum) or
explicit — and it determines whether the hydrophobic term in §9.2 means
anything.

Temperature in the statistical sense also matters: the structure jiggles, and
entropy competes with the minimum-energy pose. A design that is only marginally
stable at 0 K is not a design.

---

## 10. View-dependent form

For macro and organic structures: the object should read as one thing from one
direction and something else from another.

### 10.1 Formulation

Each view is a target silhouette — a 2D distance field. Project the solid along
the view direction, compare the projected outline against the target, penalise
mismatch. Just another cost term over the same SDF.

### 10.2 The maximal solution is free

Intersect the extrusions of all target silhouettes. That gives the **largest**
shape satisfying every view simultaneously.

Two consequences:

- If the intersection is empty or too thin, the views are incompatible and you
  know immediately, before any optimisation.
- Removing material can only shrink silhouettes, so once inside that envelope
  you can carve freely for weight, structure or aesthetics — the view
  constraints stay satisfied as long as each silhouette's *outline* is
  preserved.

### 10.3 Caveats

- Silhouette targets fight the structural and printability terms directly.
  Weight them; do not make them hard constraints.
- Orthographic vs. perspective matters. At finite viewing distance the
  projection is a cone, not a cylinder, and the intersection trick needs
  adjusting accordingly.

---

## 11. Open questions

- Where exactly to cut between "validator says no" and "cost term says
  expensive" — the boundary is a design decision, not a given.
- How much of the DFT fragment cache survives junction effects before it stops
  being a good approximation.
- Whether the strut-graph intermediate is expressive enough for the molecular
  side, or whether that needs its own intermediate level.
- Handling of tolerance stack-up across many interface contracts — currently
  the contracts are local, and stack-up is global.
- Cost model granularity for the process lattice: unit cost vs. tooling
  amortisation vs. lead time, and how those trade against each other in the
  objective.
