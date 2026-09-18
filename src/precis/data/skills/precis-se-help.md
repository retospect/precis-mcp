---
id: precis-se-help
title: precis — the se kind (structural/mechanical designs in metres)
summary: author a block-tree mechanical design (envelopes, ports, joints, axial members with preload, measures, BOM), then check it — validate/drc/clearance/stability/fasten/freedom/bom/interview; discrete block states + stimulus-labelled transitions (declare_states/declare_transitions/set_current_state, args={'state':...} to pose transiently, view=sweep to check every declared state at once); OPTICAL domain: FRET links as a comm channel (set_chromophore/set_optical_link/set_optics, view=fret); ATOMIC mode designs chemistry over the block tree — see precis-se-atomic-help; put is a full REPLACE, edit ops= is the incremental path
answers:
  - how do I author an se structural design — blocks, connects, joints?
  - how do I model a cable / spoke / strut — a prestressed axial member?
  - how do I declare a bistable/photoswitch block's two states, {loaded,bonded} or {trans,cis}?
  - how do I check a block for clash in a specific state, or across every declared state?
  - how do I check whether my structure is rigid or a mechanism (stability)?
  - how do I declare loads, supports, measures, manufacturing mode, BOM?
  - what units does se use, and what do envelope w/d/h mean?
  - how do I declare a port and connect two blocks with a capability gate?
  - how do I model FRET between two blocks — energy transfer as a communication channel?
  - why is my FRET link dead even though the blocks are close enough?
  - how do I declare a required transfer efficiency and check the geometry against it?
  - how do I search the library for a block matching several properties at once, like opto-deform + bistable + a click-chemistry port?
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
should be, and what `view='fasten'` reports — see `precis-se-fasten-help`;
for printing a block — `realize`, build frame, process DRC, STL/3MF
export, `view='fab'`'s fabrication table — see `precis-se-print-help`.

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
  `z=0`** (spans z=0..h, like `cyl`) — verified against the kernel;
  not half-extents.
- `cyl` has its **base at the pose** (not centred); `sphere` is centred.
- `rot` is a bare **radians** vector (Euler, composed `Rz@Ry@Rx`) — reads
  render degrees, but you author radians. `90°` about z is
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
rejected; an *absent* key keeps the earlier choice (design-level context,
not part of the block tree `put` replaces).

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
written by name. (A block name may not be `'uid:…'` or contain `'#'`.)

## Ops — blocks and ports

- `add_block` — `name` (req) · `parent` · `pose` [x,y,z] m (bare) · `rot`
  [x,y,z] rad (bare, Euler `Rz@Ry@Rx`; see "Units") · `envelope`
  (DSL string) · `desc` · `use`
- `instance_block` — `name`, `template` (req) · `parent`/`pose`/`rot`.
  Rejects envelope/desc/use (they live on the template).
- `array_block` — `name`, `template` (req) + exactly one of
  `linear` `{count≥2, pitch>0, axis?}` | `polar` `{count≥2, radius≥0,
  axis?}`. Blocks only; N spokes = N `connect` ops.
- `set_pose` — `block` (req) + `pose` and/or `rot`
- `set_envelope` — `block`, `envelope` (req; null clears) · `origin`
  `'user'|'proposed'`. Rejects instances/arrays.
- `remove_block` — `block`
- `add_port` — `block`, `name` (req; no dots) · `roles` [str] ·
  `direction` [x,y,z] (normalised) · `pose` [x,y,z] · `rot` [rx,ry,rz]
  (block-local, m/rad; `rot` needs `pose`) · `annotations` dict · **atomic
  mode:** `expected_element`/`expected_hybridization` (checked
  by `bind_structure`). `roles` is a
  capability set the caller asserts, never checked against real
  chemistry (see `connect`). `annotations: {"external": true}` = intentionally
  unconnected (antenna); `validate`'s `unconnected_port` downgrades it
  to `info`.
- `remove_port` — `block`, `name`

## Ops — connect, disconnect, joints

- `connect` — `a`, `b` (req, `"block.port"` — **ports must already
  exist**; connect never auto-creates) · `joint` dict · `objectives`
  flat dict (`force`/`torque`/`duty`/`cycles`) · **atomic mode:**
  `kind` `bond|interaction` (default `bond`) — a `bond` connect
  requires **both** ports to afford `'covalent'`, or, with
  `objectives={'role': ...}`, both to afford that named role instead
  (rejection names the actual roles); `interaction`
  (non-bonded) skips the gate entirely. Declared intent, not chemical
  plausibility. `kind` and `joint` are mutually exclusive — a
  bond and a kinematic joint are different claims; declare one. **Complementary roles** need one port per
  half, never two of the same: `azide` ↔ `alkyne` (gate on either half
  or on `CuAAC`), `donor` ↔ `acceptor`, `bump` ↔ `hole`, `+` ↔ `-`.
  Azide + azide is refused naming both roles; unlisted roles are
  symmetric.
- `disconnect` — `a`, `b`
- `set_joint` — `a`, `b`, `joint` (req). Joint dict:
  `{"class": rigid|revolute|prismatic|cylindrical|screw|planar|ball|
  compliant|captive|axial, "axis"?: [x,y,z], "mechanism"?: snap|screw|
  press|key|magnet|bearing|bond|integral|cable, "params"?: {…}}` —
  nested, never flat.

## Ops — loads, prose, measures, modes, fabrication, BOM, notes, formfind

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
  `reason` · `origin` · `datum` — the feature the measure is declared
  against: `frame` (default; prismatic → the three pose-frame faces,
  rotational → axis + base face) · `port:<name>` · `face:<block>.<tag>`
  · `axis:<block>`. Resolved through the block's cad primitive at read
  time — a feature, never an optimiser DOF; see `view='datums'`.
  Relation key `feature: "face:<block>.<tag>"` (same selector grammar)
  anchors the measure to a
  geometric feature instead of chaining declared values. The derived
  number's `mismatch` note is band-first: outside `[min_value,max_value]`
  when declared, else beyond `tol` of `value`, else not exactly `value`.
- `set_measure` / `remove_measure` — `block`, `name` (set needs ≥1
  field; no explicit nulls — remove then re-add)
- `set_mode` — `block`, `mode` = `"family"` or `"family/material"` or
  null. Families: `purchase · fdm · sla · cnc-2.5ax · laser ·
  stock-cut · atomic` (e.g. `"fdm/asa"`).
- `set_binding` — `block` + (`kind` ∈ `cad|structure|component|part` +
  `design`) or `clear: true`
- `realize` — `block`, `mode` (req): mints the block's first
  implementation (a `cad` design from its envelope, bound + moded) ·
  `set_process_override`/`clear_process_override`
  — `block`, `field` (+ `value`) · `set_build_frame` — `block`, `down`
  [x,y,z] / `clear_build_frame` — `block`. Contracts: `precis-se-print-help`.
- `add_bom` / `remove_bom` — `block` | `a`+`b`, `item_kind`
  `component|part`, `item` (slug/C-number) · `qty` · `uom` · `reason`.
  Slugs aren't vetted at write time; `view='bom'` flags dangling ones.

`view='order'` answers "what do I order": the instanced tree walked to
purchasable leaves (`bound_kind='component'`/`'part'`, quantities
multiplied through the arrays exactly like `view='bom'`), merged with any
explicit `add_bom` lines naming the same item (never double-counted), plus
a to-make table (`block · mode · qty`) for every unbound **leaf** block —
an unbound block with children is a plain assembly of the things below
it, not itself a thing to buy or make, so it gets no row; only a BOUND
non-leaf gets the opposite treatment: one purchasable line saying `covers
N block(s)`, with its children never separately ordered or listed. A
cross-design instance (`template='<slug>#<block>'`) counts for the
*borrowing* design at its own local quantity, resolving the binding
through the foreign design. The honesty header mirrors `bom`'s:
`purchasable: P of L leaf template(s) · to make: M` — `P`/`M`/`L` count
TEMPLATES (a merged line can carry several), while the `priced`/`massed`
lines below count purchasable LINES — and a `total: ≥ … (partial, N of
P)` line whenever not every purchasable line's price AND quantity both
resolved; `part` lines never price (no store record exists for a
C-number).
- `add_note` — `name`, `kind` `question|answer|decision`, `text` ·
  `re` · `about` [anchors] · `origin`
- `formfind` — force-density form-finding over the axial subgraph:
  `q` per-member overrides · `q_tie`(+1)/`q_strut`(−1)/`q_rod`(+1) ·
  `move` (`'all'` | block list; default: only `origin='proposed'`
  poses move).

## Discrete states + transitions (bistables, photoswitches, assembly steps)

One mechanism for anything a block can be in more than one of —
Howell-style compliant bistables/hard stops as much as photoswitches or
conformers. A block with no declared states has exactly one implicit
state; nothing about a plain block's shape changes.

- `declare_states` — `block` (req), `states` `[{'name', 'envelope'?,
  'port_pose_overrides'?, 'descr'?}]` (req; replaces the block's whole
  state set). `envelope` overrides the block's own in that state (omit =
  unchanged); `port_pose_overrides` is `{port: {'direction'?: [x,y,z],
  'pose'?: [dx,dy,dz], 'rot'?: [rx,ry,rz]}}` (≥1 key, no others) —
  `direction` replaces outright (unit-normalised at write time),
  `pose`/`rot` are a per-state rigid **delta** in the block frame (added
  to / composed on the port's own pose), applied ONLY to a port that
  carries a pose; on a pose-less port the override is stored and shown
  but changes nothing. Ordinary blocks only (instances/arrays declare no
  states of their own — pose the template).
- `set_port_pose` — `block`, `name` (req) + `pose` and/or `rot`, or
  `clear: true`. Fills/rewrites a port's OWN placement after `add_port`
  (`set_pose` one level down). The slot is nullable on purpose — with no
  pose, geometry checks fall back to the block-pose + envelope-extent
  approximation and say so; with one on both ends, `bond_length_sanity`
  reports the exact port-to-port distance instead. This op and `add_port`
  write `pose_source='declared'` (intent); `bind_structure` measures
  `'bound'` off the realization and never overwrites a declared target —
  `precis-se-atomic-help`.
- `declare_transitions` — `block`, `transitions` `[{'from_state',
  'to_state', 'driver_kind', 'driver_ref'?, 'params'?, 'requires'?}]`
  (req). DIRECTED edges — a ratchet's forward/reverse barriers are two
  rows, never one shared undirected edge. `driver_kind` is closed:
  `light | reaction | redox | ph | thermal | mechanical`. `requires`
  (e.g. `{'delta': [10, 12], 'span': [40, 50], 'bistable': True}`) is a
  DECLARED target — the box `compose='<design>#<block>'` reads back —
  distinct from `params`, the realization's own measured numbers;
  `stimulus` is refused there (it's `driver_kind`, read automatically). A
  `reaction` `driver_ref` must be an existing rxn slug
  (`put(kind='rxn', id=..., rxn_smiles=...)` first) — it fails the whole
  edit otherwise, and renders as `rxn:<slug>`.
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

## Ranked library search — search(kind='se', wants=…)

`search(kind='se', wants={...})` ranks every non-instance block in the
whole library against a per-attribute wishlist — **never a strict
filter**: results are always ranked, every row shows each attribute's
✓/✗ and its actual value, and an empty result is impossible unless the
library itself has no blocks. `q=` is then optional — with it, the card
search narrows which *designs* are considered; a narrow to zero designs
falls back to the whole library and says so in the header.

Each `wants` value is one of three shapes: a bare scalar (`'light'`,
`True`, `1.0`) is a *target* — numeric targets match within 10% relative
tolerance, str/bool match exactly; a `[lo, hi]` list is an *interval* —
matches when the value's band overlaps it; a dict
`{'target'?, 'min'?, 'max'?, 'tol'?, 'weight'?}` (weight default 1.0,
tol default 0.1) spells out both the comparison and its human-set
importance — an agent never tunes its own weight.

Three keys are built in, read off the block/tree itself: `stimulus`
(matches if the wanted value is among the block's transitions'
`driver_kind`s); `bistable` (True iff ≥2 declared states AND no
`thermal` transition — a thermal path is a T-type reverse); `joining`
(matches a port `roles` entry, or either half of a named chemistry —
`'CuAAC'` matches an `azide` or `alkyne` port). Every other key is a
**star-schema lookup**, never a fact stored on the block: a
`component`-bound block's own spec values, then that component's
`made-of` material's property values, then the design's own `made-of`
material (optionally scoped to one block via the link's
`meta.block`) — first hit wins, and the row names the entity + source
+ conditions it came from. An unrecognised key (not a spec, not a
property, not a built-in) still scores as a plain miss; the header
notes it once, never a refusal.

```python
search(kind='se', wants={'stimulus': 'light', 'bistable': True,
       'delta_length': 1.0, 'joining': 'CuAAC'})
# 3 library block(s) ranked for wants={...}
# 1. switch1#dye  3/4  ✓stimulus: light  ✓joining: azide (CuAAC)
#    ✗bistable: T-type (thermal reverse)  ✓delta_length: 1 nm (component:azo1)
```

The winning row instances directly: `edit(kind='se', id=<yours>,
ops=[{'op':'instance_block','name':'<new>','template':'switch1#dye'}])`
— `template` takes the qualified `<design>#<block>` spelling cross-design
instancing already supports (slice 1).

## Composition proposer — search(kind='se', compose=…)

`search(kind='se', compose={'delta': [10, 12], 'span': [40, 50]})`
enumerates **n switches in series + m spacers** over the ranked-search
library and scores each composition against the box exactly like a
`wants=` row — never a strict filter, never empty while one switch
exists (the nearest misses show with their distances). `delta` (Å,
port-to-port stroke) and `span` (nm, long-state length) take the same
scalar / `[lo, hi]` / dict shapes as `wants`; at least one is required.
`n_max` (default 6) and `m_max` (default 4) bound the enumeration (cap
2 000 compositions, said in the header). `wants=` may ride along: its
keys score on the **switch** block; `q=` narrows designs as before.

Facts are star-schema rows, never on the block — five ordinary
`material`/`component` properties (an unknown one mints `proposed`-tier
on first `put(kind='material', …, property=…)`, no migration):
`delta_length` (Å; a block with a row is a *switch*), `unit_length`
(nm; with no `delta_length` the block is a *spacer*),
`pss_short_fraction` (0–1, photostationary conversion; conditions carry
the wavelength), `thermal_half_life` (s), `persistence_length` (nm).
Blocks with neither length row are skipped and counted in the header.

Every row surfaces what a stroke estimate must not hide: the PSS-scaled
Δ beside the ideal one (`Δ 10.2 Å (8.16 Å at PSS 80 % short)`, or
`PSS unknown`), the bistability verdict with τ½ (`bistable ✗ (T-type
(thermal reverse), τ½ 2 d)`), `floppy: span 23 nm > Lp 15 nm (rod#u)`
when the span exceeds the spacer's (or, without one, the switch's)
persistence length — `stiffness unknown` when there is no row — and
the switch↔spacer port complementarity from the slice 3 halves
(`azide↔alkyne (CuAAC)`).

```python
search(kind='se', compose={'delta': [8, 9], 'span': [20, 30]},
       wants={'stimulus': 'light'})
# 1. 3 × azo#u + 2 × rod#u  3/3  ✓delta: 10.2 Å (8.16 Å at PSS 80 % short)
#    ✓span: 23 nm  ✓stimulus: light  · bistable ✗ (…, τ½ 2 d)
#    · floppy: span 23 nm > Lp 15 nm (rod#u) · joining: azide↔alkyne (CuAAC)
```

The Next line is the top row's ops script: `instance_block` × n +
spacers, alternating, joined by `connect` through the complementary
ports — paste it into `edit(kind='se', id=<yours>, ops=[…])` and run
DRC on the composed tree. `compose='<design>#<block>'` (or
`'<design>#<block>/<from>-><to>'`) reads the box off that block's own
declared transition `requires=` instead of a literal dict — its
`stimulus` comes from `driver_kind`. Exactly one of the block's
transitions may carry a `requires=` box; with none, declare one
(`declare_transitions … requires=`); with several, add the
`/<from>-><to>` selector to pick one.

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

`tree · block · ports · topology · measures · datums · validate · clearance · sweep ·
drc · bom · order · fasten · interview · freedom · stability · mechanics ·
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
expected and stays clean. **Joining precedent** (a `bonded`-state
transition's `driver_ref` names the reaction, blocktree slice 5): a
connect whose ports declare a known joining (`CuAAC`, ...) but no
`reaction` transition names an rxn is `joining_unnamed` (info); a named
rxn with no `reaction_class` is `joining_class_unknown` (warn); a
`reaction_class` with zero recorded yield rows is `joining_unprecedented`
(warn); one or more yield rows is `joining_precedent` (info, with the
row/rxn counts) — `search(kind='rxn', property='yield',
reaction_class=...)` is the underlying read. The header's error/warning
counts ignore `info` rows.
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

## Build a block tree over atoms (atomic mode)

See [[precis-se-atomic-help]].

Atomic mode applies when a block's realization is chemistry rather than a
solid — nested envelopes down to bond-level threading, degrees of freedom,
and a binding into a real `structure` design.

## See also

- [[precis-se-atomic-help]] — atomic-mode block trees over real chemistry

