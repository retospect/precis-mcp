---
status: draft
title: structural solution space — the axial member, prestress + tensegrity checking, in-tree solvers, SIMP generative fill
prio: high
model: opus
blocked-by: cad-sdf-rounding-and-field-export
---

# Structural solution space

The structural leg of the multiscale programme
(`multiscale-design-architecture.md` is the map). Design sessions 2026-09-05
and 2026-09-09 (Reto + agent). **Merged 2026-09-11:** this doc absorbed
`se-tension-elements-and-prestress.md` — the rung numbering below is that
doc's, so code references to "rung N" keep resolving here. Companions it
extends, never forks: `blocktree-library-build-plan.md` (block library,
discrete states — the molecular use case consumes its slice 2) and
`cad-machine-spec.md` (the cad half of the mobility tripwire below).

**Status 2026-09-11.** Shipped: rungs 1, 2, 4 + solution-space slice 1
(kinematic class `axial` with the asymmetric capacity pair,
`precis_se/stability.py` — equilibrium matrix + one SVD → `m`/`s`,
self-stress sign-feasibility, Pellegrino–Calladine second-order test,
`view='stability'`, the `cable` mechanism, the `fixed` support objective,
tripwire satisfied by construction via `stability.TRIPWIRE_LINE`); slice 2
(`precis/structsolve/formfind.py` force-density form-finder + the se
`formfind` op, solved poses stamped `origin: 'proposed'`); slice 3 = rung
5's null-space half (`stability.prestress_report`: declared `preload`s must
be a self-stress state, undeclared members completed by least squares and
vetted against role sign + capacity pair; prestress section in
`view='stability'`, warn-tier `prestress_state` DRC rule). **Open:** rung 3
(spring category), rung 5's bolted-joint load-sharing/separation half,
rung 6 (load path — build as complementarity, see below), slices 4–5.

## The two use cases are the spec

**Steel pipes and cables (se, metres).** A pipe strut is an se block (cad
envelope, `realized-by` a `component` in the `pipe`/`profile` category); a
cable is a connect with the `axial` kinematic class, tension-only capacity
pair, `mechanism: cable` (demands a BOM line — a cable joint with nothing
to buy is a drawing). The classifier answers the question that defines the
family: rigid / mechanism / **prestress-stabilized**, with the self-stress
sign-checked against each member's capacity pair (cables must end up in
tension, struts in compression, in the null-space state).

**Azobenzenes and bistable structures (nm, Å).** A photoswitch block gets
discrete states (`{trans, cis}`, blocktree slice 2) whose Δ(end-to-end)
changes an axial member's free length. The payoff is **state-dependent
stability**: run the classifier once per declared state. A tensegrity whose
tie is a photoswitch is an *actuated* tensegrity — actuation is the
equilibrium/prestress state changing between block states, and "does the
cis frame still hold shape" is the same `m − s` question asked twice.
Physics stays in `photoswitch-states-and-spectral-dof.md`.

## The model: ONE axial member, an asymmetric capacity pair

Not `tie` and `strut` as primitives — one axial member carrying
`(tension_capacity, compression_capacity)` plus a free length and a rate,
with tie/strut as its two limits:

| element | free length | rate | tension cap | compression cap |
|---|---|---|---|---|
| rope / cable | L₀ | EA/L | breaking load | **→ 0** |
| strut / contact interface | L₀ | EA/L | **0** | crush / bearing |
| coil spring | free length | k | to solid/max defl. | to solid length |
| rigid link | L₀ | → ∞ | member strength | Euler `P_cr` |
| preloaded bolt | grip | EA/L | proof load | (via the interface) |

**Tension-only is a slenderness limit, not a constitutive property** — a
rope carries no compression because `P_cr = π²EI/L²` vanishes as L grows
(the same Euler formula `precis_nm.mechanics` computes). So a slender rod —
tension-strong, compression-weak but not zero — needs no third class, and
the DRC can say "asked to carry 400 N compression against a 120 N buckling
ceiling" instead of "this isn't a rope".

**Clamping is composed, not primitive.** A bolted joint is tie + strut +
preload: a stiff tension-only bolt, a much stiffer compression-only clamped
stack, preload loading both. External load shares between them by stiffness
ratio, and the joint *gaps* when it overcomes the preload — the classic
bolted-joint diagram, no `clamp` primitive needed. This is rung 5's open
half (below), and the reason the whole ladder was `prio: high`: a correctly
preloaded bolt sees almost no cyclic stress while an underpreloaded one
sees the full range and fails in fatigue — same parts, different preload,
and nothing in the tree can tell them apart yet.

**The cross-scale vocabulary** (share the *shape*, not the module — revisit
at three consumers; units and honesty tiers differ, `se : cad :: nm :
structure` frames them as siblings):

| se (macro) | nm (atomic) | status |
|---|---|---|
| spring / axial rate | harmonic bond + angle terms | nm: built (`harmonic_strain_energy_eV`) |
| tension capacity | min-cut bond-rupture ceiling | nm: built (`mechanics.min_cut`) |
| compression capacity | Euler buckling of a tube | nm: built (`euler_buckling_ceiling_nN`) |
| unilateral contact (strut) | steric / vdW repulsion | neither |
| preload / prestress | — (no atomic analogue in scope) | se: built (rung 5 null-space half) |

A covalent bond is **bilateral and asymmetric** (Morse: steep repulsive
wall, softer tension branch to dissociation) — a spring with a rupture
limit, never a tie; the genuinely unilateral atomic element is non-bonded
contact.

**Vocabulary rule (settled with the cad track):** the *mechanism* names the
physical article, the *class* names what it does. `belt` + `tie` is a
tensioned belt used as a tie; `belt` + a future coupling class is a belt
drive (cad's `belt`/`gear` coupling spelling is the one se's eventual
coupling class reuses — never coin a third). Same for `chain`.

## The solvers: in-tree, and where they live

Core package `src/precis/structsolve/` — numpy/scipy only, **no store
access** (the cad posture: pure functions over passed-in data; handlers own
IO). Reto's 2026-09-09 decision deliberately re-opened the form-finding
deferral: solvers in-tree, but outside `precis_se`, preserving the
generator/checker split.

- **`formfind.py`** (shipped) — force-density form-finding: topology +
  force-density ratios + anchors → one sparse solve → equilibrium node
  geometry. Unit-agnostic; se feeds metres, nm will feed Å. Second
  consumer, named: `nm-stick-placement.md` stage 1 rents it topology-only
  (no initial guess) as the skeleton seed for its rigid-body pose relax,
  copying `precis_se/formfind.py`'s write-back contract verbatim.
- **`simp.py` — 3D density-field SIMP (the nTop leg, slice 4; engine
  shipped).** Domain = a **cad** keep-in expr voxelized by
  `cad.relate.component_sdf` sampling (keep-outs the same way);
  loads/supports from se declarations (`objectives.force`,
  `objectives.fixed`); 8-node hex FEA, matrix-free Jacobi-PCG; SIMP
  penalization + sensitivity filtering; Langelaar AM filter
  (`build_dir`); density field + compliance history under an iteration
  budget. **Advisory tier, never a hard DRC** — a compliance number from
  a voxel model is an estimate, and the honesty header says so. The
  result leaves the solver as a density array and enters the cad kernel
  as a **sampled-field leaf** (`cad-sdf-rounding-and-field-export.md`
  slice 2, `from_density`) — from there it is ordinary geometry:
  boolean-able, roundable, exportable. Reinterpretation as a strut/node
  graph (tubes/ribs/prism) stays a named later step in the multiscale map.

The equilibrium-matrix classifier itself stays in `precis_se/stability.py`
(one consumer today; extraction into structsolve happens when nm's
state-dependent stability lands and actually calls it).

## Storage of solver output

- **Form-found geometry** writes back as ordinary block poses stamped
  `origin: 'proposed'` (the 0005 origins facet) — a human-set pose is
  contract and is never overwritten. No new storage.
- **SIMP density fields are not blocks.** The run summary
  (content-addressed inputs hash incl. voxel pitch, `build_dir` and
  engine version; compliance, volume fraction, iterations, notes) goes on
  the se ref's meta — the pathway `results_json` posture. The **field
  itself is stored** as the cad field-leaf artifact (200³ float32 ≈ 32 MB
  upper bound; recomputing on read is a minutes-long job, not a read)
  and the block is realized by a cad design whose root holds that leaf —
  so `view='fab'`/`'print'`/`'bom'` see a realized block and never learn
  it came from an optimiser. Section previews through the existing SVG
  path. Artifact table vs `folder`-kind ref: one decision shared with the
  cad item's slice 2, made there.

## Solution-space integration

Solver outputs are minted as ordinary se/nm designs — candidates, linked
`serves` → a quest — and `quest/frontier.py` Pareto-ranks them against
human-set `rubric_objectives` (mass, compliance, member count, cost —
mass via `view='bom'`; a dedicated mass view does not exist and this doc
must not imply one: the se dogfood 2026-09-11 tripped on exactly that
phantom ref). Ranked library search is blocktree slice
4, unchanged. House discipline carries: **weights are human-set; a solver
may not tune its own objective** (this is also the multiscale doc's guard
on any future Bayesian outer loop).

## Assembly honesty

A tensegrity cannot be assembled one member at a time against a rigid
partial assembly — every intermediate state is a mechanism. When
`se-feasibility-and-cost.md`'s assembly-order existence check lands, a
prestress-stabilized design must report "requires simultaneous tensioning /
a jig / a tensioning sequence" as a **cost**, never an infeasibility.

## Open work (the remaining rungs + slices)

- **Rung 3 — `spring` component category + specs** (`spring_rate` N/m,
  `free_length`, `solid_length`, `max_deflection`, `wire_diameter`;
  `outer_diameter` universal since migration 0152) and a `spring` mechanism
  with `demands_bom` — a spring joint with nothing to buy is a drawing,
  same rule as `bearing`. A coil spring is not a flexure: it has free
  length (hence preload `k(L₀ − L)`), hard limits (solid length, max
  deflection — hard-tier DRC facts), and a catalogue. Series data: DIN
  2098/2095 exist, so the `component_series.json` mint path applies.
- **Rung 5, open half — bolted-joint load-sharing and separation.** The
  composition above, as a check: preload, stiffness-ratio sharing, gap
  opening under external load. Where three items meet: off-the-shelf rung
  3's grip stack-up is the geometric half; this is the force half; the
  feasibility doc's reliability tier gets its sharpest result (fatigue by
  preload). Separation is a complementarity question — build the check
  closed-form for the two-member joint, but don't grow it into a private
  active-set solver (next bullet).
- **Rung 6 — load path** ("which ties are taut under this load case").
  CLOSED 2026-09-12 — built as the complementarity solve, as this rung's
  deferral note demanded: `src/precis/structsolve/complementarity.py`
  (active-set core + taut/slack/seated status rows, landed e49fb80a;
  bistability probe follows). se wiring is `complementarity-solver.md`
  slices 1-bridge/2, blocked on the units window.
- **Slice 4 — SIMP engine**: ENGINE SHIPPED 2026-09-11
  (`src/precis/structsolve/simp.py` — pure numpy, matrix-free Jacobi-PCG,
  Langelaar AM filter `build_dir='z+'`, `overhang_violations()`,
  gyroid `lattice_fill()`; FD-gradient-pinned; **0 callers** as of
  2026-09-18). Engine debt, unchanged: damped-move/MMA for the
  AM-filtered OC oscillation; volume enforcement on the *printed* field.
  **The se bridge — specced 2026-09-18 with Reto, see §Slice 4 bridge
  below.** `blocked-by: cad-sdf-rounding-and-field-export` (slice 2, the
  field leaf the result binds to).

### Slice 4 bridge — `realize(strategy='simp')`, print intents, print-in-place

Decided 2026-09-18 (Reto). The goal is a printed organic unicycle toy;
the design rules are general.

**`realize(block|group, strategy='simp', ...)`** — a job, not an MCP
read (100k elements × 60 iterations is minutes on a node).

1. **Domain.** The target's keep-in envelope (the block's own envelope,
   or the union of a group's member envelopes in world pose) sampled via
   `cad.relate.component_sdf` at the voxel pitch; minus keep-outs; minus
   **cavities** (below). Pitch = a house figure (`se_capabilities.json`,
   new `simp_pitch` field, null until calibrated → the op demands
   `pitch=`), never a default in code.
2. **Loads / supports.** `objectives.force` → nodal loads on the nearest
   grid nodes touching the domain; `objectives.fixed` → supports. A block
   with neither is refused (the engine's `_check_node` posture, one level
   up). Nodes adjacent to a load or support are **passive solid** (a
   loaded face that thins to a skin is the classic SIMP artefact).
3. **Build direction is a pre-solve decision.** The AM filter bakes
   `build_dir` in, so `view='print'`'s orientation search cannot run
   after the fact. `realize` takes `build_dir=` (default: the envelope's
   largest planar face down, reported); `view='print'` on a
   SIMP-realized block *verifies* that orientation and skips the search,
   saying so.
4. **Result.** `cad.from_density(rho, 0.5, pitch)` → field leaf; optional
   `round=` / `open=` / `close=` applied on the field (the cad item's
   morphology, re-distanced — the only place an offset above a boolean is
   legal); the block is bound to a cad design rooted at that leaf (`cut`
   by exact bores / keep-outs, `add` designed seats); run summary on
   meta. Solved geometry is a **proposal**: `realize` on an
   already-realized block mints a sibling realization linked
   `realized-by`, never overwrites.
5. **Loads are declared, never scaled.** A 1:6 toy is not a 1:6 rider —
   the governing case is a squeeze, a drop, a foot. `set_load` the toy
   case on the scaled design; scale geometry only. `strength_z_ratio`
   (null today) is the figure that will matter for toys; calibrate it.

**Print `intent` on a print group** (an ancestor block in an fdm mode;
membership derived from the tree, no schema change — absorbs
`se-print-in-place-groups.md` — deleted 2026-09-19 once its group frame + one-3MF + fab-collapse behaviours landed in round B1 below; its in-place clearance rule is the `manufacture` half):

| intent | printed members | purchase members | joints with DOF | rigid joints | SIMP domain |
|---|---|---|---|---|---|
| `model` (fit-test, any scale) | print | **printed stand-in**: the catalog solid from `cad/catalog.py` (`_SERIES_FAMILIES` for fasteners; spec dims for bearings/axles), threads dropped, mating hole keeps the compensation | separate parts, one 3MF per group, one object per member | separate parts (fasteners print too — the fit test is the point) | per block |
| `manufacture` (the real part, print-in-place) | print | stays bought → a **cavity** = component envelope dilated by the fit clearance + an insertion path *or* a mid-print pause at the cavity's top layer | **in-place gap**: each side eroded by `gap/2` in the field; `in_place_clearance` finding when gap < house floor (new `min_clearance` capability, null today → the rule says so instead of guessing) | **fused**: min-union in the field, `blend` at the seam; a fastener whose both members fused is **elided** with a finding (`joint fused, <block> not needed`), the `screw` mechanism demand is satisfied by fusion | the fused group's keep-in minus cavities, one solve |

Same se design, two realizations, siblings under `realized-by`. Analytic
print-in-place features are not free the way organic members are: a
revolute bore under the AM rule wants a teardrop/diamond section — a
`teardrop_cyl` primitive (cad) or an `overhang` finding pointing at the
bore, not silence.

**Hand-off** (separate rungs, each optional): geometry 3MF as a
downloadable artifact (the figure/folder precedent) → headless slicer
(OrcaSlicer / Bambu Studio / PrusaSlicer CLI → `.gcode.3mf`) →
`bambuuzle` (Reto's `.gcode.3mf` editor: pause/insert injection with MD5
fix-up — the `manufacture` cavity pause lands here) → printer push
(Bambu LAN = MQTT + FTP; OctoPrint / PrusaLink = REST; Bambu cloud has no
public API). The fork print needs only the first rung.

**Atom models** (noted for design, not this slice): se's atomic mode
already has atoms as blocks and bonds as connects, and relations carry
`scale`. Every printable representation is a field op the cad item
already specifies: CPK = min-union of vdW spheres (exact), SAS = that
union `offset(probe)`, SES = `close(probe)`, ball-and-stick = spheres +
cylinders under the thin-feature validator, rotatable bonds = in-place
revolutes. Always `intent='model'`.

Acceptance for the bridge: `realize(strategy='simp')` on a cantilever
fixture block (load on one face, fixed on the opposite) produces a
realized block whose `view='print'` passes at the declared `build_dir`
with zero `overhang` findings; the same block with `intent='manufacture'`
in a two-member group with a `revolute` connect exports one 3MF whose two
objects are separated by ≥ gap everywhere (measure on the meshes) and
reports `in_place_clearance` when the gap is set below the floor; a
`model`-intent group containing a component-bound fastener exports the
fastener's stand-in as its own object; re-`realize` leaves the first
realization in place.

### Slice 4 bridge — round A built 2026-09-18

Landed (points 1–5 + the cantilever half of the acceptance):
`realize(block, mode, strategy='simp', volfrac, load_at, fixed_at,
pitch?, build_dir?, round?|open?/close?, max_iter?)` validates at op
time and enqueues an `se_simp` job (`precis_se/simp_job.py`,
`job_inproc`; `se` now `can_own_jobs`); the job body
`precis_se/simp_bridge.py::run_simp` (also the in-process test path)
voxelises the block's own envelope in its local frame
(`component_sdf_np`, element-centred), places `objectives.force`/`fixed`
at the named face token or posed port (se has no position on a load and
no keep-out on a block — both are stated, not invented; keep-outs skipped),
pins the elements under them passive (new `simp_optimize(passive=)`),
permutes the problem so any of the six axis tokens is the engine's `+z`
(default `build_dir` = the envelope box's largest face down, echoed),
`from_density` → optional morphology → `put_field` → a new cad design
rooted at `field:<sha>` → `set_binding` + `set_mode` + `build_frame
{origin: simp, build_dir}`; run summary on the se ref's `meta.simp`
(`last` + `runs`), each minted cad design linked `derived-from` the se
design. A block holds one binding, so re-realize mints `<design>-<block>-N`
and switches to it, leaving the previous design in place (`realized-by`
is component-only, so lineage is `derived-from` + `meta.simp.runs`).
`view='print'` on a `build_frame.origin == 'simp'` block skips the
orientation search (says so), reports the 45° voxel rule on the stored
field in place of the mesh `overhang` rule (`overhang_violations(...,
plate_at_first_solid=True)`). `simp_pitch` added null to every fdm
capability row (a `set_process_override` on the block also satisfies it).
Tests: `tests/test_se_simp_bridge.py`.

Left for round B: print `intent` (`model`/`manufacture`) on a print
group — cavities, in-place gaps + `min_clearance`, fusion + `blend` at
the seam, fastener elision, stand-ins, one 3MF per group — and the rest
of the acceptance paragraph (the two-member revolute group, the
`model`-intent fastener stand-in). Also open: `strength_z_ratio`
calibration (point 5's figure for toys), a house `simp_pitch`, the
cantilever's `objectives` still carry no position (`load_at`/`fixed_at`
live only on the realize op and in the run summary).

### Slice 4 bridge — round B1 (model intent) built 2026-09-19

Landed (`precis_se/printgroup.py`, tests `tests/test_se_print_intent.py`):

- **Where `intent` lives**: a parameter of the existing `set_mode` op
  (`set_mode(block=, mode='fdm/<m>', intent='model')`), stored as the
  `intent` key of the block's `build_frame` jsonb beside a pin — no new
  column, no migration; `ops.print_intent`/`ops.pinned_down` are the two
  reads, an intent-only record is not a pin, `clear_build_frame` keeps
  the intent, `set_mode(mode=null)` clears it (no mode, no group), a
  non-fdm mode with an intent is refused. The enum is complete
  (`ops.PRINT_INTENTS = model | manufacture`); `manufacture` is refused
  "not built yet".
- **Group = an fdm ancestor with an intent; members = its descendants**
  by `parent` edges, derived at read time (`printgroup.descendants` over
  a walk that stops where the next group root begins — a nested root
  owns its own subtree, the outer group lists it as one `nested group …
  printed separately` line and never exports it, and every block maps to
  its nearest root, so `view='fab'` has one row per root including nested
  ones). A root with no intent is not a group and everything reads as
  before (regression-pinned).
- **`model`**: fdm members print their cut solid (stamped holes keep the
  compensation); purchase members print as **stand-ins** — the cad
  catalog's analytic solid via the component's minted `meta.series`/
  `meta.size` (`cad.catalog.family_for_series` → `scene.part_spec`,
  proud heads shifted by their own height to the se head-top-at-origin
  frame), else spec dims (bearing = OD minus bore; otherwise the derived
  envelope, said so), else a `no_stand_in` warn finding naming what it
  needs. Instances/arrays and other-family members are `member_skipped`
  (info). Stand-ins are cad primitives, never meshes, so export is the
  ordinary `_component_meshes` fold.
- **One frame per group**: `cad.printability.orient` on the union of the
  member meshes in world pose with the root's rules/policy; root pin >
  SIMP member's baked `build_dir` (search skipped, said so; a second SIMP
  member disagreeing → `simp_frame_conflict` error, first-by-name wins;
  a root pin over a SIMP member → `simp_frame_overridden` warn) > search.
  Per-member frame findings are judged at that frame (a SIMP member in
  its own frame gets the 45° voxel rule instead of the mesh overhang
  rule, as `printing.report_for` does alone).
- **Output**: `view='print' args={'block': <root>}` = frame + candidate
  table + per-member findings; `fmt='3mf'` = one 3MF, one object per
  member in world pose, one rotation + one shared bed offset
  (`printability.rotate_all_to_frame`); STL refused for a group.
  `view='print'` no-args renders one section per group and skips its
  members; `view='fab'` collapses a group to one row (intent, member
  count, stand-in count, finding count or frame origin, the 3MF handle).
- Residuals closed alongside: `store.put_field` takes a per-ref
  `pg_advisory_xact_lock` (`FIELD_PUT_LOCK_NAMESPACE`) so concurrent puts
  cannot collide on `MAX(ord)+1`; `cad_propose.dry_run` attaches the
  store's `field_loader` so a proposal against a field-rooted design
  builds.

**B2 remains**: `intent='manufacture'` — cavities (component envelope +
fit clearance + insertion path or pause), in-place gaps + the
`min_clearance` capability + `in_place_clearance`, fusion of rigid
members with `blend` at the seam, fastener elision with its finding, the
fused group as one SIMP domain, the teardrop bore — and the acceptance
paragraph's two-member revolute group. Also still open: array/instance
members placed per member in a group (today `member_skipped`),
`strength_z_ratio` calibration, a house `simp_pitch`.

### Slice 4 bridge — round B2 (manufacture intent) built 2026-09-19

Landed (`precis_se/manufacture.py` + `manufacture_job.py`, tests
`tests/test_se_print_manufacture.py`; the intent table's second row and
acceptance sentence 2):

- **Op**: `realize(block=<group root>, strategy='manufacture', gap=?,
  fit=?, blend=?, pitch=?)` — `manufacture` enabled in
  `BUILT_PRINT_INTENTS`. One fused sampled field per group in the root's
  frame, field/CSG ops only (no mesh is operated on); the result is a new
  cad design `<design>-<root>-mfg[-N]` rooted at `field:<sha>`, bound to
  the ROOT the way `realize_simp` binds (per-ref lock, inputs hash of the
  whole group re-checked, `meta.manufacture` `last`/`runs`, the summary
  also in the field's provenance header so `view='print'` needs no ref
  lookup, `derived-from` link, sibling on re-run; a root carrying a solid
  of its own is refused). Inline when no SIMP member and the grid is
  under `SYNC_CELL_CAP` = 500 000 cells; else an `se_manufacture` job
  (its own job type — `se_simp`'s schema is closed and SIMP-shaped);
  above `MAX_CELLS` = 8 000 000 refused with the count.
- **Joint classification** (`classify_joints`): every connect between
  two members is rigid (`KINEMATIC_CLASSES − drc._MOVING_CLASSES` =
  `rigid`/`captive`/`axial`, and an undeclared joint — fused with a
  `joint_undeclared` info) or DOF; union-find over rigid printed pairs
  gives the fused components; rigid pairs blend with
  `fold.smooth_min_np(width=blend)` (the DSL's `blend:` semantics,
  default 0 = plain min), components hard-min.
- **In-place gap, seam-local by construction** (reviewer round): for
  each printed–printed DOF pair `(A, B)`, `A' = A \ dilate(B, gap/2)` and
  `B' = B \ dilate(A, gap/2)` — the dilation `fieldops.offset(−)` on the
  PARTNER's `redistance`d sample, the subtraction a max on the grid, so
  nothing else about `A`/`B` moves and a rigid seam with a third member
  stays exactly as sampled (a whole-member erosion opened it). Guarantee:
  face-to-face and pin-in-bore separations = `gap`; a re-entrant corner's
  worst case is `gap/2`; the report always quotes the measured minimum
  (the other side's re-distanced SDF over this side's exported mesh
  vertices). Every offset is **plus half a pitch** because the kernel's
  re-distance binarises (its surface can sit up to `pitch/2` outside the
  true one), so `gap`/`fit` are floors. `gap` required unless
  `min_clearance` resolves (new capability, null in every fdm row with a
  source; a `set_process_override` on the root also serves as the
  default); below the floor → `in_place_clearance` error, null floor →
  info "uncalibrated, taken as declared"; a pair still touching after the
  carve → `in_place_fused` error; a DOF pair whose sides a rigid path
  joins (union-find over rigid edges) → `dof_bridged` error and the
  export is refused.
- **Cavities**: bought members' B1 stand-ins re-distanced, dilated by
  `fit` (+ half a pitch; `fit` required whenever a cavity is to be cut,
  0 = exact analytic subtraction), subtracted; the report names each
  cavity's top layer above the bed in the chosen frame as the mid-print
  pause height, read off the SAMPLED cavity field on the stored grid
  (highest void cell along −down + pitch/2 − the union floor — exact to
  the grid in any frame; the AABB reading over-estimated a rotated
  member). No insertion-path search — the `bambuuzle` rung. No stand-in
  → `cavity_missing` error on top of `no_stand_in`.
- **Fastener elision**: a bought screw whose `fasten.fasten` grip stack
  holds ≥ 2 printed members all in one fused component is elided (`joint
  fused, <block> not needed`, info; nuts/washers in the stack go with
  it); its holes are excluded from the fused members'
  solids (`printed_solid(exclude=)` → `fasten.features_for(exclude=)`,
  a two-pass `group_geometry`; `model`/DRC pass nothing and stay
  byte-identical); `fasten.abstract_joints(fused=)` treats a
  `screw`-mechanism connect between fused members as satisfied — for
  `manufacture` only, `model` passes nothing and prints every fastener.
  A fastener across a DOF joint or whose stack leaves the group stays a
  cavity.
- **Fused SIMP domain**: `simp_bridge.solve_simp(domain_builder=)` hook
  + `DomainGrid`; `run_simp` picks `manufacture.simp_domain` for a
  manufacture root (union of member envelopes in the root frame minus
  stand-in cavities, optional `fit=` on the simp op; `prepare_simp` sizes
  the budget from `simp_box` and needs no root envelope); loads/supports
  stay the root's at `load_at`/`fixed_at` (face of the group box or a
  root port); a DOF joint between printed members is refused ("one solve
  is one body"). The member-qualified `member.face` form is NOT built.
- **Teardrop bore**: no primitive; a DOF axis-class joint whose declared
  axis lies within 45° of the build plate in the chosen frame →
  `overhang` warn naming both members, the connect, the axis and its
  angle; an undeclared axis → info "could not be judged".
- **Views/export**: `view='print'` on the root = frame (root pin > SIMP
  member's `build_dir` > search on the fused mesh, via the shared
  `printgroup.choose_frame`), objects (one per 6-connected component of
  the field, `fieldops.label_components`, named by the members whose
  material they hold), measured gaps, cavities + pause heights, elided
  fasteners, per-object frame findings, `manufacture_stale` warn when a
  member moved since the fuse, `manufacture_unrealized` + the plan before
  the first fuse; `fmt='3mf'` = one object per component (a DOF pair is
  two objects, a fused pair one); `view='fab'` = `intent manufacture, N
  objects, K cavities, E elided`.

**State of the section after B2.** Nothing in "Slice 4 bridge" remains
unbuilt except (a) the calibration figures — `simp_pitch`,
`min_clearance`, `strength_z_ratio`, all null-with-source in every fdm
row — and (b) the hand-off rungs (downloadable 3MF artifact → headless
slicer → `bambuuzle` pause injection → printer push). Left open, in
order: (1) **the mixed root** — today the fused design is ONE `field:`
leaf, every member re-sampled at the pitch, so sub-pitch features (hole
compensation, seats) are not preserved and every render says so; the
DSL already expresses a `field:` leaf combined with analytic `add`/
`cut`/`blend:` nodes, so the next item is emitting the fused field plus
the members' own nodes as one root expression; (2) an insertion-path
search instead of a pause; (3) a teardrop/diamond bore primitive (the
finding stands in); (4) array/instance members in a group
(`member_skipped` since B1); (5) `member.face` load tokens on the fused
SIMP domain. Whether the item folds into the package docstring is the
owner's call.

- **Slice 5 — nm state-dependent stability** (blocked on blocktree slice 2
  states): classify per declared state, plus — added 2026-09-11 from the
  multiscale intake — **sweep the switching pathway**: pose intermediate
  configurations between the two states and verify nothing collides or
  over-strains on the way (the assembly-path problem one level down).
  Endpoint-only classification would miss a switch that jams mid-throw.

## Tripwire contract (two-party, keep verbatim)

Any whole-structure constraint-vs-DOF counting that cannot run the
`m − s` + second-order machinery must emit **"first-order mobile; may be
prestress-stabilized — not checked"** rather than a bare "mechanism"
verdict. Every prior kinematic class is bilateral, so naive counting is
confidently wrong about tensegrity-class structures. Parties:
`precis_se/stability.py` (satisfied by construction —
`stability.TRIPWIRE_LINE`) and the cad track
(`cad-machine-spec.md` §NOT-in-scope records the mirror half). Recorded in
both places deliberately: a two-party contract that exists in one volatile
place is a contract with one party.

## Corrections on record (kept so they are not re-made)

- An early draft claimed `relate.translational_dof` would call a tensegrity
  "mobile, it will collapse". Wrong: that probe is a *per-joint geometric
  clearance check*, correctly scoped and honest (skips rather than
  approximates); it never analyses whole-structure mobility. The gap was
  "se has nothing to say about tensegrity", not "se is confidently wrong" —
  the danger was prospective, which is what the tripwire is for.
- An early draft justified `prio: high` on that false alarm; the priority
  survives on the bolted-joint argument.

## Deliberately deferred, named so they are not re-derived

Dynamics/vibration of prestressed structures; cable sag/catenary under
self-weight (fine for a taut tie, wrong for a slack one — declare the
assumption); creep and stress relaxation in synthetic rope (a `material`
question first); buckling FEA; multi-material / multi-load-case SIMP;
strut/node-graph reinterpretation of a density field; form-finding for
non-axial continua; port-offset node positions in the classifier (v1 pins
nodes at block poses); belt/pulley couplings (already deferred in
`joints.py`'s unknown-key error text). The physics-layer notes (modal
before time-stepping, one-way fluid coupling, thermal chain) live in the
multiscale map's §Physics.

## Resolved questions (one line each; git log has the detail)

Solvers in-tree (Reto 2026-09-09); nTop leg = density-field SIMP; tie/strut
derived from one axial class, class-vs-mechanism split resolves the
`belt`/`chain` naming collision; `translational_dof` ownership moot (needs
no change); se/nm share the shape not the module (revisit at three
consumers); classifier built with slice 1 rather than deferred (the
solution-space programme supplied the consumer); SIMP result is a cad
field leaf, not a mesh and not a block (Reto 2026-09-18); print `intent`
is `model` | `manufacture` on the group, loads declared never scaled
(Reto 2026-09-18).
