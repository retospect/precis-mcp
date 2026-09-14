---
id: precis-se-help
title: precis — the se kind (structural/mechanical designs in metres)
summary: author a block-tree mechanical design (envelopes, ports, joints, axial members with preload, measures, BOM), then check it — validate/drc/clearance/stability/fasten/freedom/bom/interview; ATOMIC mode (the merged nm kind) designs chemistry: threading, dof, bind_structure, generate, view=mechanics/literature; put is a full REPLACE, edit ops= is the incremental path
answers:
  - how do I author an se structural design — blocks, connects, joints?
  - how do I model a cable / spoke / strut — a prestressed axial member?
  - how do I check whether my structure is rigid or a mechanism (stability)?
  - how do I declare loads, supports, measures, manufacturing mode, BOM?
  - what units does se use, and what do envelope w/d/h mean?
  - how do I design a molecular machine as nested blocks before filling in real chemistry?
  - how do I declare a port and connect two blocks with a capability gate?
  - how do I record that a macrocycle is threaded onto an axle?
  - how do I bind a block's ports to real atoms in a structure design?
  - how do I find literature for a block before filling it with real chemistry?
  - how do I check whether a bound block's atoms actually fit its declared envelope?
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
  chemical-plausibility one.
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

`tree · block · ports · topology · measures · validate · clearance · drc ·
bom · fasten · interview · freedom · stability · mechanics · literature ·
links`. There is **no `mass` view** (mass goes via `bom`). `interview`
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

`view='drc'`: capacity vs declared load ("asked to carry X N compression
against a Y N buckling/crush ceiling"), mechanism-implied BOM demands,
undeclared interpenetration, dof_disagreement, unconnected ports.
`view='fasten'` refuses a stack-up with no screw-form component bound.

`view='clearance'`: with `args={'a': <block>, 'b': <block>}`, the signed
envelope gap between those two blocks (interference/touching/clear).
Omit `args` (or pass `{}`) for an all-pairs digest instead — every unique
block pair named by the design's CONNECTS, worst gap first, capped at 64
pairs; a block missing an effective envelope is skipped with a note
rather than failing the whole survey.

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
| `envelope_fit` | warn | a bound block's realized atoms protrude beyond its declared envelope + vdW margin — the L1↔L5 agreement has drifted |
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
