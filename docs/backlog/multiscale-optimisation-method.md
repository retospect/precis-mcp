---
status: draft
title: multiscale optimisation method — annealed discrete outer, shape-functional inner, preferred-number term
prio: high
model: opus
---

# Intake routing (added 2026-09-15)

Reto's method-transfer note from the 2026-09-15 voice session, taken in as
a leg doc under the map `multiscale-design-architecture.md`. Same
precedence rules as the main spec: where a concept already has an owner,
**that owner wins**; this doc holds only the deltas and the genuinely new
subsystems. Two corrections were applied on intake and are marked
**CORRECTED** where they land: the tier combination in §5.4 is `min` of
offset wells (the note's `max` inverts the intent — see there), and the
level-set layer enters as phase-3 polish over the shipped SIMP screen
first, with per-block → multiphase-seam → topological-derivative
nucleation as the upgrades that make it the whole continuous layer (§2).
Follow-ups from Reto's review the same day: bilateral Huber + softmin
(§4), and a §6a Symmetry section the note's table implied but never
defined.

Section → owner:

- §1–2 the two-layer split — spec §3.1 owns (already decided, same
  memetic/basin-hopping shape). Held here: the *forcing* argument (no
  integral finds a minimum; stationarity gives critical points).
- §3 shape functional over a level set — **new, owned here**, with the
  SIMP/feature-parameterisation reconciliation in §3.6.
- §4 fixed reference normalisation — refines spec §3.3 ("penalties
  normalised against the objective's scale") under the map's house guard
  (weights human-set). Decision held here.
- §5 preferred-number term — **new, owned here**; generalises spec §4.6's
  lattice-preference table from placement grids to declared measurements.
  The substance of the technical note / disclosure (open item 1).
- §6 cost-in-fields + cross-domain screen — spec §2.4 owns the screen;
  held here: the sharper cost-unit phrasing.
- §7 discrete move set — spec §3.1 owns the list. Deltas held here:
  atomic merge-then-resplit, LLM-proposed split surfaces with random-cut
  hedge, demand-driven LLM consultation.
- §8–9 scale mapping — consistent with spec §4.9 (its `density_at`
  fidelity ladder is this note's MM→DFT ladder in the other costume).
  Held here: alchemical relaxation (§9.3), new.
- §10 open items — carried, updated.
- §6b pcb as second renter — `pcb-guided-place-route.md` /
  `pcb-se-binding.md` own pcb; held here: the term→layer split and the
  seed_placement bridge.

---

# Multiscale design optimisation — method

The one-sentence method: split the design space into a discrete outer
layer searched by annealing and a continuous inner layer solved by
gradient descent on a shape functional, where every cost term — physics,
manufacturability, and aesthetics of dimension alike — is written as an
integral over the domain so that they all contribute to a single boundary
velocity field.

## 1. Why the split is forced

The design space is genuinely mixed:

| Discrete | Continuous |
|---|---|
| Lattice type and pitch index | Wall thickness, tube diameter |
| Fastener size (catalogue enumeration) | Fillet radius |
| Which interface in a closed loop to relax | Hole centre position within its tolerance |
| Block merge / split topology | Boundary shape generally |
| Process choice, pull direction, build orientation | Any declared measurement |
| Material class | |

No integral finds the minimum: integration gives volumes and expectations
— how much and on average, not where — and stationarity of a functional
gives critical points, not global minima. So: anneal the discrete
choices; descend the continuous geometry inside each fixed discrete
configuration; the inner descent's converged value is the energy the
annealer sees. This is spec §3.1's memetic/basin-hopping structure; the
functional formulation does not escape the outer search, it makes the
inner solve well-behaved and topology-flexible.

## 2. The continuous layer: shape functional over a level set

Domain implied by a signed-distance field $\phi$: $\Omega = \{x :
\phi(x) < 0\}$, $\partial\Omega = \{x : \phi(x) = 0\}$. With a smoothed
Heaviside $H_\epsilon$ and smoothed delta $\delta_\epsilon$ over a thin
band:

$$\int_\Omega f\,dx = \int_D f\,(1 - H_\epsilon(\phi))\,dx, \qquad
\int_{\partial\Omega} g\,ds = \int_D g\,\delta_\epsilon(\phi)\,|\nabla\phi|\,dx$$

No surface extraction is ever needed during optimisation; cost is
independent of how convoluted the boundary is. Marching cubes is for
export and viewing only — consistent with the shipped kernel posture.

The functional and its derivative:

$$J(\Omega) = \sum_i \frac{1}{R_i}\,J_i(\Omega), \qquad
J_i = \int_\Omega f_i\,dx + \int_{\partial\Omega} g_i\,ds$$

$$dJ(\Omega;\theta) = \int_{\partial\Omega} v_n\,(\theta\cdot n)\,ds$$

The scalar field $v_n$ is the output of the whole cost stack — stress,
mass, draft angle, fillet radius, preferred-number pull all contribute
additively to the same field. Update by Hamilton–Jacobi advection:

$$\frac{\partial\phi}{\partial t} + v_n\,|\nabla\phi| = 0$$

Topology changes happen on their own: holes close, ligaments pinch off,
features merge — no remeshing, no explicit topology moves at this layer.
The asymmetry stands and is *covered*, not a gap (§3.6): holes do not
nucleate in the interior; nucleation stays an annealer move plus the
SIMP screen upstream.

### What does not fit in the functional

Logical and relational requirements do not integrate: must-come-apart,
single shared insertion direction, separability/contamination graph,
distinct BOM/tool counts, catalogue membership. Two options per
requirement: smooth into a soft penalty where a real continuous measure
exists (swept-volume overlap, clearance depth), or freeze per discrete
branch and let the annealer own it. **Default to freezing — fake
smoothness is worse than an honest discrete variable.** Consistent with
the spec §2 constraint catalogue's D-kind.

### Scoping notes (intake additions)

- **Stress-type terms** need aggregated form (p-norm / KS function) to be
  shape-differentiable; pointwise stress is not. One line in the
  technical note.
- **Each inner iteration costs a state solve + an adjoint solve** — §5's
  field accounting applies to the inner loop itself. The surrogate
  ladder (Addendum A7) stands in for early iterations; full adjoint only
  near convergence, the same escalation logic as spec §3.7.
- **Fixed vs moving reference.** Chebyshev scalarisation (spec §3.3
  default) updates its utopia/nadir points adaptively in most
  implementations — that is the same chase-your-tail failure as adaptive
  normalisation, one level up. The fixed $R_i$ of §3 must pin the
  scalarisation's reference point too.

### Where the level set sits — three continuous resolutions, one formulation (CORRECTED scope)

The house already ships two other "continuous" layers beside it:

| Stage | Field advected | Topology | Role |
|---|---|---|---|
| Phase 2 sizing | feature vector (wall thickness, rib count, fillet radius) | fixed family | BO-friendly, few DOF — the "parameterise by feature, never by vertex" invariant stands here |
| Topology generation | SIMP density $\rho$ (`precis/structsolve/simp.py`, advisory) | free — nucleates holes | screens layouts; the staircased output is a starting $\phi$ |
| Phase 3 boundary polish | level set $\phi$ (this method) | merge/pinch only | crisp boundary where every boundary-touching term — draft, fillets, surface area, preferred-number pull via §5.6's chain rule — contributes to one $v_n$ |

Same smoothed-Heaviside machinery under all three; what differs is which
field is advected. The density→level-set hand-off is the standard
two-stage of the literature, and it maps cleanly onto the phase loop:
SIMP answers "where should material be" cheaply and with free
nucleation; the level set answers "exactly where is the boundary" with
every cost term in the gradient.

The polish scoping was driven by the nucleation asymmetry; that is
fixable inside the method. The **topological derivative**
(Allaire–Jouve–Toader) gives $dJ$ for opening an infinitesimal hole at
every interior point, so nucleation becomes a gradient-informed move
rather than a blind annealer move. With it the level set can be the
whole continuous layer; SIMP→level-set stays the default route only
because SIMP is shipped.

The level set is not confined to one hierarchy layer — it applies
wherever there is a boundary to advect:

- **Per block** — one $\phi$ inside the block's envelope; the contract
  envelope is the design domain $D$. First target.
- **Assembly level — multiphase level set.** One $\phi_k$ per block (or
  per material); a seam is where two zero-sets meet and is itself a
  boundary with its own $v_n$. Split surfaces stop being fixed SDFs and
  become optimisable, mating rules become seam integrals
  $\int_{\text{seam}} g\,ds$, and merge/split moves are phase
  *relabelling* rather than geometry surgery — block topology = phase
  labels. This makes §6's "merged blocks remember their seams as
  internal load paths" literal.
- **2D sections and projections** — the silhouette term of map
  §View-dependent form is a 2D level set on a projection; same
  machinery, and the plane on which a symmetry is asked for (§Symmetry)
  is the same object.

Three independent upgrades — per-block first, multiphase for assemblies,
topological derivative for nucleation — none blocking the others.

## 3. Normalisation, not weights

Terms have genuinely different units — joules, kilograms, square metres,
currency. A "weight" in such a sum is secretly a unit conversion factor
with a preference smuggled inside it.

**Decided: fixed reference values $R_i$**, measured once from a starting
design, held constant thereafter. Refines spec §3.3's "normalised
against the objective's scale" into a concrete rule.

**Rejected: adaptive normalisation** (dividing by the running value of
the term) — it keeps a successfully-shrinking term at constant apparent
magnitude, so it goes on fighting terms that have already conceded; the
optimiser chases its own tail.

Long-run target: a single currency. Once cost-in-money is the unit for
mass, machining time, BOM lines, tooling amortisation and scrap, the
scaling question largely dissolves and PCB terms sit in the same sum as
mechanical ones. Scenario (spec §1.3) sets quantity and therefore the
exchange rates.

## 4. The preferred-number term (the novel piece)

Motivation: optimisers emit 8.3586 mm; humans want 10 mm. Round
dimensions are not cosmetic — they make validation, measurement,
inspection and communication tractable, and they make a design legible
to the person holding the calipers. Standard practice rounds at the end,
by hand — discarding the optimiser's knowledge of which dimensions can
afford to move and which cannot.

Rejected alternatives:

- *End snapping* — silently violates constraints; no feedback path.
- *Quantised search* — destroys the gradient; explodes the annealer's
  discrete space.
- *True sawtooth penalty* — discontinuous gradient at the midpoints
  between wells causes chatter. **Note:** tip-only Huber smoothing does
  not remove this — the peak at $d=p/2$ keeps its cusp. Resolved by
  bilateral smoothing, §5.4.

### The term

For a declared measurement $m$ and pitch $p$, distance to the nearest
multiple $d_p(m) = |m - p\,\mathrm{round}(m/p)|$, with Huber-style
smoothing of the well tip so the gradient is continuous at the bottom:

$$h_\epsilon(d) = \begin{cases} d^2 / 2\epsilon & d < \epsilon \\
d - \epsilon/2 & d \ge \epsilon \end{cases}, \qquad
W_p(m) = A_p\,h_\epsilon\!\big(d_p(m)\big)$$

A V-shaped well with a rounded tip: pull magnitude $A_p$ on the flanks,
vanishing only within $\epsilon$ of the bottom. Deliberately sharper
than a cosine, whose gradient vanishes exactly where a decisive pull is
wanted. Smoothing radius $\epsilon \approx 10^{-4}\,p_{\min}$ — a tenth
of a micron on a 1 mm grid, far below the three significant figures any
real dimension carries.

### Overlapping tiers — min of offset wells (CORRECTED)

Tiers at multiple pitches: 100s, 10s, 5s, 2.5s (and finer where wanted).
The voice note proposed $P(m) = \max_p W_p(m)$ to avoid compounding —
**max is wrong, and inverts the intent.** The coarsest tier's sawtooth
has amplitude $\sim (p/2)\,A_p$, which exceeds every finer tier's almost
everywhere because $A_p$ grows only logarithmically while $d_p$ ranges
linearly in $p$. Check $m = 10$ mm: $W_{10} = 0$ but $W_{100} =
10\,A_{100}$, so max drags a perfectly round 10 mm *away* from round
toward 0 or 100. Under max the only rest points are multiples of the
coarsest pitch — precisely the "hauled to 100" behaviour the note set
out to avoid.

The intended semantics — *a dimension settles on the coarsest round
value that is good enough* — is a union of wells, i.e. **min** over
tiers with per-tier depth offsets:

$$P(m) = \min_p \big[\,A_p\,h_\epsilon(d_p(m)) - B_p\,\big], \qquad
B_p = B_0\Big(1 + \lambda \ln\frac{p}{p_{\min}}\Big)$$

- Every grid point of every tier is a rest point (if 5 suffices, it
  stops at 5); one active tier per basin, so the sum's compounded-well
  pathology is still avoided.
- Coarse wells deeper via $B_p$: among reachable options the coarser is
  preferred, but nothing is hauled across a basin boundary by the term
  itself.
- Constraint: the depth increment must be smaller than the transport
  cost to a coarser grid point ($B_{p'} - B_p <$ the uphill work), else
  coarse wells swallow fine ones — sets a bound on $\lambda$ during
  calibration.
- **Bilateral Huber (Reto 2026-09-15).** Cap the peak at $d = p/2$ with
  an inverted quadratic of the same width $\epsilon$: linear flanks,
  quadratic at both bottom and top, so the gradient is continuous
  everywhere within a tier. The peak becomes a zero-gradient ridge of
  width $2\epsilon$ — an unstable equilibrium of measure zero that any
  physics gradient tips. This removes the chatter mechanism outright,
  closing the §5.2/§5.3 tension: descent alone is well-defined at the
  midpoint. Across tiers, replace the hard $\min$ with a **softmin**
  (log-sum-exp, sharpness $\beta$) to smooth the tie surfaces between
  wells the same way. Cost: pull vanishes within $\epsilon$ of the top
  as well as the bottom — irrelevant at $\epsilon = 10^{-4}\,p$.
- Corollary annealer move: **snap to next-coarser tier** — the scalar
  cousin of spec §4.6's lattice-step move, for when physics leaves a
  dimension in a fine well that a coarser one would also satisfy. With
  bilateral smoothing this is an accelerator (a basin *jump* descent
  cannot make), not a correctness requirement.
- Prior-art honesty: min-of-offset-wells is the standard multi-well
  potential of phase-field models. The novel claim stays §5.7's: a
  differentiable preferred-number term inside a multiphysics shape
  functional, competing with stress and mass on one gradient.

### Tiers from part size; coefficients become rules (Reto 2026-09-15)

Tiers are not a global table. Take the characteristic length $L$ of the
part (bbox diagonal, else largest dimension) and pick 3–4 pitches
$L/10, L/50, L/100$, each **rounded to the nearest 1-2-5 value** so the
pitch is itself a round number ($L = 87$ mm → 10, 2, 1 mm). Port and
mating-face extents sit on the same raster. This removes $p_{\min}$ and
$\lambda$ as free constants; the rest become fixed dimensionless
numbers or rules evaluated once on the starting design:

| Coefficient | Becomes |
|---|---|
| $\epsilon$ | fixed: $10^{-4}\,p_{\min}$ |
| $\beta$ (softmin) | fixed: blend width $= \epsilon$ |
| $B_p$ offsets | fixed fraction $\kappa \approx 0.5$ of the next-finer tier's peak height $A\,p_{\text{fine}}/2$ — the survival criterion (a fine grid point at distance $d$ from a coarse one is a local minimum iff $A_{\text{coarse}}\,d > B_{\text{coarse}} - B_{\text{fine}}$) applied as the definition |
| $A_0$ | rule: scaled so the pull ratio $\lvert\nabla P\rvert / \lvert\nabla J_{\text{physics}}\rvert$ on the starting design equals a target $\rho^* \approx 0.1$ |
| $R_i$ | rule: measured on the starting design (§3) |

Two dimensionless knobs remain — $\rho^*$ and $\kappa$ — with defaults.
No per-design calibration.

**Magnitude discipline.** The same pull ratio is reported per scenario
as a diagnostic (taken over the active well's $A_p$). Above $\rho^*$
anywhere after the run, the design is being rounded into being worse
and the report says so — same posture as spec §3.8's solve-provenance
advertising.

### Where the wells attach

To declared measurements in the part's own frame, relative to its
**datum** — hole diameter, datum-face to hole-centre distance, wall
thickness, overall length. Never to global coordinates. Chain rule from
measurement to design variables gives the contribution to $v_n$.

**A datum is a feature, not a DOF (Reto 2026-09-15).** The datum is a
named feature of the block — a face, an axis — resolved through the SDF,
so when the boundary advects the datum rides along and every
measurement declared against it stays meaningful. The chain rule
therefore has two paths: through the measured feature and through the
datum feature. Guard: the datum is never an independent design
variable. If it could slide freely the preferred-number term would
round a dimension by moving the datum instead of the geometry — a free
lunch that satisfies the well and changes nothing.

**Default datum = the block's pose frame** (se L1 gives every block an
envelope + pose). For a prismatic envelope that frame *is* the 3-2-1 —
three faces meeting at the frame origin; for a rotational primitive,
its axis plus base face. Because ports/contracts attach at the same
frame, the datum coincides with the mating interface by default, which
is what shortens the tolerance stack-up chain for free — consistent
with contracts carrying tolerance (spec §1.2). Explicit `datum:`
overrides only where a functional feature (a bore) should govern
instead. Datum conventions follow shape family, not process; holes are
secondary datums fixing rotation, never primary.

Build gap (se side, unspecced elsewhere): `MeasureSpec`
(`precis_se/measures.py`) is today a declared relation with a band and
an `origin` (provenance — `user | proposed`, not a frame). It needs (a)
a `datum:` reference resolving to a feature of the block's SDF and (b)
an evaluator $m(\text{design})$ returning the number from geometry
rather than reading a declared one. Prerequisite for the wells to
attach at all.

**Datum selection heuristics (Reto 2026-09-15).** A deterministic
ranking, `datum:` override by user/LLM; not an annealer variable — too
cheap to be worth a move.

- Round face → its axis (centre); square face → a corner, so dimensions
  are edge-referenced the way a caliper measures. Centre-referenced
  only when a symmetry plane is imposed (§6a), where it becomes the
  secondary datum.
- **Primary = the largest flat face**, so the projection is large and
  clean; generally perpendicular to the single assembly /
  fastener-insertion direction (spec §2.1's cone) — the face the part
  sits on.
- **Ports are free datums.** A face already fixed by a contract carries
  no optimiser DOF, so choosing it costs nothing and satisfies the
  no-free-DOF guard by construction. Default primary = largest port
  face.
- **Process setup supplies candidates**: FDM build-plate face (flat by
  construction), milling clamping face (fixturing scenario row, A3),
  moulding parting-plane side — never a drafted face. Datum choice
  therefore couples to the build-orientation variable; same discrete
  state.
- **Same-setup correlation** (A4 tags): datum and measured feature from
  the same setup share error, so prefer it.
- **Must be measurable**: probe/caliper access via the test/probe
  scenario row with the caliper as the tool — reuses swept volumes.
- Tertiary = a hole (rotation lock).
- Ranking score: area × flatness × functional (port / assembly-normal)
  × accessible.

### Prior art position

Renard / preferred-number series are ancient and universal; standard-
section selection in structural optimisation and quantisation-aware
training (the quantisation grid present during training, not applied
post hoc — LSQ is the direct analogue) are the nearest neighbours. What
appears absent: preferred numbers as a *differentiable* term inside the
optimisation, coupled to a multiphysics shape functional. This section
plus §2 is the substance of the technical note / disclosure — needs
diagrams (well shapes, tier interaction, min-vs-max-vs-sum comparison)
and a worked example.

## 5. Cost is counted in fields, not terms

The scaling insight: cheap terms merely read design state (measurements,
counts, volumes, distances — microseconds; a thousand are free);
expensive terms solve for a field (stiffness inversion, temperature,
flow — seconds to minutes); once a field exists, every further question
asked of it is near-free. The unit of cost per candidate is **how many
distinct physics fields it requires**, not how many terms are written.
Adding fatigue costs nothing — it reuses the structural solve; adding a
thermal term costs a whole field. Fields share machinery (same mesh,
same sparse solver, different material property and source) but not
cost. Ports onto spec §2.4's screen verbatim.

### Cross-domain screening (already owned by spec §2.4)

Analytic bound first, no solve (absorbed power × rough thermal
resistance; expansion-coefficient difference × span × swing) — cheap
enough to run all pairs. Sensitivity probe for ambiguous pairs. Full
coupled solve only for survivors. Report the checked/active/dismissed
matrix with margins as provenance; show the model only the delta of
newly-active couplings; re-screen when the design moves substantially.
This is the machinery that catches the couplings nobody thought to
check.

### Worked example: bolt spacing

Three fields disagree about one variable: structural pushes bolts apart
(lever arm, shear per bolt); thermal pushes them together (expansion
mismatch loads fasteners in shear); geometric terms bound both ends
(edge distance, tool access, driver swept volume). A genuine interior
trade-off — and the positions then snap to the lattice, making final
placement an annealer move rather than a descent result.

### Grid sizing

The grid resolves features, not tolerances: 2–3 cells across the
thinnest wall, ~200 across the part, adaptively refined near boundaries.
Tolerances are analytic offsets and stack-ups; hole positions and
feature relations live in the parametric layer at full double precision
and never touch a voxel.

## 6. Discrete layer — the move set (deltas to spec §3.1)

Spec §3.1 owns the list; this session adds:

- **Merge-then-resplit as one atomic move**, not a composition — the
  intermediate scores worse and would be rejected before the resplit
  could happen (k-opt logic; barrier-crossing moves must be atomic).
- **Split surfaces are not limited to planes**: boss-and-socket
  (carries shear across the seam), stepped/lapped, split-by-feature,
  split along the medial axis — anything expressible as an SDF plus a
  mating rule. Merged blocks remember their seams as internal load
  paths, so they can split back along the same lines when the scenario
  changes (unicycle fork: four welded pieces at one-off, one forging at
  volume, and back).
- **LLM proposes, annealer scores.** The LLM proposes a handful of
  candidate split lines from world knowledge; the annealer scores and
  picks. It will sometimes miss the non-obvious optimal cut — so do
  people — so seed a few random cuts into the candidate pool to detect
  a systematically-missed class of solution.
- **Demand-driven consultation.** The annealer asks the LLM only when
  search stalls on an active constraint; it packages the block, the
  binding constraint and the neighbours; candidates come back as
  surfaces plus mating rules, minted as seeded parallel starts. Blocks
  comfortably inside constraints are never discussed.
- **Nucleate a hole** — the one topology move the polish stage cannot
  make (§2).
- Refinement pass outputs **a report, not edits** — a ranked list of
  what each change would buy (spec §3.6 owns; the report-not-edits
  phrasing is the addition: a report cannot quietly make the design
  worse, and it shows the slack even when nothing is actioned).

## 6a. Symmetry (added 2026-09-15 — the §8 table row had no definition)

The scale-mapping table lists symmetry at every tier; nothing defined
it. Two forms, different owners:

**Imposed** (hard) — `pattern-groups.md` owns: one prototype × transform
group (linear/polar spec'd; mirror and point groups its named later
extensions). Field cost divides by $|G|$: solve on the fundamental
domain with symmetry boundary conditions. Pattern-groups' own caveat
stands — loads may break the symmetry (lateral load on a polar tripod),
so a symmetry-reduced solve is only valid when the load case shares the
group.

**Soft** (cost term) — owned here, new. For each generator $g$ of the
wanted group,

$$J_{\text{sym}} = \int_D \big(H_\epsilon(\phi) - H_\epsilon(\phi\circ
g)\big)^2\,dx$$

— mismatch between the shape and its own image under $g$.
Differentiable, contributes to $v_n$ like any other term, and lets
physics *break* the symmetry where it pays, at a reported cost. Same
fixed-$R_i$ normalisation as everything else.

| Group | Generators | Notes |
|---|---|---|
| mirror | one reflection plane | the common case; plane pose is a continuous variable or datum-fixed |
| rectangular | two or three orthogonal reflections ($D_2$, $D_{2h}$) | prismatic parts; aligns with the 3-2-1 datum |
| rotary $C_n$ | rotation $2\pi/n$ about an axis | rotational parts; axis = primary datum |
| 2D | a section or projection is symmetric, the solid need not be | $\phi\circ g$ evaluated on a plane only — shares machinery with the silhouette term |
| 3D point group | full group | atomic tier — `structure`'s `ops.apply_ops` is the enclave analogue; no third symmetry vocabulary |

*Which* group (and whether imposed or soft) is a small enumerated
discrete variable — an annealer choice, per the split. Preference:
impose when the field-cost saving is wanted and the load case permits;
soft when near-symmetry is aesthetic or when a load case is known to
break it.

## 6b. PCB as the second renter of the optimiser stack (Reto 2026-09-15)

The same two-layer split applies to `precis.pcb` unchanged, with strong
prior art (DREAMPlace / ePlace: nonlinear analytic placement in torch —
wirelength via log-sum-exp / weighted-average HPWL, overlap via an
electrostatic density field, gradient over all positions at once).

| pcb term (`pcb/cost.py`, `pcb/optimize.py`) | layer |
|---|---|
| `loop_inductance` (segment endpoints), `coupling` (pairwise proximity), `board_area` (bbox → smooth-max), `gap_capacity` (nearest-neighbour → soft-min), component overlap (density field) | continuous — descend |
| `crossings` (integer, standard continuous relaxation exists) | continuous with relaxation, or annealer |
| `layer_count`, `via_count`, layer assign, side flip, pin swap, 90° rotation, sketch topology | discrete — anneal |
| detailed routing beyond the rubber-band sketch | discrete, not descendable (same status as bond topology at the atomic tier) |

What it buys over SA alone:

- **A different cost profile.** pcb's SA needs its per-move locality
  contract because it scores one move at a time; descent scores every
  term once per step, O(board), and moves *every* instance. The two
  compose — descent inside each discrete state, SA between them — so
  the locality caches stay behind pcb's `energy` and stop being the
  design constraint on adding terms.
- **Preferred numbers fit pcb better than mechanical.** Component grid
  (2.54 / 1.27 / 0.5 mm), mounting-hole positions, outline dimensions —
  all round-value wells. Today's legalisation is end-snapping (§4's
  rejected alternative); the term makes it part of the gradient.
- **Board outline as an SDF** puts board shape and keep-outs in the
  same shape functional as the enclosure — §3's "PCB terms in the same
  sum as mechanical ones" becomes literal through
  `pcb-se-binding.md`'s mechanical-profile funnel (mm enclave, one
  crossing).

**Sequencing (Reto).** Define the optimiser protocol first (§Build
order: `State` / `Move(apply, undo)` / `MoveGenerator` / `energy` /
staged schedule with reheat-at-boundary — pcb's own hard-won fix,
kept); pcb adopts it *after* the interface exists, bridging
heuristically meanwhile. The heuristic bridge is `seed_placement`
(`pcb/optimize.py`): analytic descent produces the seed, the existing
SA polishes with its caches untouched. No pcb rewrite is on the
critical path.

## 7. Scale mapping — what survives downward

| Layer | Macro (parts) | Meso (cube assembly) | Atomic |
|---|---|---|---|
| Discrete variables | topology, fasteners, process | which face mates which, patch identity | composition, connectivity, bond order |
| Continuous variables | boundary shape via level set | linker length, orientation | atom coordinates |
| Inner solver | FEA / thermal | coarse-grained MM | MM → semi-empirical → DFT |
| Gradient source | shape derivative + adjoint | force field | analytic forces (Hellmann–Feynman) |
| Preferred numbers | yes | partially (linker lengths) | no |
| Symmetry term | yes (mirror, rotational, rectangular) | yes | yes (point groups) |
| Commonality term | yes (BOM lines) | yes (fragment reuse) | yes (fragment reuse) |
| Level-set advection | yes | no | no |

### The atomic scale specifically

*Transfers cleanly:* geometry optimisation within fixed bonding topology
is exactly the continuous inner layer — positions differentiable, forces
are the gradient, descend. MM/semi-empirical/DFT/ML potentials all
supply analytic gradients; the best-behaved inner solve in the stack.
The fidelity ladder becomes *more* important: inner-solve cost spans
~six orders of magnitude (ms MM to hours DFT) vs ~two at macro, so
per-candidate solver-fidelity choice stops being an optimisation and
decides whether the search is possible at all. Screening, termination
nodes and provenance carry over unchanged; symmetry strengthens
(point-group reduction of the electronic solve ≡ imposed geometric
symmetry reducing a field solve); commonality survives as fragment
reuse, part count as fragment count.

*Breaks:* no continuous boundary to advect (coordinates are internal:
bonds, angles, torsions — level-set advection is gone). Preferred
numbers are meaningless — bond lengths are what physics says; the term
switches off below the fragment tier. The discrete space is enormous and
genuinely gradient-free (no derivative w.r.t. "carbon or nitrogen") —
the two-layer split gets *sharper*: a much larger discrete space around
a much more expensive continuous solve. Topology change is no longer
free: bonds are made/broken by explicit discrete moves.

### Alchemical relaxation — the escape hatch worth investigating

Atom identity as a continuous parameter $\lambda$ blending element types
or force-field parameter sets — standard in free-energy perturbation,
used in inverse-molecular-design work for composition gradients.
Caveats: intermediate states are unphysical, the path matters, and it
works far better perturbing a fixed scaffold than exploring
connectivity. It does not remove the discrete layer — but it may convert
local composition search (*which* substituent at *this* position) from
combinatorial into descendable, which is exactly where an annealer
wastes most of its budget. Worth a serious look before committing to
pure discrete search at the fragment tier. Routes to spec §4.9 /
the `src/precis_se/__init__.py` docstring's library-search paragraphs
when investigated.

## 8. Open items

1. **Technical note / disclosure** — §2 + §4 with the corrected min-of-
   wells formulation, diagrams (well shapes, tier interaction,
   min/max/sum comparison), worked example. Highest priority.
2. Auto-adjoint tooling decision — FEniCS + dolfin-adjoint, Firedrake,
   or JAX-FEM (lighter, autodiff-native, probably the right start).
3. Calibrate $\lambda$, $A_0$, $B_0$ against real designs; fix the
   pull-ratio reporting threshold (§4, magnitude discipline); verify
   the $B$-offset vs transport-cost bound.
4. Decide the random-cut seeding rate for split-candidate hedging.
5. Investigate alchemical relaxation at the fragment tier (§7).
6. Single-currency cost model — exchange rates per scenario (§3).
7. Multiphase level set for assembly seams — spec'd as an upgrade path
   (§2); needs a first design that wants an optimisable split surface.
8. Topological-derivative nucleation — decide whether it replaces the
   SIMP pre-stage or complements it.
9. Symmetry: softmin sharpness $\beta$ and $J_{\text{sym}}$'s $R_i$
   calibrate alongside item 3; mirror/point-group generators land in
   `pattern-groups.md` for the imposed form.
10. Datum-as-feature + $m(\text{design})$ evaluator in se — spec as its
    own backlog item; prerequisite for the wells (§4).
11. pcb bridge: descent-seeded `seed_placement` — after the optimiser
    protocol lands.
