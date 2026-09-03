---
status: draft
title: se (structural envelope) kind — scale-agnostic space planner, suggestive assembly language, manufacturing-mode realization
prio: high
model: opus
---

# The `se` (structural envelope) kind — 3D environment / space planner

Design session 2026-09-02 (Reto + agent), general-3d-env worktree. Goal:
generalize the nm shape-then-fill pattern beyond the atomic scale — define
connected blocks ("a fork about this size, connected to a hub that goes
through a wheel so the wheel can rotate") in a deliberately *suggestive*
language, let an LLM job structure it and **prompt back** for what's
missing (what forces on the wheel? which extents are critical?), then
realize each block under a **manufacturing mode** (FDM ASA, FDM TPU, resin,
2.5-axis CNC steel, atomic assembler) whose process rules constrain and
inform the design.

The symmetry that locates this kind: **se : cad :: nm : structure.** nm
is intent-over-atoms renting the cad kernel as Å; se is
intent-over-solids renting the same kernel as metres. Same six-level IR
discipline, same propose-only LLM loop, same "model the agreement, not the
two sides" posture — different invariants (kinematic joints, tolerances,
process rules instead of threading, chirality, chemistry).

Ship as a **plugin** (Route B: `src/precis_se/`, entry points, own
migration namespace from 0001, dark behind `requires_setting`
`se.enabled`), exactly the nm scaffold.

## Relation to nm — no merge; se's core is the shared layer

Raised by Reto 2026-09-02: do nm and se merge? **No — two kinds; se's
core becomes the shared library, extracted *after* se slice 2.** The
kinds' invariants diverge (chemistry vs manufacturing) and nm is shipped
through 4b; but the structural layer both need is identical and becomes a
kind-neutral package once the second consumer exists in code: block tree
with name-keyed identity, envelope + pose (cad DSL), ports, connect
edges, instancing + arrays, the notes ledger, `envelope_fit`,
filled-fraction honesty — **and loads** (Reto 2026-09-02: "atomic systems
also have the same loads"). Force/torque objective vectors are
kind-neutral vocabulary in the same units (a molecular axle bears torque
like a macro one, at piconewton scale — float64 spans it); nm's
`mechanics.py` advisories are the atomic-side consumer, se's
stack-up/FEA-someday the macro side. se's data model below deliberately
copies nm's shapes so the extraction is mechanical. nm rents the
extracted core in a later refactor slice, or never — either is fine;
don't force it.

### Annotations — one superset registry, contract-classed

The generalizing move is **annotated surface fields on envelope
faces/ports**, and (Reto 2026-09-02) the annotation space is **one
superset, not per-kind vocabularies** — macro things can carry charge, a
nano block could carry color (structural color is real; nm already
plans optical property views). Concretely, an annotation is *a
dictionary with registered keys*: storage is an open jsonb dict per
face/port; one shared registry defines each key's meaning, units, type,
and **contract class** (the `pcb_capabilities.json` field discipline).
Contract class is the load-bearing distinction (corrected on
re-examination 2026-09-02 — LLM agents are first-class consumers here,
so "real = has a code consumer" is too strong):

- **checked** — the key *claims a constraint* (tolerance relation, face
  code, max load): it must have a code consumer (fit scorer, DRC rule,
  estimator) in the kind/mode's engaged set, and registering that
  consumer means naming the physics it encodes
  (`TermSpec.justification` posture). Declared-but-unchecked is the
  swallowed-facet bug (`**_kw` lesson — a silently ignored command) and
  must be *reported loudly* on validate/read.
- **descriptive** — the key states a fact (color, finish, provenance,
  "this face mates"): rendered into views/prompts, it is legitimately
  consumed by agents; no code consumer needed, no warning. It is
  dormant only if stored and rendered *nowhere*.

What differs per kind is not the vocabulary but the **cared-for set**
of checked keys: nm engages charge/face codes by default, se engages
mating/tolerance/load-direction; the selection is per kind (even per
mode), over the same registry. Honesty report is three-way: engaged /
declared-but-unchecked (warn) / descriptive (fine). Registry hygiene: a
key enters only together with its first consumer or an explicit
descriptive marking — no speculative keys. Face-code
complementarity/fit scoring lives in the shared core, written once
(note: a face code is storage-wise a dict entry but semantically a
typed object with theorems — stabilizer, chirality, margin; the dict is
transport, the semantics live in the typed consumer).

**Mate codes are scale-free** (Reto 2026-09-02: magnet patterns "to
only fit one, 2 or 3 different ways … same with molecular things").
The nm face-code alphabet — {donor, acceptor, bump, hole, +, −, null},
`nm-face-codes-and-scale.md` — already covers every keying carrier
raised: embedded magnet arrays are ±, machined key/spline surfaces are
bump/hole, chemistry is donor/acceptor. One mate-code abstraction,
three physical carriers, shipped once in the shared core. Orientation
multiplicity is already that doc's first theorem: the code's rotational
stabilizer order *is* the number of distinct ways it mates. se
generalizes the check from nm's "demand trivial stabilizer" to a
**declared multiplicity** the checker compares against — a polar magnet
ring meant to index 3 positions declares stabilizer order 3; "must
assemble one way only" declares 1. Status: design-only on both sides
(zero face-code code exists in nm today), so nothing constrains the
shared placement.

## Decisions (Reto + agent, 2026-09-02)

- **Kind name: `se` (structural envelope); handle prefix `se`**
  (`se234`-style code identities, registered via `precis.handle_codes`
  as `precis_nm.handles` does). Decided by Reto ("just call it
  structural envelope and leave it"), superseding the working name
  `mech`. The earlier `me` prefix idea was never available — `me` is the
  `memory` kind's record code in
  `utils/handle_registry.py::KIND_CODES`; `se` is verified free in both
  KIND_CODES and CHUNK_CODES.
- **Units: metres, float64, everywhere.** CONFIRMED by Reto 2026-09-02
  ("64-bit floats") after he offered int64 attometres and asked whether
  the nm-kind fixed-point rejection (`nm-kind.md` Decisions, 2026-08-31)
  holds; the agent holds it valid. The reasoning, so it isn't
  re-litigated:
  - **Range kills attometre outright**: 2⁶³ am = 9.2 m — an int64-am
    world is ±9.2 m across, dead for a space planner (femtometre buys
    ±9.2 km but inherits everything below).
  - The classic *pro*-integer "triangles" argument — exact predicates so
    mesh booleans don't leak or sliver — is real but doesn't apply: this
    kernel is analytic CSG (SDFs, closed-form ray/interval algebra) and
    never booleans triangle soups; triangles appear only at
    export/tessellation, exactly where the house pattern already
    quantizes (`pcb/gerber.py::_u` precedent).
  - The *anti*-integer argument stands regardless: rotations produce
    irrational coordinates, so an int store rounds after **every**
    transform, accumulating error rather than avoiding it.
  - **"Avoid conversions" (Reto's stated goal) picks float64**: the
    rented kernel and numpy speak float64, so an int64 representation
    converts around every kernel call; float64 metres is the
    zero-conversion choice. The one declared conversion anywhere is the
    single Å↔m multiply (1 Å = 1e-10 m exact) where an atomic-mode block
    binds an nm design — which exists under any unit choice. Within
    ±10⁶ m of origin float64 metres resolves below 10⁻⁴ Å:
    atoms-to-buildings in one unit, no scale switch.
  - Exact equality where actually needed (dedup, caching, lattice snap
    in the atomic mode) is solved at the hash/boundary only
    (`structure/canonical.py::geom_hash_c`), never in the
    representation. The cad kernel is unit-agnostic float64 and is used
    here as metres; declare the unit once, resolver-style.
- **The objects are blocks**, same word and same meaning as nm's: a thing
  *made* — from stock, from a printer, or recursively from other blocks —
  with software-style modularity, instancing, and reuse. A thing *bought*
  is a `component`/`part` link on a block or interface (BOM below), never
  a block.
- **Suggestive by contract.** Every field beyond a block's name is
  optional. Validation reports absence (filled-fraction honesty, the
  maze.py lesson) but never fails on it; an empty design must read as
  *unfilled*, not done. The design language is a medium for thinking out
  loud that hardens monotonically as answers arrive.
- **Hierarchy is real and survives realization** (Reto 2026-09-02: "a
  module made from 3 things replicated in an array 10 times on an axle
  … when realized, this remains as a sort of overlay"). Blocks form a
  tree; a module is a block of child blocks; templates are instanced by
  *reference, resolved at read time* (nm's `effective_envelope`
  pattern, `cad/graph.py::Design.instance`) — never copied. **Arrays
  are first-class block-level structure**: an array-instance node
  carries template name + `linear` (count, pitch, axis) or `polar`
  (count, radius, axis) — the cad text language's `linear:`/`polar:`
  modifiers lifted from node level to block level. Per-member deviation
  ("member 7 has a keyway") = an override entry on the array node or an
  explicit *unlink to concrete copy*; never silent mutation of the
  template. And **realization never flattens the tree**: the block
  graph stays canonical, realized geometry is derived and regenerable
  (sketch-canonical/copper-derived discipline); an array member's solid
  is the template's solid under that member's transform, stored once
  and posed N times; DRC/fit findings name blocks, not raw geometry —
  after realization you can still ask which member a face belongs to.
  Print-in-place merges the physical part but keeps the overlay: block
  names become *regions* of the merged solid.
- **Geometry vocabulary: CSG primitives + one profile tier; no atoms,
  no free meshes** (Reto 2026-09-02: "waterproof shell from straight,
  bent things … how far do we go"). se never models sp3/sp2 or crystal
  structure — that is nm/structure's whole job, reached through the
  atomic-mode L3 binding. Macro geometry stays analytic CSG (the
  kernel's primitives cover box/disk/octagon bodies already); the one
  genuine addition is a **2D profile tier**: closed or open
  arc-polylines (`line | arc` segments — exactly `pcb/realize.py`'s
  track-segment shape, with `pcb/geom.py::fillet_polyline` /
  `rounded_polygon` as in-tree prior art) plus **extrude** and
  **revolve** into solids. The OCCT plumbing already consumes 2D rings
  (`cad/_occt.py::_prism`/`_loft`, today private to ngon/frustum
  realization), so this is exposure of existing plumbing, not
  invention — but it is cad-kernel work (a new `Primitive` answering
  classify/ray/SDF for an extruded profile), sized as such in slice 5.
  **Loft rule** (Reto 2026-09-02): the tier admits exactly the
  operations whose 3D membership test reduces closed-form to a 2D
  profile query — extrude (identity), **tapered extrude** (section at
  z = profile scaled by s(z); unscale the point, test once — the
  frustum→cylinder generalization, covers draft angles and tapered
  bosses, and `_occt.py::_loft` already realizes it for export), and
  revolve (rotate into the (r,z) plane). A **true multi-profile loft**
  (circle morphing to square) has interpolated sections with no
  closed-form membership: OCCT could export it but the analytic
  kernel — and therefore every DRC/clearance/DOF check — would be
  blind to it, breaking "the analytic IR is truth." Deferred, and
  deliberately so: a blend between two differently-shaped ports is
  intent that L0/L2 already fully states (ports + connect + loads);
  the smooth joining solid is an L3 *answer* for generative filling
  later — hand-authoring a loft at envelope level specifies the answer
  instead of the question. Interim escape hatch: bound/component
  geometry, opaque to DRC, eyes open.
  The tier is chosen to exactly cover the two hard acceptance cases:
  a funny-shaped cam = tabulated r(θ) sampled into an arc-polyline
  (arc-smoothed), extruded; a 2D trace cut into a surface = open
  arc-polyline + width, extruded and cut as a pocket. Straight +
  circular-arc segments only — arcs keep the SDF/ray math closed-form;
  splines are approximated to arc-chains at authoring time (the gerber
  precedent: no bezier primitive). **Out, deliberately**: arbitrary
  watertight shells (imported meshes, NURBS) — the analytic kernel
  cannot answer classify/ray on them; a shape that can't be said as
  CSG + profiles is a `component` binding or a named deferred item.
  Also out for now: mirror and non-uniform scale
  (`cad/vec.py::Transform` is rigid by construction — det +1, no
  scale/shear); a mirrored template is a second template until the
  kernel learns reflection, and note mirroring interacts with
  mate-code chirality (theorem 2) when it does.

## The IR — six levels

Same invariant as pcb/nm: dropping everything above level k leaves a valid
level-k object; a move at level k dirties only levels above.

- **L0 — block graph.** Blocks + ports + intent connections, hierarchical
  (module trees, template refs, array-instance nodes — see the hierarchy
  decision above). No geometry. "Fork, wheel, hub; fork holds hub; hub
  goes through wheel." BOM intents live here too ("ball bearing ×2
  between hub and wheel" — a `component` kind link with quantity, not a
  block).
- **L1 — envelopes + pose.** Per-block analytic envelope in the cad
  mini-DSL, **reused verbatim** (`precis.cad.dsl::build_config`/`parse`
  — all eleven primitives: `box:`, `cyl:`, `cone:`, `tcone:`, `sphere:`,
  `torus:`, `hex:`, `ngon:`, `frustum:`, `pyramid:`, and `chamfer:`
  (made buildable 2026-09-02 in the same worktree: a half-space bevel
  tool positioned by the node transform, `cut`/`intersect` only, never
  a component's base; exports substitute an AABB-clamped box)), in
  metres, plus rough pose. "About this size" is an envelope, nothing more.
  Clearance/enclosure via `cad/relate.py::component_sdf` — its
  shaft-in-bored-hub case is literally the hub-through-wheel interface.
- **L2 — declared invariants.** Three families, all stored explicitly,
  never derived from L3 geometry:
  - **Joints split into two orthogonal axes** (Reto 2026-09-02: "rigid,
    sliding, rotating, flexible … snap together, screws, keyed" — the
    original flat list conflated them). The *kinematic class* says what
    motion the connection permits: `rigid | revolute | prismatic |
    cylindrical | planar | ball | compliant | captive`, with axis and
    allowed-DOF. `compliant` is a DOF with stiffness rather than
    freedom (TPU living hinge, flexure); stiffness is an annotation,
    advisory-tier until a real consumer exists. The *mechanism* says
    how the relation is physically realized — a separate, optional,
    suggestive field drawn from a registry: `snap | screw | press |
    key | magnet | bearing | bond | integral`. Each mechanism entry
    carries its own checked keys and *implied demands*: `press` ⇒ an
    interference tolerance relation must exist; `bearing` ⇒ bore/OD
    relations + a BOM `component`; `snap` ⇒ engagement depth + a
    flexing member; `bond` at the atomic scale is a single covalent
    bond — which *is* a revolute realization; `integral` =
    print-in-place, same body. Substrate today: nm's declared-DOF is
    the seed (`precis_nm/ops.py::_op_declare_dof`,
    rotational/translational + axis ports) and nothing else in the tree
    has joints. The L4 agreement check is **declared vs derived DOF**:
    `cad/relate.py::translational_dof` already probes actual travel
    against the CSG SDF (a declared `revolute` that can translate along
    its axis is a finding); the rotational probe is its missing twin.
    "Wheel can rotate on hub" is `revolute` about the hub axis,
    mechanism unset or `bearing`; a rotaxane is `captive` (interlocked,
    no mechanism — checked by clearance + connectivity via
    `component_sdf`).
  - **Tolerances as relations between named measures**, not absolute
    numbers on one block: `wheel.bore.d = hub.od.d + 0.2mm ± 0.05mm`.
    Copy the `pcb_measures` shape (hard/soft/gauge) — hard gates
    realization, soft is an objective, gauge just reports. Tolerance
    *stack-up* over a chain of interfaces is the genuinely new computation
    (nothing in the tree does it today); it is a pure L2→L4 evaluation.
  - **Loads**: force/torque vectors, duty ("push around how"), cycles —
    objective vectors in real units on blocks and connections, the pcb
    `objectives.py` posture (intent as physics, never a bespoke rules
    table; every cost term carries a one-line justification). Loads are
    kind-neutral shared-core vocabulary (see above).
- **L3 — realized solids.** Per block, one of: a cad node set (authored,
  generated, or proposed), an instanced template
  (`cad/graph.py::Design.instance`, name-keyed as in nm — identity is the
  block name, never a row id), a `component` binding (bought part with a
  datasheet envelope), or — atomic-assembler mode — a **bound `nm`
  design** (unit boundary declared once: 1 Å = 1e-10 m exact).
- **L4 — metrics / agreement.** The realized solid *checked against* the
  spec, never stored twice: `envelope_fit` generalized (worst protrusion
  of the L3 solid beyond the L1 envelope + margin, in the block's local
  frame, posed at identity — the nm function's design transfers whole);
  interface fit (clearance vs the declared tolerance relation); mass /
  volume / centre-of-mass via `cad/bulk.py`; tolerance stack-up results;
  closed-form load advisories (beam/buckling in the nm `mechanics.py`
  register — advisory tier, never a gate, until a real FEA rung exists).

  **Design DRC, two tiers, the pcb split** (Reto 2026-09-02: "the
  envelope grammar can be DRC'd — are the parts solid — akin to pcb"):
  - *Graph tier, before any geometry exists* (pcb `ir.py`'s pre-L5
    checks): dangling ports, connects referencing missing blocks/ports,
    joint DOF contradictions (a block both `fixed` and `revolute` to the
    same partner), tolerance relations naming measures that don't exist,
    over-/under-constrained assemblies. Backs `view='drc'` on an
    unfilled design.
  - *Geometry tier, on envelopes and realized solids*: **solidity** —
    positive volume (`cad/bulk.py::volume`), a block's declared pieces
    actually connected (`cad/relate.py::connectivity` — the
    spokes-not-touching-rim defect already filed as
    `cad-connectivity-lint`; this kind is its paying customer), no
    net-empty solids (cuts consumed everything), no zero/sub-minimum
    walls; undeclared interpenetration between blocks (overlap that no
    joint or fit relation sanctions). Findings in the structure-validate
    shape: rule / where / measured / expected / `suggested_fix`.
  Process DRC (overhangs, pockets, tool radius) stays at L5 — it needs a
  mode and a build frame; design DRC needs neither, and runs on every
  read like nm's validate.
- **L5 — fabrication plan.** Per block (or per assembly for
  print-in-place): the assigned manufacturing mode, the **build frame**
  (print orientation with its own "down", distinct from the part's working
  frame), process DRC findings, applied process skills (below), and the
  export artifact (cad's existing three routes: OpenSCAD / STL-3MF /
  STEP; the atomic mode exports nothing — its L3 binding *is* the nm
  design).

## The propose/interrogate loop

Fill paths in preference order, as in nm: (1) parametric generators where
the math is known (gears, threads, shells — later slices); (2) the
`se_propose` job; (3) hand ops.

`se_propose` mirrors `nm_propose` (`precis_nm/job.py::SPEC` is the
template): tool-less `claude -p`, targets **exactly one block or one
interface**, output is JSON — ops script + rationale — dry-run against a
scratch, never-persisted design and gated through validate before anyone
sees it. Prepare/finish op split (`_prepare_generate`/`_finish_generate`)
so a failing later op can't orphan a store write.

The distinctive addition is the **interrogation contract**: when the
target is underdetermined, the job's *deliverable is questions, not
guesses*. The proposal JSON carries a `questions[]` array — each one
naming the missing invariant it would pin down ("what radial and axial
load does the wheel see?", "is the fork's outer extent constrained by
anything it must fit inside?"). A proposal may mix: pin what's
determined, ask about what isn't.

**Questions persist as structured, linkable notes** (decided, Reto
2026-09-02: "LLM support to talk to itself, super-structured linkable
notes"). A note is a first-class name-keyed row on the design — kind
`question | answer | decision | observation` — carrying the target
block/interface/measure *names* it's about, prose body, and name-keyed
threading (`answers`, `supersedes`). The note ledger is the medium by
which the propose loop talks to itself across sessions: a later
`se_propose` run receives the open notes for its target and must
either answer them (an answer note + the L2 op that writes the invariant
asked for) or leave them standing — never silently re-derive. Decisions
land as `decision` notes, so "why is the bore +0.2 mm" has a durable,
citable home. Surfaced by `view='interview'` (open questions first,
threads collapsed) and counted in the validate header next to
filled-fraction; open-question text folds into the design's one
`card_combined` so an unanswered design is *searchable by what it doesn't
know yet*.

## Manufacturing modes — the process layer

Copy the pcb capability architecture wholesale; it is the only
process-rules precedent in the tree and it already has the right bones:

- **`src/precis/data/se_capabilities.json`** (or plugin-local
  equivalent) — capability rows as **versioned data, not Python
  constants**, one row per (process, material): `fdm/asa`, `fdm/tpu`,
  `sla/clear-resin`, `cnc-2.5ax/steel`, `atomic/—`. Two tiers per field
  (published/physical minimum + house default at a margin), with
  `source`/`retrieved`/`field_confidence`; an unpublished field is `None`,
  never a figure carried over from another row. Fields per process
  family. FDM, with numbers **validated against slicer docs + community
  design guides 2026-09-02** (`perplexity-reasoning:292746` — carry its
  per-claim sources into the seed rows): layer height (0.2 mm default,
  0.4 mm nozzle class), extrusion line width (0.42–0.48 mm ≈ 1.1×
  nozzle — *not* 0.2 mm; sub-0.3 mm widths on a 0.4 mm nozzle are
  out-of-spec/weak), max overhang angle per layer (45° confirmed as the
  slicer-default rule at 0.15–0.25 mm layers; tighten toward 40° near
  the 0.32 mm layer ceiling), min bed-contact area as a function of
  total print size, max unsupported bridge span **per material** (~10 mm
  is the reliable community limit for PLA-class; ASA bridges worse under
  low fan/enclosure — keep ~10 mm warn; TPU bridges worst — ~5 mm; the
  flat global 5 mm was over-conservative), min wall/hole, hole shrink
  compensation, **anisotropic strength vector** (weak across layers —
  the reason the build frame feeds back into design); CNC-2.5ax: everything must be a pocket
  reachable from the top, internal corner radius ≥ tool radius, min wall;
  SLA: drain/vent, island/support, min feature; atomic: delegate — the
  block's realization must be an nm design that passes nm's own validate.
- **One resolver** (`pcb/rules.py::resolve_net_rules` pattern):
  most-specific-first (explicit block override → load-derived → capability
  floor), always clamped to the physical floor regardless of tier, and
  every consumer — the implementer, process DRC, cost — reads through it.
  Decided posture (Reto 2026-09-02): **rows ship with the validated
  generic defaults; the model overrides if it wants** — an agent/propose
  job may loosen or tighten any field per block or per design through
  the override layer, but never below the physical floor. This also
  de-blocks seeding: generic 0.4 mm-nozzle-class rows land in slice 5
  as-is; printer-specific house-tier tuning is an optional later
  refinement, not a prerequisite.
- **The implementer** (`pcb/realize.py` discipline): a pure function of a
  settled design snapshot, run at checkpoints, never in an inner loop.
  Given block + mode it chooses the build frame (orientation search
  scored by overhang violation, bed contact, bridge count, and strength
  vector vs the declared load axes), runs process DRC over the L3 solid
  in that frame, and emits findings in the structure-validate DRC shape:
  rule / where / measured / expected / `suggested_fix`.
- **Process skills are named, first-class rewrites**, not folklore: e.g.
  `bridge-closing-a-bored-ceiling` — a round hole in a pocket ceiling is
  printed by spanning chords tangent to the hole to shrink it to a
  bridgeable gap, closing the field one layer up, leaving only the hole
  rounding. The implementer *proposes* a skill application as a
  `suggested_fix` (with the geometry ops that implement it); applying it
  is an explicit L3 edit, so the design stays legible and the skill's
  provenance is on the record. Skill catalog grows per process family.
- **Feedback into design**: process findings can imply L2 edits ("FDM
  bore shrinks ~0.15 mm at this diameter — the wheel-bore tolerance
  relation should absorb it") — surfaced as advisory findings referencing
  the tolerance relation by name, applied only through the normal edit
  path.

Mode is assigned per block; `whole-assembly` print-in-place is a mode
assignment on the root that forces one shared build frame and adds the
in-place clearance rule (moving joints need printable gaps ≥ the process
floor).

## Schema sketch (plugin namespace `precis_se`, from 0001)

Mirror nm's tables and its two hard-won lessons — persist is
retire-all/reinsert-all, so **everything cross-referencing is name-keyed
text, never an FK to a block row id**; ports persist in lockstep keyed by
`(block name, port name)`.

- `se_blocks` — ref_id · parent/template self-refs · name · pose
  (`double precision[]`, metres/degrees) · envelope text (cad DSL, NULL =
  pure L0) · array spec (nullable: linear/polar count+pitch+axis, member
  overrides jsonb) · descr/use_ · mode (nullable — unassigned is honest)
  · build_frame (nullable) · bound_design (cad or nm slug) · retired_at.
- `se_ports` — block name-keyed · name · roles · axis/direction ·
  annotations jsonb (superset registry keys).
- `se_connects` — port↔port · joint kind + DOF · objectives jsonb ·
  meta.
- `se_measures` — named measures + relations (the tolerance
  expressions), hard/soft/gauge.
- `se_bom` — block/connect name-keyed → `component`/`part` ref ·
  quantity.
- `se_notes` — the interrogation ledger: name · kind
  (`question|answer|decision|observation`) · target names (`text[]`,
  block/port/measure names) · body · `answers`/`supersedes` (name-keyed
  text, never row-id FKs) · author (job id or 'reto') · retired_at.

ONE `card_combined` chunk per design for intent search; geometry and graph
live in the tables, never chunks (nm/ADR-0041 storage rule).

## Ship order (each slice lands green)

1. **Scaffold** — plugin skeleton, migration 0001, dark flag, `se`
   handle code, empty handler registering. (nm build order step 1.)
   SHIPPED 2026-09-03 together with slice 2's block-tree half:
   blocks/instancing/**arrays** + tree/block views + set_envelope
   (`src/precis_se/`).
2. **L0/L1 core** — blocks/ports/connects ops + tree view, incl.
   template instancing + array nodes; envelope via the cad DSL;
   clearance/fit views renting `component_sdf`. Validate with
   filled-fraction honesty from day one, plus the geometry-tier
   solidity checks (volume/connectivity/net-empty) — they need nothing
   but envelopes. REMAINING: ports/connects ops, clearance/fit views,
   validate (blocks/arrays shipped with slice 1).
3. **L2 invariants + graph DRC** — joints as kinematic class +
   mechanism registry (incl. mechanism-implied demands); measures +
   tolerance relations; loads as objective vectors. Tolerance stack-up
   evaluation. Declared-vs-derived DOF check renting
   `relate.py::translational_dof` (the rotational probe twin is new
   kernel work, may slip a slice). Graph-tier DRC (dangling ports, DOF
   contradictions, unresolvable relations) backing `view='drc'`.
4. **Propose/interrogate** — `se_notes` ledger + `view='interview'`
   first (it's pure store work and useful for hand design alone), then
   the `se_propose` job + dry-run gate wired to read/answer notes.
5. **Realization + first mode** — L3 binding (cad node sets, instancing,
   component binding); `envelope_fit` port; **the profile tier lands in
   the cad kernel here** (arc-polyline + extrude/revolve as a new
   `Primitive`; cam-from-r(θ) and trace-pocket as acceptance tests);
   capabilities file + resolver + implementer with **FDM only** —
   `fdm/asa` and `fdm/tpu` rows seeded with the validated generic
   defaults (`perplexity-reasoning:292746`; decided 2026-09-02 —
   defaults + model override, printer-specific tuning optional later),
   each field with `source`/`retrieved`; orientation search + process
   DRC; export through cad's existing routes.
6. **More modes** — CNC-2.5ax, SLA, TPU row, print-in-place; the process
   skill catalog (bridge-closing first); atomic mode = nm binding with
   the declared unit boundary.
7. **Skill file last** (`precis-se-help.md`) — after behavior exists;
   a skill describing target state misdirects agents.

Deferred, named so they aren't re-derived: FEA/load simulation beyond
closed-form advisories; generative placement/optimizer over an assembly
(the pcb SA engine pattern applies but no customer demands it yet —
see `pcb-global-codesign-north-star.md` before building it); generative
*filling* of envelopes (Reto 2026-09-02: "we'll do generative filling
later"); mate codes with declared multiplicity (ships once in the
shared core alongside nm face codes — see Annotations); spline profile
segments (arc-chain approximation until then); true multi-profile loft
(generative-fill territory — see the loft rule in Decisions);
mesh/NURBS import;
mirror + non-uniform scale in the kernel; gear/thread generators;
multi-axis CNC; costing; nm renting the extracted shared core.

## Open questions for Reto

Resolved 2026-09-02: units (float64 metres); kind name + handle prefix
(`se`); no nm merge (shared core extracted after slice 2); annotations
(one superset registry, contract-classed); interrogation persistence
(structured linkable notes ledger); hierarchy/arrays first-class with
realization as overlay; joints split kinematic-class/mechanism; mate
codes scale-free with declared multiplicity; geometry = CSG + profile
tier, no atoms/meshes; FDM numbers validated
(`perplexity-reasoning:292746`); capability seeding = validated generic
defaults + model override (printer-specific house tuning optional,
whenever Reto names the printers — no longer blocking).

None open.
