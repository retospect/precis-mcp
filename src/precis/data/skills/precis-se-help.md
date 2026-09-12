---
id: precis-se-help
title: precis — the se kind (structural/mechanical designs in metres)
summary: author a block-tree mechanical design (envelopes, ports, joints, axial members with preload, measures, BOM), then check it — validate/drc/clearance/stability/fasten/freedom/bom/interview; put is a full REPLACE, edit ops= is the incremental path
answers:
  - how do I author an se structural design — blocks, connects, joints?
  - how do I model a cable / spoke / strut — a prestressed axial member?
  - how do I check whether my structure is rigid or a mechanism (stability)?
  - how do I declare loads, supports, measures, manufacturing mode, BOM?
  - what units does se use, and what do envelope w/d/h mean?
applies-to: get/search/put/edit/delete (kind='se')
status: active
---

# precis-se-help — the call surface

An `se` design is a **block tree** (blocks with poses + cad-DSL envelopes)
plus **connects** (port↔port edges carrying a joint class, objectives,
and optional preload). You author with typed ops, then read views that
check what you claimed. For the design *workflow* (abstraction ladder,
refine, tradeoffs) see `precis-se-design-help`.

## Units and geometry conventions — read first

- **Everything is metres, newtons, radians.** `envelope`/`pose`/`rot` are
  bare numbers, **not** run through the units-policy-cutover's
  unit-required ingest boundary (that boundary is for `cad`'s own
  hand-authored text; `se` parses/stores its envelope DSL and pose/rot
  vectors in the pre-existing bare-SI convention — see
  `units-policy-cutover.md`'s decisions log). `cyl:r0.02h0.01` is a
  2 cm × 1 cm cylinder — the cad kernel is unit-agnostic; `se` stores
  metres.
- **⚠ Envelope `box` `w`/`d`/`h` are HALF-extents**: `box:w0.028d0.240
  h0.020` is a 56 × 480 × 40 mm block. Measured, not documented
  elsewhere; mis-authoring by 2× is the most common corpus error.
- `cyl` has its **base at the pose** (not centred); `sphere` is centred.
- `rot` is a bare **radians** vector (Euler, composed `Rz@Ry@Rx`) — reads
  render it back in degrees (the shared neat formatter), but the op-level
  value you author is radians, not degrees. `90°` about z is
  `rot: [0, 0, 1.5707963267948966]` (`math.pi / 2`), not `[0, 0, 90]`.
- `pose` is the block origin in the parent frame, bare **metres**.

## put vs edit — put is a full REPLACE

`put(kind='se', id=…, text=<json>)` **replaces the whole design**. The
payload's only top-level keys are `description` and `ops` (anything else
is rejected — an unrecognised shape used to silently empty the design).
Incremental changes go through `edit(kind='se', id=…, ops=[…])`. Op
batches are **atomic**: one bad op rolls the whole batch back.

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
  `direction` [x,y,z] (normalised) · `annotations` dict
- `remove_port` — `block`, `name`
- `connect` — `a`, `b` (req, `"block.port"` — **ports must already
  exist**; connect never auto-creates) · `joint` dict · `objectives`
  flat dict (`force`/`torque`/`duty`/`cycles`)
- `disconnect` — `a`, `b`
- `set_joint` — `a`, `b`, `joint` (req). Joint dict:
  `{"class": rigid|revolute|prismatic|cylindrical|screw|planar|ball|
  compliant|captive|axial, "axis"?: [x,y,z], "mechanism"?: snap|screw|
  press|key|magnet|bearing|bond|integral|cable, "params"?: {…}}` —
  nested, never flat.
- `set_load` — exactly one of `block` | `a`+`b` (connect), then flat
  keys: `force` [N] · `torque` [N·m] · `duty` · `cycles` · `fixed`
  (true or subset of `["x","y","z"]`; blocks only) · `clear`. Flat —
  never nested under `objectives=`.
- `add_measure` — `block`, `name` (req) · `value`/`min`/`max` · `unit`
  `m|count|ratio|deg` · `relation` `{source: "block.measure", scale,
  offset, tol}` · `strength` `hard|soft|gauge` (default gauge) ·
  `reason` · `origin`
- `set_measure` / `remove_measure` — `block`, `name` (+ at least one
  field for set; no explicit nulls — remove then re-add)
- `set_mode` — `block`, `mode` = `"family"` or `"family/material"` or
  null. Families: `purchase · fdm · sla · cnc-2.5ax · laser ·
  stock-cut · atomic` (e.g. `"fdm/asa"`).
- `set_binding` — `block` + (`kind` ∈ `cad|nm|component|part` +
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

`tree · block · ports · measures · validate · clearance · drc · bom ·
fasten · interview · freedom · stability`. There is **no `mass` view**
(mass goes via `bom`). `interview` elicits what's missing — lead with it.

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

`view='drc'`: capacity vs declared load ("asked to carry X N compression
against a Y N buckling/crush ceiling"), mechanism-implied BOM demands,
undeclared interpenetration, dof_disagreement, unconnected ports.
`view='fasten'` refuses a stack-up with no screw-form component bound.

## Known sharp edges

- Ports are mandatory for `connect` but stability discards their
  geometry; invented port names are fine.
- A `component` for common hardware (bearings, spokes) may not exist —
  your first BOM will be dangling until minted; that's expected.
- Process/printability DRC (does `fdm` survive this load?) is unshipped:
  `set_mode` is intent, nothing checks it yet.
