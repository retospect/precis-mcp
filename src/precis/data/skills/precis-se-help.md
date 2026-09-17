---
id: precis-se-help
title: precis — the se kind (structural/mechanical designs in metres)
summary: author a block-tree mechanical design (envelopes, ports, joints, axial members with preload, measures, BOM), then check it — validate/drc/clearance/stability/fasten/freedom/bom/interview; discrete block states + stimulus-labelled transitions (declare_states/declare_transitions/set_current_state, args={'state':...} to pose transiently, view=sweep to check every declared state at once); OPTICAL domain: FRET links as a comm channel (set_chromophore/set_optical_link/set_optics, view=fret); ATOMIC mode (the merged nm kind) designs chemistry: threading, dof, bind_structure, generate, view=mechanics/literature; put is a full REPLACE, edit ops= is the incremental path
answers:
  - how do I author an se structural design — blocks, connects, joints?
  - how do I model a cable / spoke / strut — a prestressed axial member?
  - how do I declare a bistable/photoswitch block's two states, {loaded,bonded} or {trans,cis}?
  - how do I check a block for clash in a specific state, or across every declared state?
  - how do I check whether my structure is rigid or a mechanism (stability)?
  - how do I declare loads, supports, measures, manufacturing mode, BOM?
  - what units does se use, and what do envelope w/d/h mean?
  - how do I design a molecular machine as nested blocks before filling in real chemistry?
  - how do I declare a port and connect two blocks with a capability gate?
  - how do I record that a macrocycle is threaded onto an axle?
  - how do I bind a block's ports to real atoms in a structure design?
  - how do I find literature for a block before filling it with real chemistry?
  - how do I check whether a bound block's atoms actually fit its declared envelope?
  - how do I model FRET between two blocks — energy transfer as a communication channel?
  - why is my FRET link dead even though the blocks are close enough?
  - how do I declare a required transfer efficiency and check the geometry against it?
applies-to: get/search/put/edit/delete/link (kind='se')
status: active
tags: verbs, design
kinds: se
---

# precis-se-help — the call surface

An `se` design is a **block tree** (blocks with poses + cad-DSL envelopes)
plus **connects** (port↔port edges carrying a joint class, objectives,
and optional preload). You author with typed ops, then read views that
check what you claimed. For the design *workflow* (abstraction ladder,
refine, tradeoffs) see `precis-se-design-help`; for screwing a design
together — picking a real ISO screw, what a printed part's threaded hole
should be, and what `view='fasten'` reports — see `precis-se-fasten-help`.

## Units and geometry conventions — read first

- **Everything is metres, newtons, radians.** `envelope`/`pose`/`rot` are
  bare numbers, **not** run through `precis/utils/units.py`'s
  unit-required ingest boundary (that boundary is for `cad`'s own
  hand-authored text; `se` parses/stores its envelope DSL and pose/rot
  vectors in the pre-existing bare-SI convention — see
  `precis/utils/units.py`'s module docstring). `cyl:r0.02h0.01` is a
  2 cm × 1 cm cylinder — the cad kernel is unit-agnostic; `se` stores
  metres.
- **Envelope `box` `w`/`d`/`h` are FULL dimensions**: `box:w0.028d0.240
  h0.020` is a 28 × 240 × 20 mm block — centred in x/y, **base at
  `z=0`** (spans z=0..h, like `cyl`). Verified against the kernel
  (`box:w0.028d0.240h0.020` → AABB x ±0.014, y ±0.12, z 0..0.02);
  an earlier revision of this skill claimed half-extents — wrong.
- `cyl` has its **base at the pose** (not centred); `sphere` is centred.
- `rot` is a bare **radians** vector (Euler, composed `Rz@Ry@Rx`) — reads
  render it back in degrees (the shared neat formatter), but the op-level
  value you author is radians, not degrees. `90°` about z is
  `rot: [0, 0, 1.5707963267948966]` (`math.pi / 2`), not `[0, 0, 90]`.
- `pose` is the block origin in the parent frame, bare **metres**.

## put vs edit — put is a full REPLACE

`put(kind='se', id=…, text=<json>)` **replaces the whole design**. The
payload's only top-level keys are `description`, `ops` and `scenario`
(anything else is rejected — an unrecognised shape used to silently empty
the design). Incremental changes go through `edit(kind='se', id=…,
ops=[…])`. Op batches are **atomic**: one bad op rolls the whole batch
back.

`scenario` names the **production context** that governs the design —
`prototype` (one off, lifetime physics OFF) · `small_batch` (100, indoor
five-year service) · `mass_production` (100 000, full lifetime physics).
It decides which checks are meaningful, so `view='validate'` and
`view='drc'` both print which scenario governed the run — or say
`scenario: none chosen` rather than assume a default. An unknown name is
rejected; an *absent* key leaves an earlier choice standing (the scenario
is design-level context, not part of the block tree `put` replaces).

**Blocks are addressed by label or by uid.** A label (the block `name`)
is unique within a design and is the usual way to say which block you
mean. A block also carries a stable **uid** — shown as `(uid #41)` in
`view='block'` — that survives edits and re-`put`s and is what every
stored cross-reference actually points at. **Anywhere an op or a view
takes an existing block**, `'#41'`/`'uid:41'` addresses it by uid
instead — including the block half of a `'block.port'` endpoint
(`connect a='#41.bore'`). Use it when a label is ambiguous; the error
then lists every matching uid. What gets *stored* is the block's label
either way, so a design written by uid reads back the same as one
written by name. (A block may not be *named* `'uid:…'` — that would be
unaddressable — nor contain `'#'`.)

## Ops (exact parameter lists)

- `add_block` — `name` (req) · `parent` · `pose` [x,y,z] m (bare) · `rot`
  [x,y,z] rad (bare, Euler `Rz@Ry@Rx` — see "Units" above) · `envelope`
  (DSL string) · `desc` · `use`
- `instance_block` — `name`, `template` (req) · `parent`/`pose`/`rot`.
  Rejects envelope/desc/use (they live on the template).
- `array_block` — `name`, `template` (req) + exactly one of
  `linear` `{count≥2, pitch>0, axis?}` | `polar` `{count≥2, radius≥0,
  axis?}`. Blocks only — **there is no array form for connects**; N
  spokes are N `connect` ops (generate them programmatically).
- `set_pose` — `block` (req) + `pose` and/or `rot`
- `set_envelope` — `block`, `envelope` (req; null clears) · `origin`
  `'user'|'proposed'`. Rejects instances/arrays.
- `remove_block` — `block`
- `add_port` — `block`, `name` (req; no dots) · `roles` [str] ·
  `direction` [x,y,z] (normalised) · `annotations` dict · **atomic
  mode:** `expected_element`/`expected_hybridization` (what
  `bind_structure` checks the bound atom against). `roles` is a
  **capability set the caller asserts**, never checked against real
  chemistry — see `connect`'s gate below. `{"external": true}` in
  `annotations` marks a port as intentionally left unconnected (an
  antenna) — `view='validate'`'s `unconnected_port` reads it as `info`
  instead of `warn`.
- `remove_port` — `block`, `name`
- `connect` — `a`, `b` (req, `"block.port"` — **ports must already
  exist**; connect never auto-creates) · `joint` dict · `objectives`
  flat dict (`force`/`torque`/`duty`/`cycles`) · **atomic mode:**
  `kind` `bond|interaction` (default `bond`) — a `bond` connect
  requires **both** ports to afford `'covalent'`, or, with
  `objectives={'role': ...}`, both to afford that named role instead
  (rejection names the port's actual roles); `interaction`
  (non-bonded) skips the gate entirely. A declared-intent check, not a
  chemical-plausibility one. `kind` and `joint` are mutually exclusive on
  one connect — an atomic bond and a kinematic joint are different claims
  about the same pair; declare one. **Complementary roles** need one port per
  half, never two of the same: `azide` ↔ `alkyne` (gate on either half
  or on `CuAAC`), `donor` ↔ `acceptor`, `bump` ↔ `hole`, `+` ↔ `-`.
  Azide + azide is refused naming both ports' roles; any unlisted role
  is symmetric.
- `disconnect` — `a`, `b`
- `set_joint` — `a`, `b`, `joint` (req). Joint dict:
  `{"class": rigid|revolute|prismatic|cylindrical|screw|planar|ball|
  compliant|captive|axial, "axis"?: [x,y,z], "mechanism"?: snap|screw|
  press|key|magnet|bearing|bond|integral|cable, "params"?: {…}}` —
  nested, never flat.

## Ops — loads, prose, measures, modes, BOM, notes (exact parameter lists)

- `set_load` — exactly one of `block` | `a`+`b` (connect), then flat
  keys: `force` [N] · `torque` [N·m] · `duty` · `cycles` · `fixed`
  (true or subset of `["x","y","z"]`; blocks only) · `clear`. Flat —
  never nested under `objectives=`. Keys MERGE across calls (`fixed`
  then `force` keeps both; a repeated key overwrites); `clear: true` is
  the only reset. Same for the connect target — amend a connect's
  objectives in place, no disconnect/reconnect.
- `set_desc` — `block` + `desc` and/or `use` (null clears). Amends a
  block's prose after creation; on a template, not an instance.
- `add_measure` — `block`, `name` (req) · `value`/`min`/`max` · `unit`
  `m|count|ratio|deg` · `relation` `{source: "block.measure", scale,
  offset, tol}` · `strength` `hard|soft|gauge` (default gauge) ·
  `reason` · `origin`
- `set_measure` / `remove_measure` — `block`, `name` (+ at least one
  field for set; no explicit nulls — remove then re-add)
- `set_mode` — `block`, `mode` = `"family"` or `"family/material"` or
  null. Families: `purchase · fdm · sla · cnc-2.5ax · laser ·
  stock-cut · atomic` (e.g. `"fdm/asa"`).
- `set_binding` — `block` + (`kind` ∈ `cad|structure|component|part` +
  `design`) or `clear: true`
- `add_bom` / `remove_bom` — `block` | `a`+`b`, `item_kind`
  `component|part`, `item` (slug/C-number) · `qty` · `uom` · `reason`.
  Slugs are not vetted at write time — `view='bom'` reports dangling
  ones (`⚠ not in the store`).
- `add_note` — `name`, `kind` `question|answer|decision`, `text` ·
  `re` (note name) · `about` [anchors] · `origin`
- `formfind` — force-density form-finding over the axial subgraph:
  `q` per-member overrides · `q_tie`(+1)/`q_strut`(−1)/`q_rod`(+1) ·
  `move` (`'all'` | block list; default only `origin='proposed'` poses
  move — human-set poses are never overwritten).

## Discrete states + transitions (bistables, photoswitches, assembly steps)

One mechanism for anything a block can be in more than one of —
Howell-style compliant bistables/hard stops as much as photoswitches or
conformers. A block with no declared states has exactly one implicit
state; nothing about a plain block's shape changes.

- `declare_states` — `block` (req), `states` `[{'name', 'envelope'?,
  'port_pose_overrides'?, 'descr'?}]` (req; replaces the block's whole
  state set). `envelope` overrides the block's own in that state (omit =
  unchanged); `port_pose_overrides` is `{port: {'direction': [x,y,z]}}`
  (unit-normalised at write time) — the only pose-like field a port
  carries today. Ordinary blocks only (instances/arrays declare no
  states of their own — pose the template).
- `declare_transitions` — `block`, `transitions` `[{'from_state',
  'to_state', 'driver_kind', 'driver_ref'?, 'params'?}]` (req). DIRECTED
  edges — a ratchet's forward/reverse barriers are two rows, never one
  shared undirected edge. `driver_kind` is closed: `light | reaction |
  redox | ph | thermal | mechanical`.
- `set_current_state` — `block`, `state` (req). PERSISTENTLY poses a
  block into one of its declared states — the write-time counterpart of
  the transient `args={'state': ...}` read below.

**Posing a state to read it (transient).** `view='tree'|'block'|
'clearance'` additionally take `args={'state': {'<block>':
'<state name>'}}` — a one-read pose override, never written back (use
`set_current_state` to persist a choice). Several blocks can be posed at
once in the same `args.state` dict. An undeclared state name, an unknown
block, or an instance target (states live on the template) are all
rejected loudly, naming what IS available.

```python
edit(kind='se', id='switch1', ops=[{'op':'declare_states','block':'dye',
     'states':[{'name':'trans'},{'name':'cis','envelope':'sphere:r0.006'}]}])
get(kind='se', id='switch1', view='clearance',
    args={'a':'dye','b':'wall','state':{'dye':'cis'}})  # probe THIS state
get(kind='se', id='switch1', view='sweep')               # probe EVERY state
```

## Optical (FRET) ops — energy transfer as a comm channel

Use these when blocks talk to each other by **Förster resonance energy
transfer**: a donor dye hands its excitation to a nearby acceptor by
near-field dipole coupling. There is no waveguide — the channel is the
geometry — so the efficiency falls as `r⁻⁶` and is multiplied by an
orientation factor `κ²` computed from the two transition dipoles. Check
it with `view='fret'`.

- `set_chromophore` — `block` (req) + the whole card: `label` (the dye,
  e.g. `"Cy3"`) · `dipole` `[x,y,z]` **in the block frame** (the block's
  own pose rotates it into world space, so an instance of a template
  inherits the chemistry and gets its own orientation) · `quantum_yield`
  (0–1) · `lifetime_s` (donor excited-state lifetime, seconds) ·
  `emission` `[[nm, value], …]` (arbitrary units) · `absorption`
  `[[nm, M⁻¹cm⁻¹], …]`. All fields required — a partial card would still
  produce a number, from physics that isn't there. `clear: true` removes
  it. Lives on the template, not on instances.
- `set_optical_link` — `a`, `b` (each `'block.port'`, addressing an
  existing connect like `set_joint` does) + `min_efficiency` (req,
  strictly 0–1) · `channel` · `reason`. The L2 declaration: what this
  link **needs**, stored, never derived. Both endpoints must already
  carry a chromophore — a transfer requirement between blocks with no
  optics is a typo, not an unmet requirement. `min_efficiency: null`
  clears. Compatible with `joint`/`kind` on the same connect: an optical
  link is different physics on the same pair, not a competing claim.
- `set_optics` — the design's optical context: `medium_index` (req; every
  Förster radius in the design divides by the same `n⁴` under a sixth
  root, so this is a real input, not bookkeeping) · `excitation_nm` (the
  pump, enables the spectral-crosstalk figure) · `clear`. Undeclared is
  legal — the view then assumes ~1.4 and says so.

**Two traps worth knowing before you place anything.** (1) `κ² = 0` for
dipoles that are mutually perpendicular and both perpendicular to the
line between them: a geometrically perfect link that transfers nothing,
at any distance. The fix is rotating a block, not moving it. (2) A donor
is a **broadcast, not a wire** — every acceptor in range competes for the
same excitation, so `view='fret'` solves them together and a pair
efficiency read in isolation overstates the link.

## Atomic-mode ops (exact parameter lists)

- `declare_threading` / `remove_threading` — **atomic mode.** `a`, `b`
  (req): block `a` is threaded through block `b` (a macrocycle on an
  axle). Directional, per-pair, stored not derived from poses — the
  same rule `pcb` uses for its combinatorial embedding. Mutual
  threading (`a` through `b` *and* `b` through `a`) is rejected as
  physically impossible; fix a wrong-direction declaration with
  `remove_threading`, not by declaring the opposite pair on top.
- `declare_dof` / `clear_dof` — **atomic mode.** `block`, `kind`
  `rotational|translational`, `axis_ports` [exactly two of the block's
  **own** ports] (req). Records intent only — no torsion scan, no
  barrier estimate.
- `bind_structure` / `unbind_structure` — **atomic mode.** `block`,
  `design` (a `structure` slug), `ports` (`{port: atom_label}`) —
  every mapped port must exist on the block, the atom label must exist
  in the structure, and a port's `expected_element` must match the
  bound atom's element (a loud rejection at bind time). Binding again
  to the **same** design is incremental; binding to a **different**
  design first clears every port binding on the block. Both target an
  ordinary block only — bind via the template for an instance.
  `unbind_structure` clears a block's binding and every one of its
  ports'.
- `generate` — **atomic mode.** `generator` `cnt|fullerene|cone|
  cyclodextrin`, `params` (dict), `name` (new block) · `parent`/`pose`/
  `rot` passthrough. One op = a canonical block whose atoms follow from
  math, no LLM: mints a `structure` design at `<design>-<name>` holding
  the generated atoms, adds the block (envelope + ports + topology
  facts), and binds it — the echo names the minted slug. A `structure`
  design already living at the target slug is a loud rejection —
  `generate` never overwrites.

## The axial member (ties, struts, rods, spokes)

`class: "axial"` — a pin-ended two-force member between two blocks. No
`axis` (derived from endpoint geometry). Params, vetted:

- `tension_capacity` / `compression_capacity` [N ≥ 0] — the pair is the
  cross-scale contract. **tie** = compression 0 (cable, spoke);
  **strut** = tension 0; both > 0 = **rod**. (0, 0) is rejected.
- `preload` [N ≥ 0, tension-positive] · `free_length` [m > 0] · `rate`
  [N/m > 0]. Physically `preload = rate × (L_installed − L_free)` —
  declare all three so the builder has a tensioning instruction
  (consistency is NOT yet cross-checked; keep them honest yourself).
- Mechanism for a tensioned member is currently only `cable` (BOM
  demand: "the cable / wire rope") — there is no spoke/turnbuckle
  idiom yet; note the mismatch in a `add_note` if it matters.

## Views (`get(kind='se', id=…, view=…)`)

`tree · block · ports · topology · measures · validate · clearance · sweep ·
drc · bom · fasten · interview · freedom · stability · mechanics ·
literature · fret · links`. There is **no `mass` view** (mass goes via
`bom`). `interview`
elicits what's missing — lead with it. `mechanics`/`literature` are
atomic-mode-only (below); `topology` renders atomic mode's threading
pairs + declared dof together and is empty prose for a non-atomic
design.

`view='links'` renders the design's link graph both directions. Write
edges with the `link` verb: `link(kind='se', id='<slug>',
target='kind:identifier', rel=…)` — `rel='serves'` for the quest/todo
the design serves, default `related-to` for a sibling variant,
`rel='parent'` (with `target='folder:N'`) files it into a folder;
`mode='remove'` deletes the edge.

`view='stability'` (Maxwell/Calladine m−s on the axial subgraph):

- Model honesty (stated in its header): one pin node per block **at the
  block pose** (port offsets are discarded — cross-laced wheels are
  inexpressible today), axial members only; `rigid`/`revolute` connects
  are NOT in the equilibrium matrix, so rigid substructures must be
  modelled as axial trusses or accepted as unanalysed.
- Verdicts: `rigid` · `prestress-stabilized` · `first-order mobile —
  NOT stabilized …` · the not-checked tripwire line.
- Declared preloads are checked as a self-stress state (out-of-balance
  vs a preload-scaled tolerance); all-zero preloads report
  "no preload declared — nothing to verify", never a pass.
- A loaded block that is not in the analysed subgraph is currently NOT
  flagged — check block membership yourself (j of N blocks in header).

## Checking views — drc, fasten, clearance, sweep

`view='drc'`: capacity vs declared load ("asked to carry X N compression
against a Y N buckling/crush ceiling"), mechanism-implied BOM demands,
undeclared interpenetration, dof_disagreement, unconnected ports, and
**connect geometric plausibility** (gr337040/gr338426):
`connect_envelope_disjoint` (a declared connect whose envelopes never
touch — undeclared_interpenetration's mirror), `mechanism_no_interference`
(a `press`/`snap` mechanism with zero envelope overlap),
`captive_not_contained` (`captive` class with no overlap),
`screw_axis_no_overlap` (the `screw` *class* — not the `screw`
*mechanism*, which is `view='fasten'`'s job — with nothing overlapping
along its axis), and `axis_not_coaxial`/`axis_not_radially_contained`
(`bearing` mechanism or `revolute`/`cylindrical` class whose two
envelopes' own axes — circular envelopes only — aren't lined up or
nested). All warn tier; a bearing/press fit's declared interference is
expected and stays clean.
`view='fasten'` refuses a stack-up with no screw-form component bound. It
also decides **nothing** about what a printed member's far end threads into
— `params.thread_strategy` (`nut | nut-trap | insert | thread-forming |
tapped`) is contract-classed and required there, because a cut thread in
plastic is the wrong default; metal members still default to `tapped`. The
view reports the stack, the grip, the thread as a lead, which driver can
reach the head, and every stamped hole with the provenance of its diameter.
Full workflow: `precis-se-fasten-help`.

`view='clearance'`: with `args={'a': <block>, 'b': <block>}`, the signed
envelope gap between those two blocks (interference/touching/clear).
Omit `args` (or pass `{}`) for an all-pairs digest instead — every unique
block pair named by the design's CONNECTS, worst gap first, capped at 64
pairs; a block missing an effective envelope is skipped with a note
rather than failing the whole survey.

`view='sweep'`: "does anything collide in ANY declared state?" — the
cross product of every state-carrying block's declared states (no
`args`; a block needs 2+ declared states to enter the sweep at all).
Each combination reruns the same undeclared-interpenetration check
`view='validate'` uses; a design with no state-carrying blocks reads as
a clean "nothing to sweep", not an error. The combination count is
capped (64) — a sweep that hits the cap says so and names how many
combinations went unchecked, never truncates silently.

## Atomic mode — block trees over atoms (the merged `nm` kind)

`set_mode(block=…, mode='atomic')` marks a block's realization as
**chemistry** rather than a solid: `mode='atomic'` on a block whose
binding is anything other than a `structure` design (or a `structure`
binding on a block whose mode says otherwise) is a `mode_binding_mismatch`
`view='drc'` finding, never a write-time rejection — the house posture for
a design that can be mid-thought about its own realization. This is the
former `nm` kind (retired 2026-09, `docs/backlog/nm-se-merge.md`): the
same six-level block tree — nested envelopes/poses/ports/connects, L2
threading + declared dof, an L5 binding into a real `structure` design for
the filled chemistry — now authored through `kind='se'` with **no
separate units convention**: atomic-mode `envelope`/`pose`/`rot` are the
same bare-metres/bare-radians numbers every other `se` block uses (see
"Units and geometry conventions" above) — unlike the retired `nm` kind,
which required a unit suffix on every hand-authored envelope. The one
surviving Å boundary is internal to `generate` (below): its generators
compute in ångström and the crossing to metres happens once, before the
block ever reaches the tree.

A block is *designed*, not bought (unlike `component`) — the library
grows by composition, a sugar defined once and instanced seven times, the
way a software module tree does. `add_block`/`instance_block`/`array_block`
/`set_pose`/`add_port`/`connect` etc. are the same ops as every other `se`
block (see "Ops" above); atomic mode's own ops are `declare_threading`/
`remove_threading`, `declare_dof`/`clear_dof`, `bind_structure`/
`unbind_structure`, and `generate` (all documented above), plus `add_port`'s
`expected_element`/`expected_hybridization` and `connect`'s `kind='bond'|
'interaction'` capability gate.

### Generate — parametric block factories (deterministic fill)

```python
put(kind="se", id="tube1", text='''{"ops": [
    {"op": "generate", "generator": "cnt",
     "params": {"n": 10, "m": 10, "length_A": 40}, "name": "axle"},
    {"op": "generate", "generator": "fullerene", "params": {"atoms": 60},
     "name": "stopper"}
]}''')
```

`cnt` (chiral index `n ≥ m ≥ 0`, radius `a√(n²+nm+m²)/2π`; rim atoms
become `sp2-rim` ports), `fullerene` (`atoms: 60` only so far — truncated
icosahedron, 12 pentagons, Kekulé bond orders), `cone` (`pentagons: 1–5`
apex disclinations, opening angle `sin(θ/2) = 1 − P/6`; honest *open*
frustum — the apex is truncated at a derived floor, both rims get ports),
`cyclodextrin` (`variant: alpha|beta|gamma` — 6/7/8 glucose units, real
rotaxane macrocycles; seeded-rdkit conformer gated by an O4-ring diameter
check, `cd-primary-rim`/`cd-secondary-rim` ports, topology carries measured
`o4_ring_diameter_A` + derived `cavity_diameter_A` + `b1: 1`). Param
validation is theorem-loud (impossible chirality/size is rejected at op
time), an unknown generator lists the registered ones. Prefer a generator
over hand ops or LLM fill whenever the family has one. The `cyclodextrin`
torus envelope is **bore-preserving**, not fully containing: the hole is
pinned at the derived cavity radius so a threaded axle reads clear; rim
atoms folded toward the axis surface read as the warn-tier `envelope_fit`
finding instead of closing the bore.

## Atomic mode — views and scope limits

### `view='mechanics'` — advisory L4 ceilings, never a gate

Min-cut tensile (bond-graph max-flow × ~5 nN/bond), Euler buckling for
tube-shaped generated blocks, harmonic angle strain vs VSEPR ideal
angles — defect-free continuum estimates, never `validate` findings.
Unbound blocks read `unfilled`, cross-design connects read `not fused`,
and every number carries the pristine-lattice caveat.

### `view='literature'` — deterministic (no-LLM) paper search

```python
get(kind="se", id="rotax1", view="literature", args={"block": "hub"})  # one block
get(kind="se", id="rotax1", view="literature")                          # whole design
```

Builds a paper-search query from the target block's `name`/`desc`/`use`,
the design's own `description`, the objective vocabulary on any connect
touching the block (`objectives={'role': ...}`'s values), and — when the
block is already bound — its bound structure's element composition. Runs
the query in-process against the paper corpus and returns both the
generated query and the ranked hits. Naming no `block=` queries the whole
design instead.

## Atomic mode — `view='validate'`, the chemistry-tier findings

Atomic mode's findings (`precis_se.atomic.validate`) share `view='validate'`
with the block/structure-tier findings above (the header's filled-fraction
line counts atomic-mode blocks: `"N/M block(s) filled (bound to real
chemistry)"`, counting ordinary blocks only — an instance is filled
exactly when its template is):

| rule | severity | catches |
|---|---|---|
| `port_capability` | error | a stored connect violates its own endpoints' declared roles |
| `dangling_binding` | error | `bound_design` no longer resolves, or a `bound_atom` no longer exists in it |
| `binding_element_mismatch` | warn | a bound atom's element doesn't match the port's `expected_element` |
| `envelope_fit` | warn | a bound block's realized atoms protrude beyond its declared envelope + vdW margin — the L1↔L5 agreement has drifted. Or `cannot check — frames do not correspond` when the whole scene sits an envelope-width away (imported structure, no local-frame alignment): re-author the atoms near the envelope's origin (e.g. `from_smiles` `offset=`), do NOT widen |
| `connect_cycle` | warn | the connect graph closes a loop across the block tree (a macrocycle IS real chemistry — this names the path, never says "forbidden") |
| `bond_length_sanity` | warn | a `kind='bond'` connect's block-pose gap (ports have no stored position; see Scope below) is wildly beyond a plausible bond |
| `bond_vector_alignment` | warn | a `kind='bond'` connect's two ports' `direction` vectors are far from anti-parallel (>60° off 180°) |

`bind_structure` also runs an `envelope_fit` **preflight** on bind (never
blocking — the same check as the read-time finding above, one call
earlier) at a ~1.7 Å vdW margin via the same `cad` SDF kernel
`view='clearance'` uses.

The **graph-tier** atomic findings — `dangling_threading` (a threading
pair names a block that no longer exists), `threaded_without_envelope` (a
threading pair where either side has no envelope), `mode_binding_mismatch`
(mode↔binding contradiction, above), and `dof_disagreement` — live on
`view='drc'` instead, alongside the rest of `se`'s graph tier (joint
contradictions, mechanism-implied demands): the split is by tier
(per-block/atom vs whole-graph), same as every other `se` finding, not a
leftover from the merge.

`undeclared_interpenetration`/`bond_length_sanity`/`bond_vector_alignment`'s
thresholds are all fractions of the smaller involved block's own envelope
size (never an absolute figure — an atomic design spans sub-nm to
tens-of-nm blocks, so a fixed epsilon is scale-wrong at one end or the
other).

### Scope limits — stated plainly

- A block's clearance envelope is its own config only — no subtree union
  across children.
- **Propose exists; apply does not.** `se_propose_atomic`
  (`docs/backlog/se-atomic-round2.md`, the follow-on spec) proposes a fragment for
  ONE block and dry-runs it — candidate fragment, a `structure` op script,
  a port→atom map, DRC already run against a scratch scene. It never
  writes anything; turning an accepted proposal into a real design is
  still manual (run the ops yourself, then `bind_structure`).
- **No charge, optical, or simulation views.** No mechanism/dynamics
  analysis, no torsion scan, no rotational-barrier estimate —
  `declare_dof` records intent only.
- **Ports have no stored position of their own** — only their owning
  block's pose. `bond_length_sanity` approximates a bond's real length
  from the two blocks' pose-to-pose gap, so it can read long for a
  legitimate off-axis port even on an otherwise-correct design; it warns,
  never gates.

## Known sharp edges

- Ports are mandatory for `connect` but stability discards their
  geometry; invented port names are fine.
- A `component` for common hardware (bearings, spokes) may not exist —
  your first BOM will be dangling until minted; that's expected.
- Process/printability DRC (does `fdm` survive this load?) is unshipped:
  `set_mode` is intent, nothing checks it yet.
