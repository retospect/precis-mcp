---
status: ready
pillar: 3d-design
prio: medium
model: opus
title: se flow kernel — dimension-free thin-chamber flow + particle tracing on continuous se/cad objects, with a regime card that says when the model holds
---

# se flow kernel (dimension-free Hele-Shaw + Lagrangian tracer)

Reto, 2026-10-20: "we do mm to 50 cm or so scale for now. If we can
meaningfully do this dimension-free, I love it." Source code: the
`cfdtest` scratch repo at `../cfdtest`
(`src/breakthrough_annealer/`, own git, not in this repo): `flow.py`
(Hele-Shaw finite-volume Poisson solve → depth-averaged U, resistance),
`tracker.py` (vectorised Lagrangian tracer with a stochastic vertical
dispersion active over holes, breakthrough plane), `cost.py`,
`anneal.py`, `config.py` (defaults: `w_break 10, w_holes 0.5, w_res 5,
w_even 5`, `cooling 0.97`, `iters_per_temp 3`), `geometry.py` (hex
chamber mask, ports, hole rasterisation), `render.py`; 1.4 k lines,
numpy + scipy, tests on the invariants. Its pyproject pins
python ≥ 3.14; precis is ≥ 3.12 — the vendored code drops the pin.

## The dimension-free formulation (worked out)

Thin chamber of lateral extent `L`, gap `h(x, y)` with reference `h₀`,
viscosity `μ`, pressure drop `Δp` between ports. Lubrication (Hele-Shaw):

```
U = −(h²/12μ) ∇p,        ∇·(h³ ∇p) = 0
```

Scale `x* = x/L`, `h* = h/h₀`, `p* = p/Δp`, `U* = U/U₀` with
`U₀ = h₀² Δp / (12 μ L)`, `t* = t/t₀` with `t₀ = L/U₀` (one transit):

```
∇*·(h*³ ∇*p*) = 0,     U* = −h*² ∇*p*,     dx*/dt* = U*
```

Nothing dimensional survives in the solve. The field, the streamlines,
the cell resistance ratio `R/R_ref`, the breakthrough fraction, its
evenness and the hole count depend only on **shape**: the mask, the
aspect ratios `h₀/L`, hole depth `/h₀`, hole diameter `/h₀`, port width
`/L`, the seed band `/L`, the tracking-plane offset `/h₀`. One solve per
shape serves a 1 mm chamber and a 50 cm chamber alike.

One modelling parameter carries physics: the vertical dispersion over a
hole, `D_v* = D_v · t₀ / h₀²` (transit time over vertical mixing time;
cfdtest's `mix_coeff` made dimensionless). It is a pure number in the
kernel, but its *value* belongs to the regime, so it is an input the
regime card sets or the user states, never a hidden default.

**Regime card — where dimensions re-enter.** From the real
`(L, h₀, μ, ρ, Δp or U₀, particle d and ρ_p, molecular D)` the kernel
computes and stores, with a verdict per line:

| Number | Formula | Model holds when | Else |
|---|---|---|---|
| thinness | `h₀/L` | ≤ 0.05 | 3-D Stokes needed (not here) |
| reduced Reynolds | `Re_h · h₀/L`, `Re_h = ρU₀h₀/μ` | ≤ 0.1 | inertia: 3-D Navier–Stokes (not here) |
| Stokes number | `St = τ_p U₀/h₀`, `τ_p = ρ_p d²/(18μ)` | ≤ 0.1 | particles are not tracers |
| gravity ratio | `G = v_s L/(U₀ h₀)`, `v_s = (ρ_p−ρ) g d²/(18μ)` | ≤ 0.1 | settling dominates; add `−v_s*` to the vertical model |
| Péclet | `Pe = U₀h₀/D` | ≥ 10 | diffusion dominates the tracer |
| Knudsen | `Kn = λ/h₀` | ≤ 0.01 | free-molecular: the nano propagator (slice 3), not this kernel |

Worked points: water, `h₀ = 1 mm`, `L = 100 mm`, `U₀ = 1 cm/s` →
`h₀/L = 0.01`, `Re_h · h₀/L = 0.1` (marginal, holds). Water, `h₀ = 5 mm`,
`L = 500 mm`, `U₀ = 5 cm/s` → `Re_h · h₀/L = 2.5`: refused, 3-D needed.
A 1 nm hexfold lumen with gas: `Kn ≫ 1`, slice 3. So "mm to 50 cm" holds
as long as the chamber is thin and slow; the card says which, per case.

**What is stored.** Results in reduced units (`x*`, `t*`, `R*`,
fractions) plus the scale triple `(L, h₀, U₀)` and the regime card, so a
stored solve re-dimensionalises for any size with the same shape and the
card tells whether that size is still inside the model.

## What a continuous se object supplies

- The fluid domain: a cad envelope (or a `cad` kind solid) → thin axis =
  the smallest bounding-box extent; mask = the projection along it;
  `h*(x*, y*)` = local extent along the thin axis over `h₀`; holes and
  pockets are floor topography (depth = max over overlaps, as cfdtest).
- Ports: cad faces selected with the existing `patch:` selector
  (`precis_se/datums.py`), tagged inlet / outlet; the seed band is the
  inlet face's footprint.
- Tracking planes: any `z*` offset, or a station z-window
  (`pocket-from-molecule` stations) — the breakthrough plane generalises
  to "fraction of particles that reach region X by `t*`".
- Outputs as **measures on regions** through the `COMPUTERS` path that
  pocket-from-molecule slice 2a wires (`breakthrough_fraction`,
  `residence_time_t0`, `resistance_ratio`, `evenness`), so a pocket spec
  or a chamber spec can *require* a flow property and DRC checks it.

## Slices

*Slice 1 (kernel package `src/precis_flow/`) landed 2026-10-20 — the
commit carrying this line; `git log` has the sha.*

2. **se adapter** *(landed 2026-10-21 — the commit carrying this
   line)*: cad envelope → `Domain` (thin-axis detection, depth map,
   `patch:` ports), `view='flow'` (field, streamlines, card), measures
   through `COMPUTERS`. Test: the cfdtest hex chamber rebuilt as a cad
   envelope reproduces cfdtest's breakthrough fraction within its own
   seed scatter. `view='flow'` also serves the player payload of slice
   2b: chamber mesh (outline + floor depth map), the recorded frames
   (`trace(record_frames=K)`, float32 `(K, n, 3)` reduced positions,
   `z*` 0 = floor, 1 = ceiling) as one binary blob, the scale triple
   and the regime card. Over ~20 MB the server subsamples particles,
   then frames, and the payload says by how much. Landed as
   `src/precis_se/flow.py`: `domain_from_solid` accepts a `Placed`
   envelope or a `(Design, Expr)` cad solid; the chamber spec is a
   pocket shaped `flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=…]
   [;zplane=…]`; the adapter's profile default is `parabolic` (Q4).

2b. **Web particle player** *(landed 2026-10-21 — the commit carrying
   this line)*: the Flow tab `/se/<slug>/flow` (`routes/se_flow.py`,
   `static/se-flow-player.js` on three-r184, the payload maths in
   `static/se-flow-frames.js`), linked from the se 3-D view when the
   design carries a `flow:` pocket. The payload gained port masks, the
   tracking plane, per-particle outcomes and `size=` re-dimensioning;
   chamber build and trace are memoised per design version. A `field:`
   envelope works as a chamber (its ports sit on other blocks).
3. **Nano propagator** *(landed 2026-10-21 — the commit carrying this
   line)*: test-particle Monte Carlo with diffuse wall reflection on a
   hexfold lumen (`generated.sites` + vdW-offset wall), same `trace`
   outputs; the card routes `Kn ≫ 1` here. Landed as
   `src/precis_flow/nano.py` (`CylinderLumen`/`AtomWall`/`propagate`),
   `RegimeCard.route`, the adapter's lumen path (`flow:lumen;alpha=`,
   `view='flow'` on structure-bound blocks), and
   `tests/test_precis_flow_nano.py` + `tests/test_se_flow_nano.py`.
   Reduced time `t* = t v̄/L` with `v̄ = sqrt(8kT/πm)`. Transmission then
   depends on shape and the accommodation coefficient `α` only: the same
   dimension-free claim the continuum kernel makes.
   Acceptance (`tests/test_precis_flow_nano.py`, kernel;
   `tests/test_se_flow_nano.py`, adapter):
   - **Clausing.** For a straight cylinder with diffuse walls (`α = 1`),
     `n = 10⁵`, transmission at `L/R = 1, 2, 10` is `0.6720, 0.5136,
     0.1910` within ±0.005 absolute (3σ ≈ 0.0045). Transmission falls
     monotonically with `L/R` and approaches `8R/3L` from below for
     `L/R ≥ 50`.
   - **Specular limit.** `α = 0` on a straight tube gives transmission
     exactly 1.
   - **Cosine law.** Re-emitted directions from a diffuse wall pass χ²
     against `cos θ` (p > 0.01, 10⁵ samples).
   - **Equilibrium.** A closed lumen with no ports, run long, gives a
     uniform number density per accessible volume bin (χ², p > 0.01),
     with no build-up at the walls.
   - **Size invariance.** The same shape at two absolute scales with the
     same seed gives identical transmission and identical `t*`
     residences.
   - **Hexfold lumen.** A tube from the hexfold generator, with the wall
     made of `generated.sites` spheres at vdW radius. Transmission lies
     within 10% of the cylinder Clausing value at `R_eff = R_axis −
     r_vdW` and the same `L/R_eff`. The measured deviation is recorded in
     the test docstring as the corrugation effect.
   - **Same outputs.** The propagator returns a `Paths`
     (`breakthrough` = transmission, `residence`, `frames`), so the four
     `FLOW_COMPUTERS` measures and the slice-2b payload work unchanged.
     Test this on a hexfold lumen block through `view='flow'`.
   - **Routing.** `RegimeCard.from_physical` with `Kn ≥ 10` names
     `regime.knudsen` and routes to the propagator. `view='flow'` runs it
     without being asked. With `0.01 < Kn < 10` (transition regime) both
     solvers refuse, and the reason names DSMC as out of scope. Slip
     (`se-multiphysics.md` slice 5) later moves the lower bound to 0.1.
   - **Determinism.** Same `seed_rng` gives bitwise-equal `Paths`.
4a. **Workspace flow: DB-free adapter, `evaluate(['flow'])`, tracer to
   the player** — *library half landed 2026-10-23 (the commit carrying
   this line): `precis_geom.flow`, `precis_geom.physics.evaluate`,
   `FLOW_COMPUTERS` on `evaluate`, the skill's `evaluate` section.
   Still owed: the `out.physics` manifest → `flow:` pocket + import
   re-check (needs structure-workspaces slice 0's importer), the
   end-to-end run-to-player test (needs slice 1's `run`), and
   `src/precis_flow` in the precis-geom wheel's pyproject (needs slice
   2's packaging — `evaluate` imports it, so the wheel must ship it).*
   No server op (the earlier `anneal_chamber` is dropped).
   Depends on [structure-workspaces](structure-workspaces.md) slices
   1–2 (the `run` loop; the `precis-geom` wheel). Order (Reto,
   2026-10-22): "particle tracer first and run it all the way to
   visualizer, then we do a chamber optimizer example (as dogfood).
   Then we add more physics." Delivers:
   - **`precis_geom.flow`** — the DB-free cad → `Domain` adapter
     refactored out of `src/precis_se/flow.py` (`domain_from_solid`,
     `_wrap_solid`, `_claim_port`, the port rectangle given directly).
     Kit-facing names are general: `Chamber` → `GapDomain` (a built
     thin gap: `Domain` + scale triple + axis map + outline + depth
     map; nothing in it is chamber-specific), `_chamber_placed` →
     `_solid_placed`. `PortPatch` keeps its name — a port rectangle on a
     face is already the general object; `FlowSpec`/`LumenSpec` keep
     theirs. `precis_se/flow.py` keeps `patch_port` (needs the tree),
     the `flow:` token, `FLOW_COMPUTERS` and `view='flow'`, imports the
     adapter from the wheel module and carries `Chamber = GapDomain`
     for one release. The slice-2 tests pass unchanged.
   - **`precis_geom.physics.evaluate(shape, physics=['flow'], spec, *,
     seed, cells)` → `Outcome`** — the one interface across physics
     ([se-native-fields](se-native-fields.md) §The toolkit): flow is its
     first physics; each native-fields slice adds its names. `measures`
     = the four flow measurands (`breakthrough-fraction`,
     `residence-time-t0`, `resistance-ratio`, `evenness`) computed by the
     same code `FLOW_COMPUTERS` run (the computers call `evaluate`; one
     implementation), `cards['flow']` = the regime card,
     `hard_ok`/`failures` from the spec's hard measures, `cost`, and
     `paths` (the traced `Paths`, lazy, never in the manifest).
     **Physics only** (ruling): no hole count, no material or process
     term. `seed=` reseeds the tracer identically on every call (common
     random numbers); `cells` is the kernel's `nx`.
   - **Manifest → player.** `out.physics(block, physics=['flow'], spec)`
     writes the manifest entry; the importer turns it into the `flow:`
     pocket and the port measures (`origin='user'`) and re-runs
     `COMPUTERS` (import re-check below). The slice-2b Flow tab
     (`/se/<slug>/flow`, `view='flow'`) then plays the workspace-built
     shape with no player change — the pocket is the object slice 2
     already stores.
   - **Skill `precis-workspace-optimize-help`** (new; the general
     "optimise a design in a workspace" skill, CFD as its first physics
     section rather than its subject). This slice writes the `evaluate`
     contract (returns and refusals per physics), the regime card, the
     spec shape (hard/soft/gauge) and the import re-check rule; 4b adds
     the optimisation sections; se-native-fields slice 1 adds its
     physics section.
   Acceptance (`tests/test_precis_geom_flow.py` for the adapter;
   `tests/workspaces/test_flow_player.py` end to end;
   `tests/test_se_flow.py` for the import re-check):
   - **End to end.** A workspace script builds the hex chamber with `pg`
     (the `test_se_flow.py` `_hex_design` fixture as code), calls
     `evaluate(physics=['flow'])`, writes `out.block` + `out.physics`;
     after `run` the design carries the `flow:` pocket, `view='flow'`
     serves the player payload (mesh, frames, ports, card — the keys the
     slice-2b test asserts) and `/se/<slug>/flow` renders it.
   - **Common random numbers.** `evaluate` on the same shape twice gives
     a bitwise-equal `Outcome`.
   - **Physics only.** `Outcome.measures` keys are a subset of the
     registered flow measurands; a test asserts no count/material key
     and that `evaluate` is pure over `(shape, spec, seed, cells)`.
   - **Import re-check.** After `run`, the server's `COMPUTERS` values
     equal the manifest's stated values within 1e-9 at equal `cells`;
     a tampered manifest value yields `measure.script_mismatch`; a hard
     bound the server's own number violates is a DRC finding whatever
     the manifest claims.
   - **Budget.** `evaluate` above `INLINE_CELLS` refuses with `next=`
     naming `budget='job'`.
   - **Offline.** The end-to-end test's script step runs with no DB and
     no se import (the wheel alone; structure-workspaces §Core design 7).
4b. **Design-optimisation kit + two examples (dogfood).** Reto,
   2026-10-22: "the chamber with the holes is but a point solution. We
   want the general case implemented, the coder can do cool stuff with
   it then." And: "hole count will not be the answer to evaluate shape.
   That's what the tool makes the space to be." After 4a. Delivers:
   - **Design representation — proposed, under discussion** (Reto is
     still on this item; the rest of 4b is ruled). Two cases set the
     bar: the chamber (holes poked, shifted, added, removed —
     annealing) and a bracket (two planes at an angle, a load, a
     material, a manufacturing spec: add small supports where the
     remaining need is highest, anneal, then remove the least-stressed
     ones, ESO/BESO-like). "It's the coder's choice." Recommended:
     0. **Recipe vs product.** The design *recipe* (feature graph +
        params) is the editable, snapshot-able thing the optimiser
        changes. Built geometry (atoms, cad instances, faces) is
        **immutable**: every build is a pure function of the recipe,
        so each object has exactly one creator frame and no per-move
        traces exist (Reto: "if we want the traceability … the objects
        kind of better be immutable (otherwise we have stack traces for
        each movement)"). A move is a recipe edit plus a rebuild; any
        transform helper returns a new object with its own creator
        frame, never a modifier frame on the old one. The search
        history (which iteration, which proposal changed which feature)
        lives in the trace's `search` section, linked from the root,
        not stamped on objects.
     1. **`precis_geom.design`** — the mutable recipe: a feature graph
        whose features have stable ids (`add`/`remove`/`modify` by id,
        never "the third hole"). The primary interface, because
        topology changes (growing and pruning features) are awkward as
        fixed-length parameter vectors. `build(design) -> Design` (cad
        graph) is pure; `precis.cad.graph.Design` is a build product —
        its `instances`/`components` dicts grow only during the build
        (the nodes are frozen) and the kit never edits a built one in
        place. A stable feature id
        is not a stable face: booleans split and merge faces, so BCs and
        ports reference semantic regions and datums — the existing
        `patch:` selector (`precis_se/datums.py`, moving to
        `precis.cad.patch`) is exactly that, re-resolved on every
        rebuild; the rebuild reports `one→many`, `many→one` and
        `deleted` mappings for the regions a spec names, and a deleted
        region is a refusal naming the feature, not a silent drop.
        Interchangeable features canonicalise (holes sorted spatially)
        so permutations hash to one `shape sha` for the cache.
     2. **Snapshot/restore**: `design.snapshot()` / `restore(snap)` —
        copy-on-write or a plain copy, designs are small — for
        backtracking and Metropolis rejects. Each evaluation is one
        atomic transaction over design + grid + semantic maps + solver
        restart data; workers hold copy-on-write snapshots, never a
        shared undo stack. Across runs the per-run commit
        (`script_sha`), `target:'fork'` and the retire/restore trail
        cover it; no design snapshots in the DB.
     3. **`precis_geom.params`** — named, bounded parameters
        (`Continuous(lo, hi)`, `Integer(lo, hi)`, `Boolean()`) as a
        convenience layer over feature attributes, with constraints as
        functions of the parameters, for continuous optimisers; a
        `Space` samples, clips and validates; params are an immutable
        mapping (`build(params)` stays the convention for fixed-topology
        searches).
     4. **Ask/tell adapters** (Optuna/Nevergrad style): `ask(n) ->
        candidates`, `tell(observations)`, `recommend()`; an
        `Observation` carries `objectives`, `constraints` (`≤ 0`
        feasible), `status ok|failed|pruned`, `design_hash` and
        `artifact_uri`, so any search style plugs in and a failed solve
        is never read as a good objective.
     5. **`evaluate` returns more than scalars** — still physics
        outcomes, so the physics-only ruling holds: the `fields`
        (stress, flow speed, temperature; lazy handles), queries over
        them (`argmax`, `sample(point)`, `top_k(regions)`), and
        **per-feature attribution** (`attribution[feature_id][metric]`:
        flow through each hole, strain energy or stress utilisation per
        support — what "remove the least-stressed support" reads), each
        value tagged with its attribution type (`direct` local metric,
        `additive` integral, `counterfactual`/adjoint influence) because
        only additive ones sum. Later: adjoint sensitivities. Warm start
        / incremental re-solve after a small edit (`evaluate(...,
        warm=prev)` passes restart data; the kernel decides what is
        still valid) is a performance requirement, measured in the
        bracket example.
     6. **Scale by memoised hierarchy, not one monolithic script.** A
        module is its own workspace/design, frozen at `(fn, wheel
        version, params_sha, script_sha)`; an assembly fetches it from
        the content-addressed cache and links its trace (the `unit`
        frame, "linked not copied" — structure-workspaces §Frozen
        units/modules, both scales). A change rebuilds only the changed
        module; co-optimisation covers only what the assembly script
        owns (placement, interface params) unless the author re-opens a
        module. A trivial recipe rebuilds in µs–ms per feature; real
        builds cost minutes (the step-stack bullet below), so both
        scale: rebuild through the step cache, evaluation through
        the cache, `evaluate_many`, warm start, coarse-to-fine (`cells`
        low for the search, the final candidates re-evaluated fine, the
        trace recording both); module-level reduced models (a frozen
        module's response surface standing in for its solve) are a
        named later item.
     Prior art: the cached survey `perplexity-research:506346`
     (ESO/BESO, SIMP, level-set, SA over features; OpenMDAO, Optuna,
     Nevergrad, pymoo interfaces; representation and solver-output
     needs). Folded in: SA over features uses our own move/energy loop
     (SciPy `dual_annealing` is a fixed-length vector optimiser, the
     wrong abstraction for add/delete moves); ESO/BESO needs per-element
     strain energy with neighbourhood filtering, level-set needs a
     topological derivative to nucleate holes — both read the
     attribution and fields above; SIMP is a separate density-field
     route, named as a later plug-in, not slice scope; field-biased
     proposals are heuristics only — the objective decides.
     **Scope.** This kit is se's. pcb
     ([pcb-workspace-code](pcb-workspace-code.md)) shares the workspace
     run loop, the construction trace, the snapshot and the ask/tell
     layer, but its builders and evaluators differ: the netlist and
     modules are built in code, while place/route/DRC stay server
     services, like relax.
   - **Build-step stack cache** (ruled, Reto 2026-10-23; general kit
     infra at both scales, not a chamber special case). `build(recipe)`
     is a stack — generally a DAG — of primitive and 3-D manipulation
     steps (box, extrude, drill, boolean, fillet, wrap, relax…). Each
     step's output is memoised under `step key = hash(step name, its
     params, parent step key(s))`: changing step `k` rebuilds `k` and
     its descendants; everything below is a cache hit. The high-water
     mark falls out of the keys with no extra bookkeeping; branches
     (two independent hole sets) keep separate entries; undo is a hit
     on an earlier key, so an annealer's reject and backtrack cost no
     build — consistent with item 0 (recipe mutable, geometry
     immutable, a move = recipe edit + rebuild). It is the in-build
     counterpart of the frozen units/modules (structure-workspaces
     §Frozen units/modules: the same key discipline, one step instead
     of one module). The chamber is the smallest demo; the builds
     that cost minutes are macro — booleans on bodies with many
     features, fillets, the cylinder wrap + mold negative
     (`cad/primitives.py::CylRelief`, `cad/mold.py`), lattices,
     assemblies of frozen modules — and, at Å, relax over pre-relaxed
     modules (below). Named heavy example: the filleted
     bracket (se-native-fields §Bracket example) — a rib edit rebuilds
     the rib boolean and the fillets above it, never the base, pads or
     clearance holes.
     - **Pre-relaxed modules are the stored product** (Reto
       2026-10-23: "we can benefit from module pre-relaxing"). (a) Each
       module is relaxed once on its own, cut bonds capped, and stored
       under its key — a library of pre-relaxed chunks, the se
       architecture of pre-relaxed bits (structure-workspaces §Relaxed
       units, §Frozen units/modules). (b) A `join` starts from the
       pre-relaxed modules, so far atoms are already near their
       minimum; only the seam buffer has to move — that is what makes
       the seam-local relax cheap and the frozen-atom force check
       likely to pass. (c) A full relax of the assembly runs only when
       the force check fails, and even then warm-starts from the joined
       pre-relaxed geometry. (d) Per join the frame records the buffer
       radius used, the max residual force on frozen atoms and the
       steps taken; [se-seam-spacers](se-seam-spacers.md) calibrates
       the defaults. Why the check exists: relax and DFT are non-local
       (Reto: "if we relax again with some more atoms the structure
       will be slightly or massively different. DFT is similar"), so a
       module's stored relax is exact for the module alone and is the
       seed above a join, not the answer — geometry steps (booleans,
       tiling, placement) are local in the key sense; `relax` and
       `evaluate` are not, and the keys do not pretend otherwise.
     - **Exact key, warm start.** Keys stay exact — no approximate hits.
       A miss on a `relax` step does not start from scratch: the nearest
       earlier key on the same lineage (same parents, earlier params)
       seeds the new relax with its geometry (and density/wavefunction
       for a DFT rung); the service frame records `warm_from=<key>`. A
       warm-started relax converges to the same minimum as a cold one
       when it is in the same basin; the trace says which it was.
     - **Assembly = DAG of sub-stacks** ("it's an assembly"). Modules are
       built independently as small stacks with their own keys; a
       `join` step takes module keys as parents; further steps pile on
       top. Changing module A reruns A, the join and everything above
       it, not module B. Reruns above a join are expected; what bounds
       their cost is that the post-join relax is seam-local.
     - **Seam-local relax.** A `join` declares its seams — insulating
       spacers, e.g. saturated C2H4/(CH2)n linkers for electronic
       decoupling at Å. The post-join relax starts from the pre-relaxed
       modules, frees a buffer region around the seams and freezes far
       atoms (QM/MM / ONIOM-style; H-capped link atoms for DFT
       fragments). Locality is checked, not assumed: after the local
       relax a single point measures residual forces on the frozen
       atoms (and the density/charge change at the buffer boundary for
       DFT) against a threshold; above it the buffer grows and the
       relax repeats, up to the full-assembly relax of (c); the frame
       records (d) (structure-workspaces §Relax hierarchy (c) owns the
       numbers). Electronic insulation is not mechanical insulation:
       strain from lattice or geometry mismatch propagates past a
       spacer that decouples the electrons, so the force check is
       mandatory even with a good spacer. Which spacers insulate, and
       the default buffer radius, come from
       [se-seam-spacers](se-seam-spacers.md) (literature + demo).
     - **Params in the frame.** A step-built object's creator frame
       carries the step name, its params, the recipe key that set them
       (`holes[3].x`) and the step key; interrogating a contact point
       answers `made by drill(x=12.1, d=3) · step 7 · set by holes[3]`
       (structure-workspaces §Construction traces).
     - **Order stable → volatile.** The kit records per-step build
       seconds and rebuild count and reports the steps that are both
       expensive and often invalidated, so a bad order is visible;
       `out.search` prints the report with the search summary.
     - **Container-only.** An LRU over step snapshots, in-process, with
       a byte cap; never Postgres — only the final build's trace and
       the search summary are stored (structure-workspaces
       §Construction traces, Storage).
     - **Later, not v1:** incremental evaluation under the same keys
       (slice 4c).
   - **`precis_geom.optimize`** — search tooling over `evaluate`:
     - `cache`: `Outcome`s keyed `(shape sha, physics, spec sha, seed,
       cells)`; a revisited state costs no solve; persisted under
       `/work/out/evaluate-cache/` so a resumed run reuses it.
     - `evaluate_many(shapes, ...)`: parallel over the cgroup's cores,
       results in input order (deterministic).
     - common random numbers: one `seed` per search, passed to every
       `evaluate`.
     - `out.search(iteration, cost, measures, params, accepted)` records
       **every** evaluation into the trace's `search` section;
       `view='trace'` renders the best iteration with its measures.
     - checkpoint/resume: `Search.checkpoint(path)` every N evaluations
       and when the run's `wall_seconds` estimate would be exceeded
       (`/work/out/search.json`); `params.resume` reloads it.
     - `budget='job'`: an `evaluate` above `INLINE_CELLS` refuses naming
       it; the request pattern of se-native-fields §Budgets.
     - optimisers as adapters behind the ask/tell interface: `anneal(
       moves, energy, schedule, rng)` — cfdtest's generic Metropolis
       helper (geometric cooling, `T/T0`-scaled proposals supplied by
       the caller; nothing about holes or chambers inside it) vendored
       here, not into `precis_flow`, over feature-graph moves with
       snapshot/restore on reject; `scipy_minimize(space, ...)`
       (`scipy.optimize.minimize`, derivative-free methods on the
       continuous subspace, integers rounded through the space);
       `cmaes(space, ...)` over the `cmaes` PyPI wheel (an example dep,
       not a core dep). Gradient methods later (adjoint sensitivities,
       se-native-fields "later, named"; `precis-differentiation-help`).
       A script may import any PyPI optimiser (Optuna, Nevergrad,
       pymoo plug into ask/tell directly) — the adapters are
       conveniences; the contract is `design`/`Space` + `evaluate`.
   - **Examples** (`examples/workspaces/`, CI goldens per
     structure-workspaces slice 7, run without a container):
     - `chamber-anneal/main.py`: the hex chamber, written as the
       workspace flow every optimising script follows (Reto,
       2026-10-22; structure-workspaces §The loop):
       - `build(recipe)` — pure, deterministic; `search()` — the
         annealer: edit recipe → build → evaluate → accept/reject,
         returns the best recipe. Two functions, one file.
       - `recipe.json` is the **full** recipe: every input `build`
         needs (chamber dimensions, ports, the hole set as `(x, y, d,
         depth)` rows with `user` holes pinned, material/process refs,
         units) — not just the hole list; `build(recipe.json)` needs
         nothing else. It is stored with the run beside the code at
         `script_sha`, the built blocks and the search summary (seed,
         iterations, cost curve, best score, starting recipe hash).
       - entry points: `uv run main.py` builds from `recipe.json`;
         `uv run main.py search` **warm-starts from the existing
         `recipe.json` by default** (Reto), `--fresh` starts from the
         base design. A warm start begins at a lower temperature (a
         reheat schedule, `T0_warm = T0 · reheat`, default 0.1) so it
         does not scramble the converged state; it writes the new
         `recipe.json`, then builds. The summary records the parent
         recipe hash, so recipes form a lineage chain.
       - rebuild, import re-check, `regenerate` and the frozen-unit
         cache all call `build(recipe)`, never `search`. Re-running
         `search` with the same seed reproduces within one toolkit
         version only (kernel and optimiser versions are in the
         summary).
       - the search's constraints (spacing/depth inside the process
         `min_wall`, read through the read MCP and staged into the
         recipe) reject before `build`; `hard_ok == False` rejects
         before the cost is read; the objective is the script's:
         `cost = −w_break·f + w_res·R/R_ref + w_even·(1 − evenness) + w_holes·n/n_ref`
         (the `w_holes` term is the script's, not the toolkit's); the
         best chamber is emitted with its holes as cad `body add`
         cylinders, the four measures `soft` with the weights in meta,
         plus the hard `resistance-ratio ≤ 1.2`.
       - traces during `search` stay in-container (in-memory pointers,
         never Postgres); Postgres stores only the final build's trace
         (the stored objects' creator frames, a few KB) plus the search
         summary, so provenance survives teardown
         (structure-workspaces §Construction traces, Storage).
     - `manifold-split/main.py`: a thin manifold splitting one inlet
       evenly to `N` outlets; params = `N` channel widths (continuous,
       bounded below by `min_wall`) and the split position; objective =
       outlet evenness (the `evenness` measurand evaluated over the `N`
       outlet patches — `precis_flow.stats.nonuniformity` takes a list
       of outlet masks; the one kernel change, se-machine-design seam)
       plus `resistance-ratio`; run with `scipy_minimize` and with
       `anneal`, both recorded in the trace.
     - a heat example (fin thickness search) when se-native-fields
       slice 1 lands — filed there.
   - **Skill `precis-workspace-optimize-help`** gains the optimisation
     sections: params/space, the loop (objective from measures, hard
     constraints reject, common random numbers, the cache,
     `evaluate_many`, budgets inline vs `budget='job'` on pollux/spark,
     checkpoint/resume, recording the search), the adapters, both
     examples, the `precis-differentiation-help` link.
   Acceptance (`tests/test_precis_geom_optimize.py` for params, cache
   and adapters; `tests/workspaces/test_chamber_anneal.py` and
   `tests/workspaces/test_manifold_split.py` for the examples, without a
   container):
   - **Cost parity.** On the hex chamber with cfdtest's default weights,
     `profile='plug'` and the same seed, the example's cost of the
     initial hole set (flow terms from `evaluate` + the script's hole
     term) equals cfdtest `cost.evaluate`'s `total` within 1e-9.
   - **Reproduction.** At cfdtest's default config (`cooling 0.97`,
     `iters_per_temp 3`, cfdtest's iteration count), the example's best
     cost lies within the spread of cfdtest `anneal` over 5 seeds, and
     is strictly below the initial cost.
   - **Determinism.** The same seed gives the same best hole set.
   - **Params.** `Space.validate` refuses a constraint violation naming
     the parameter; `build` on equal params gives equal shape shas and
     never mutates its arguments (the test passes a frozen mapping and
     a `Design` whose instance count is asserted unchanged).
   - **Feature graph** (if the proposed representation stands):
     `remove(id)` then `restore(snap)` gives the same shape sha as
     before; two hole orders give one sha (canonicalisation); a `patch:`
     region split by a new feature reports `one→many`, a deleted one
     refuses naming the feature; a failed solve is an `Observation`
     with `status='failed'`, never a cost.
   - **Attribution.** On the chamber, the per-hole flow fractions sum
     to the breakthrough fraction within 1e-9 (`additive`); the
     `counterfactual` value for one hole equals evaluate-without-it
     minus evaluate-with-it on a 3-hole case.
   - **Cache.** Re-evaluating a visited state does not call the kernel
     (a call counter); the key changes with each of its five parts.
   - **`evaluate_many`.** Results equal the sequential results bitwise
     and arrive in input order, with 1 and 4 workers.
   - **Adapters.** `scipy_minimize` and `cmaes` reach the optimum of a
     quadratic test objective over a mixed `Space` within tolerance;
     `anneal` on the same converges in the mean over 5 seeds.
   - **Manifold.** The best manifold's outlet evenness is ≥ 0.95 (plug
     profile, default cells), up from the equal-width start; the hard
     `resistance-ratio` bound holds on every accepted state.
   - **Hard constraints hold.** With the hard `resistance-ratio ≤ 1.2`,
     no accepted state violates it; after import `view='drc'` is clean
     for that measure.
   - **Script-side invariants** (the chamber example's own test): a
     pinned `user` hole is never moved or removed; hole depth ≤ plate
     thickness − `min_wall`; hole spacing ≥ `min_wall`, both asserted on
     every accepted state; holes import with `origin='proposed'`.
   - **Checkpoint/resume.** A search interrupted after `k` evaluations
     and resumed reaches the same best state as an uninterrupted run
     with the same seed.
   - **Trace.** The run's trace carries the `search` section (scalars
     per evaluation: iteration, cost, measures, `accepted`; never a
     per-evaluation construction trace) and the final build's creator
     frames only; `view='trace'` renders the best iteration with its
     measures.
   - **Step cache.** Changing a top step (one hole's `x`) rebuilds that
     step only — per-step rebuild counters show one increment; a base
     step edit rebuilds it and its descendants, none below; two
     independent hole sets hold separate entries and an edit to one
     leaves the other's counter at 0; undo to the previous recipe is a
     cache hit (no counter moves, the build is bitwise-equal to the
     earlier one).
   - **Params in the frame.** The creator frame of a drilled hole
     carries the step name, params, `recipe_key` (`holes[3].x`) and
     `step_key`; `view='trace'` on the hole's contact point renders
     `drill(x=12.1, d=3) · step 7 · set by holes[3]`.
   - **Build report.** `out.search` writes per-step build seconds and
     rebuild count into the search summary and the run echo names the
     steps that are both expensive and frequently invalidated; a
     fixture with a volatile step placed under a stable one flags it.
   - **Container-only.** Step snapshots never reach the manifest or
     Postgres (the stored trace carries only the final build's frames);
     the LRU evicts under its byte cap and the build still completes.
   - **Heavy macro case.** On a filleted-bracket fixture (base, pads,
     clearance holes, ribs, fillets — the se-native-fields §Bracket
     shape built with `pg`, no solver), a rib edit rebuilds the rib
     boolean and the fillets above it only; base, pads and holes hit
     the cache, asserted via the counters and the per-step report.
   - **Recipe round trip.** `uv run main.py` on the stored `recipe.json`
     rebuilds the stored blocks byte-identical; `search` without
     `--fresh` starts from that recipe at the reheat temperature and
     its summary names the parent recipe hash; `--fresh` starts from
     the base design with no parent.
4c. **Incremental evaluation under the step keys** *(later, not v1;
   after 4b and se-native-fields slice 1)*. `evaluate(..., warm=prev)`
   keeps mesh and solver state for the regions below the high-water
   mark: the cut-cell grid (se-native-fields §Grid from the solid)
   re-cuts only the cells whose SDF the changed steps touched, the
   flow kernel re-rasterises only those holes, and the restart data
   rides the step key. Acceptance when sliced: a top-step edit re-cuts
   a bounded cell set (a cut counter) and the result equals a cold
   `evaluate` within 1e-12.

## Decisions (Reto 2026-10-20: "yes")

- Q1 Vendored into `src/precis_flow/` (numpy + scipy, both already
  precis deps), not imported from `breakthrough-annealer`.
- Q2 Cad envelopes first (slice 2); hexfold lumens in slice 3 with the
  nano propagator — the continuum kernel is wrong there.
- Q3 3-D (non-thin) chambers stay out; a follow-on (external Stokes
  solver) is named when a case needs it.

Sequencing: slice 1 is self-contained and builds now. Slice 2 waits for
pocket-from-molecule slice 2 to land, because that slice fixes the
provisional `Computer` signature in `precis_se.properties.COMPUTERS`.
Slice 2b follows slice 2. Slices 4a → 4b follow structure-workspaces
slices 1–2 and precede se-native-fields slice 1 (ranked in
[threads/structure-workspaces](threads/structure-workspaces.md)).

## Decided 2026-10-22 (Reto, chat-99)

- Slice 4 is a general design-optimisation kit, not the hole annealer:
  `precis_geom.optimize` over `evaluate`, the chamber as one of two
  examples. Split 4a (tracer to the player through a workspace) / 4b
  (kit + examples) so the order "tracer → visualizer, then the optimizer
  dogfood, then more physics" is one slice each.
- The design representation (feature graph with stable ids + snapshot
  + params layer + ask/tell + rich `evaluate` outputs) is the
  recommended design, **proposed, under discussion** — not ruled.
  Ruled within it: the recipe is mutable, built geometry immutable
  (one creator frame per object; no in-place transform, no per-move
  traces); modules are frozen units at both scales; scale evaluation
  through the evaluate cache and rebuild through the step cache.
- **2026-10-23:** the build-step stack cache with a high-water mark
  (4b bullet) is ruled — step keys `hash(name, params, parent keys)`,
  params + recipe key in every creator frame, general at both scales
  with the chamber as the smallest demo, stable → volatile ordering
  with a per-step cost/rebuild report, in-container LRU only;
  incremental evaluation is slice 4c, later.
- **2026-10-23, later the same day (direction; the representation stays
  proposed, under discussion):** pre-relaxed modules are the stored
  product and the main payoff ("we can benefit from module
  pre-relaxing") — each module relaxed once, capped, stored under its
  key; a build is an assembly, a DAG of independently built sub-stacks
  joined by `join` steps; a join starts from the pre-relaxed modules so
  only the seam buffer moves (seam-local relax with the residual-force
  check); a full relax runs only when the check fails and warm-starts
  from the joined geometry (`warm_from=<key>`; keys stay exact); each
  join records buffer radius, max residual force, steps (4b bullet
  §Build-step stack cache; [se-seam-spacers](se-seam-spacers.md)
  calibrates the defaults).
- Kit-facing names are general (`GapDomain`); the skill is
  `precis-workspace-optimize-help`, CFD its first physics section.
- Scripts build, evaluate and optimise with no DB and no se — the wheel
  is DB-free (structure-workspaces §Core design 7).
- cfdtest's `anneal.py` was not vendored in slice 1 (`src/precis_flow/`
  has no `anneal`); it enters as `precis_geom.optimize.anneal` in 4b.

## Decided before slice 2 (Reto 2026-10-21: "Parabolic default yes")

- Q4 Vertical speed profile. cfdtest's tracker (`tracker.py::track`)
  moves every particle horizontally at the gap-averaged speed whatever
  its height; the Hele-Shaw profile is parabolic (1.5× at mid-gap, 0 at
  floor and lid), which is what spreads a tracer pulse (Taylor
  dispersion). The player makes the uniform speed visible. Using the
  profile `U(z*) = 6 z*(1 − z*) · Ū` is a few lines but changes
  cfdtest's breakthrough numbers, so slice 2's reproduction test would
  compare against the profiled tracker, not cfdtest. Decided:
  slice 2 adopts the profile behind `profile='parabolic'|'plug'`,
  default parabolic; `plug` reproduces cfdtest for the reproduction
  test.

  **Mechanism landed (the commit carrying this line).** `trace` now takes
  `profile='plug'|'parabolic'`; `'parabolic'` weights the horizontal step
  by `6 psi (1 − psi)` (vertical model untouched). The *kernel* default is
  `'plug'` so the landed slice-1 tracer and its cfdtest-calibrated tests
  are unchanged — the parabolic-as-default *policy* belongs to slice 2's
  adapter, which selects `profile='parabolic'` and runs its reproduction
  test against `'plug'`. Only that policy choice is still Reto's to confirm;
  the capability is ready.

## Explicitly NOT in scope

3-D Navier–Stokes; two-phase or free-surface flow (EWOD droplets are a
different model); heat; chemistry in the fluid; the GIF renderer (the
web player of slice 2b replaces it).

## Target + blast radius

New `src/precis_flow/`; `precis_se` gains the adapter module and the
`COMPUTERS` entries; `cad` kind untouched beyond reading envelopes;
`precis_web` gains the Flow tab and its static JS (slice 2b). Slice 4a:
`precis_geom.flow` and `precis_geom.physics` in the `precis-geom` wheel
(`packaging/geom/`), `precis_se/flow.py` shrinks to the tree-bound half,
`workspace_import.py` (`out.physics` → `flow:` pocket, re-check), the
skill `precis-workspace-optimize-help`. Slice 4b: `precis_geom.params`,
`precis_geom.optimize`, the step cache in `precis_geom.design.build`
with `step`/`recipe_key` on `precis_geom.trace` frames,
`examples/workspaces/chamber-anneal/` and `manifold-split/`,
`precis_flow.stats` (outlet-mask list), the skill's optimisation
sections. Slice 4c: `precis_fields.grid` partial re-cut, `evaluate(warm=)`.
