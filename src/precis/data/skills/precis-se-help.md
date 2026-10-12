---
id: precis-se-help
family: se
title: precis — the se kind (structural/mechanical designs in metres)
summary: author a block-tree mechanical design (envelopes, ports, joints, axial members with preload, measures, BOM), then check it — validate/drc/clearance/stability/fasten/freedom/bom/interview; discrete states + transitions and the derived kinematics — see precis-se-states-help; FRET links as a comm channel — see precis-se-fret-help; ATOMIC mode designs chemistry over the block tree — see precis-se-atomic-help; put is a full REPLACE, edit ops= is the incremental path
answers:
  - how do I author an se structural design — blocks, connects, joints?
  - how do I model a cable / spoke / strut — a prestressed axial member?
  - how do I check whether my structure is rigid or a mechanism (stability)?
  - how do I declare loads, supports, measures, manufacturing mode, BOM?
  - what units does se use, and what do envelope w/d/h mean?
  - how do I declare a port and connect two blocks with a capability gate?
  - how do I search the library for a block matching several properties at once, like opto-deform + bistable + a click-chemistry port?
applies-to: get/search/put/edit/delete/link (kind='se')
status: active
tags: verbs, design
kinds: se
---

# precis-se-help — the call surface

S1 authored-surface read: `get(kind='se', id=..., view='surface_deviation',
args={'name':<bound block>, 'target':{'features':[{'name':'tube',
'centre_A':[0,0], 'r0_A':5, 'pieces':[['arc',1,-90],['line',3]]}]},
'z_offset_A':0})`. Reports region atom count/mean/p95/max in Å over stored
structure-local atoms using the existing S1 judge. Explicit target is caller-authored,
not proof of the original generation target; only its supplied rigid z shift is
removed. Omit target to use a matching exact generated target receipt, including
the recorded row-vector affine isometry (possibly reflected), actual selected R
and evaluated meridians. No fitted map, requested-radius substitution or planner
reconstruction. Legacy, missing, malformed or stale receipt returns unavailable
with explicit-target/new-generation guidance; old data is never rebuilt. Stored
mode allows only its recorded map: nonzero z_offset_A requires an explicit
target. Explicit null is invalid. SE pose is not applied. Explicit `features:[]` means
a sheet-only target; feature discs must be disjoint. A specific design id is
required. Receipt/identity/version/cell/live atom positions and row IDs use one
SQL statement snapshot; replacement rows invalidate the receipt even at the
same version with byte-identical positions. Missing snapshot or
unverifiable version returns unknown/retry without automatic retries.
Source SE slug/block UID and stable bound structure version are separate
provenance. No build/relax/write/job.

An `se` design is a **block tree** (blocks with poses + cad-DSL envelopes)
plus **connects** (port↔port edges carrying a joint class, objectives,
and optional preload). You author with typed ops, then read views that
check what you claimed. For the design *workflow* (abstraction ladder,
refine, tradeoffs) see `precis-se-design-help`; for screwing a design
together — picking a real ISO screw, what a printed part's threaded hole
should be, and what `view='fasten'` reports — see `precis-se-fasten-help`;
for printing a block — `realize`, build frame, process DRC, STL/3MF
export, `view='fab'`'s fabrication table — see `precis-se-print-help`;
for a block with more than one shape — declared states, transitions,
`view='kinematics'`, `view='sweep'` — see `precis-se-states-help`; for
FRET links between blocks — see `precis-se-fret-help`.

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

`put(kind='se', id=…, text=<json>)` **replaces the whole design**. Its
only top-level keys are `description`, `ops` and `scenario` (anything else
is rejected). An empty op list is refused while the design has blocks —
ops go in `text=` or `ops=`; to start over, `delete` the design first. `delete` also retires the
`structure` refs the design's blocks were bound to, except ones another
live design still binds (named in the reply as "stay live") and promoted
building blocks. Incremental changes go through
`edit(kind='se', id=…, ops=[…])`. Op batches are **atomic**: one bad op
rolls the whole batch back.

`scenario` names the **production context** that governs the design —
`prototype` (one off, lifetime physics OFF) · `small_batch` (100, indoor
five-year service) · `mass_production` (100 000, full lifetime physics).
It decides which checks are meaningful, so `view='validate'` and
`view='drc'` both print which scenario governed the run — or say
`scenario: none chosen` rather than assume a default. An unknown name is
rejected; an *absent* key keeps the earlier choice (design-level context,
not part of the block tree `put` replaces).

`view='drc'` always ends with a one-line `validate:` digest (error/warning
counts, first finding, or `validate: clean`), and a DRC-clean design with
validate warnings reads `no DRC findings; validate has N warning(s)` — a
clean DRC never implies a clean design.

**Every `put`/`edit` reply ends with that same digest line** (`validate: clean`,
or `validate: N error(s), M warning(s) — first: <rule> <detail>; see
view='validate'`), and the `/se/<slug>` page shows it above a collapsible
findings table — a write never refuses, but it no longer stays silent
about what it left behind. For bound structures, `atom_clash` fires per
block pair when posed atoms of two blocks come within 2 Å (error under
1 Å, warn in [1, 2) Å; count, minimum distance and the closest atom pair
are in the text). A `connect` between the pair does not excuse it:
coincident atoms are never intended chemistry — re-pose a block
(`set_pose`). Blocks generated at the default pose `[0,0,0]` pile up on
each other and trip it.

**Blocks are addressed by label or by uid.** A label (the block `name`)
is unique within a design. A block also carries a stable **uid** — `(uid
#41)` in `view='block'` — that survives edits and re-`put`s. **Anywhere an
op or a view takes an existing block**, `'#41'`/`'uid:41'` addresses it by
uid, including the block half of `'block.port'` (`connect a='#41.bore'`);
an ambiguous label's error lists every matching uid. The label is what
gets stored either way. (A block name may not be `'uid:…'` or contain `'#'`, `'/'`, `'@'`, `'['` or `']'` —
the last four delimit region selectors, `precis-se-regions-help`.)

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
  press|clamp|key|magnet|bearing|bond|integral|cable, "params"?: {…}}` —
  nested, never flat.

## Ops — loads, prose, measures

- `set_load` — exactly one of `block` | `a`+`b` (connect), then flat
  keys: `force` [N] · `torque` [N·m] · `duty` · `cycles` · `pinned` · `fixed`
  (true = `["x","y","z"]` or a subset of `x y z rx ry rz`; blocks only;
  pin vs clamp under `view='stability'`) · `clear`. Flat, never under
  `objectives=`. Keys MERGE across calls (a repeated key overwrites);
  `clear: true` is the only reset. Same on a connect target — amend in
  place, no disconnect/reconnect.
- `set_desc` — `block` + `desc` and/or `use` (null clears). Amends a
  block's prose after creation; on a template, not an instance.
- `add_measure` — `block`, `name` (req) · `value`/`min`/`max` · `unit`
  `m|count|ratio|deg` · `relation` `{source: "block.measure", scale,
  offset, tol}` · `strength` `hard|soft|gauge` (default gauge; `hard`
  on a measure with a toleranced relation makes its stack-up
  `tolerance_mismatch` an error instead of a warning — that is the one
  place hardness is consumed, so `hard` anywhere else draws
  `minimum_constraint`) · `reason` · `origin` `user|proposed|generated`
  (generators stamp their length anchors `generated`) · `datum` — the
  feature the measure is declared
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
- `measurand=` (charge, contact angle, field…), region selectors
  (`patch:`/`ring:`/`sites:`/`atoms:`) and `add_pocket`/`set_pocket`/
  `remove_pocket` + `view='pockets'` — see `precis-se-regions-help`.

## Ops — modes, binding, fabrication

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

## Ops — BOM, order, notes, formfind

- `add_bom` / `remove_bom` — `block` | `a`+`b`, `item_kind`
  `component|part`, `item` (slug/C-number) · `qty` · `uom` · `reason`.
  Slugs aren't vetted at write time; `view='bom'` flags dangling ones.

`view='order'` answers "what do I order": the instanced tree walked to
purchasable leaves (`bound_kind='component'`/`'part'`, quantities
multiplied through the arrays exactly like `view='bom'`), merged with any
explicit `add_bom` lines naming the same item (never double-counted), plus
a to-make table (`block · mode · qty`) for every unbound **leaf** block —
an unbound block with children is a plain assembly, so it gets no row; a
BOUND non-leaf instead gets one purchasable line saying `covers N
block(s)`, its children never separately ordered. A cross-design instance
(`template='<slug>#<block>'`) counts for the *borrowing* design at its own
local quantity, resolving the binding through the foreign design. The
honesty header mirrors `bom`'s:
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

## Discrete states, transitions and kinematics

See [[precis-se-states-help]].

A block that can be in more than one shape — a bistable, a photoswitch, an
assembly step — declares its states and the stimulus-labelled transitions
between them; `view='kinematics'` reads the swing a transition performs off
the two port frames, and `view='sweep'` checks every declared state at once.

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
+ conditions it came from. When a material property carries several
sourced rows, the pick is a band row (the spread summary) if exactly one
survives, else the newest — never a silent, unexplained newest — and the
row's provenance says which and lists the samples passed over; a dict
want `{'target'?, ..., 'conditions'?: {...}}` narrows to the row(s)
whose own `conditions` match every given key/value first (falling back,
and saying so, when nothing matches). An unrecognised key (not a spec,
not a property, not a built-in) still scores as a plain miss; the header
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
`n_max`/`m_max` (default 6/4) bound the enumeration (cap 2 000
compositions). Left unset, each switch/spacer derives its own from the
box's `delta`/`span` hi edge (clamped to 200) — the header names the
bound and fix when that still misses. `wants=` may ride along: its keys
score on the **switch** block; `q=` narrows designs. A `conditions` box
key (same shape as `wants[key]['conditions']`) filters every per-unit
fact read — e.g. pick the row logged at one wavelength over a newer
one.

(Scoring and ranking rules: the module docstring of
`precis/src/precis_se/compose.py`.) Facts are star-schema rows, never on
the block — five ordinary
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
the switch↔spacer port complementarity (`azide↔alkyne (CuAAC)`).

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
`stimulus` comes from `driver_kind`. Exactly one transition may carry
`requires=`; none → `declare_transitions … requires=`; several → add the
`/<from>-><to>` selector.

## Lever family — rotary unit + arms (compose swing=)

A third box key, `swing` (°, total rotary angle), adds a second family.
A **rotary unit** is a block with a non-zero `view='kinematics'` swing
(else a sourced `step_angle`); one with no usable envelope (no `arm₀`)
is skipped and counted in the header. With
`delta`, lever rows — one rotary unit + k arm units (ordinary spacers)
at its rotating port, tip stroke `2·(arm₀ + k·unit_length)·sin(angle/2)`
— rank in ONE merged list beside switch chains (`family: lever` /
`family: chain` on each row). With `swing` instead, `n` rotary units
chained (angles summed) rank alone (`family: series`). `delta` and
`swing` together is refused — one stroke measure. A lever row's `span`
is its arm reach (`arm₀ + k·unit_length`, nm), scored like a chain's and
labelled `(arm reach)`; a series row's `span` is its units' summed
`unit_length`. Each lever row shows the arm breakdown, the angle and its origin (`derived
from trans → cis on port p1`, or a sourced `step_angle` beside a
disagreeing derived one), the bistable/τ½ verdict, and the
rotating-port↔arm joining — `joining: none (…)` when no role is
complementary, and the ops script then omits that connect rather than
guessing a port.

## Optical (FRET) ops — energy transfer as a comm channel

See [[precis-se-fret-help]].

Blocks that talk by Förster resonance energy transfer: `set_chromophore` /
`set_optical_link` / `set_optics`, checked by `view='fret'`.

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
- Mechanism for a tensioned member: `cable` (BOM demand: "the cable /
  wire rope") or `tensioner` for a threaded rod tensioned by a
  nipple / turnbuckle (spoke, tie-rod; BOM demand names the rod and the
  tensioning part). `thread_pitch` [m > 0, default 0.0004536 = the
  56 TPI bicycle spoke thread of 2.0/2.2 mm spokes] sets the nipple's
  pitch; with `preload` and `rate` declared, `view='bom'` ends with an
  "Assembly — tensioning" line: tension to N = (preload / rate) /
  thread_pitch nipple turns. The pitch is a setting, not derived.

## Views (`get(kind='se', id=…, view=…)`)

`tree · block · report · ports · topology · chain · measures · revisions · datums · validate · clearance · sweep ·
drc · bom · order · fasten · interview · freedom · stability · mechanics ·
literature · fret · links · export · ops`. There is **no `mass` view** (mass goes via
`bom`). `interview`
elicits what's missing — lead with it. `mechanics`/`literature` are
atomic-mode-only (below); `topology` renders atomic mode's threading
pairs + declared dof together and is empty prose for a non-atomic
design.

`view='revisions'` lists every saved revision of the design — rev number,
UTC time, op names, block count. `edit(kind='se', id=…,
ops=[{'op':'restore_revision','rev':N}])` puts the design back to
revision N (a sole op in its call); the restore is itself recorded as a
new revision, so nothing is rewound. An unknown N names the available range.

The default tree shows each available stored build report's error count beside
its block. `view='validate'` includes stored ERROR/WARN findings by block, grouped
by error code and summarised for warnings; these are build-time evidence.

`view='report'` reads stored generate/join build findings (severity, code,
where, message, span) without recomputation. Omit args for every direct block,
or use `args={'block': '<label or #uid>'}`. Missing records are unavailable,
not evidence of a successful build; this does not validate current geometry
or expand template occurrences. State overrides are not accepted.

`view='ops'` is the only view that reads back into `put`: the design as a
replayable ops list in a fenced `{"ops": [...]}` block, so a design can be
reproduced in another database without a `pg_dump` or prod write access
(the use it was built for: checking a production design locally). It is
**final state, not history** — nothing removed ever appears, and no
destructive op is emitted. Its header lists what the round-trip does not
carry, and the two that bite are **block uids** (a re-`put` mints fresh
ones, so anything pointing at a block by uid must be re-resolved by label)
and **declared states/transitions** (they live in the shared
`design_states` tables keyed by uid, not on the tree).

`view='links'` renders the design's link graph both directions. Write
edges with the `link` verb: `link(kind='se', id='<slug>',
target='kind:identifier', rel=…)` — `rel='serves'` for the quest/todo
the design serves, default `related-to` for a sibling variant,
`rel='parent'` (with `target='folder:N'`) files it into a folder;
`mode='remove'` deletes the edge.

## Check rigidity — view='stability'

Maxwell/Calladine m−s on the axial subgraph:

- Model (stated in its header): axial members attach at **port world
  positions** (block pose + `add_port` pose); a block met at different
  points is a 6-DOF body (hub spokes on rim-offset ports carry axle
  torque), else a pin node. `rigid` connects merge blocks into ONE 6-DOF
  body (reference = first grounded member, else first by name), so a rim
  or frame needs no axial-truss stand-in; an axial inside one group is
  skipped. Other connects (`revolute` …) are NOT in the matrix.
- `fixed` pins each grounded block's point on its body: one foot grounds
  3 DOF, two 5, three non-collinear 6. Clamp: `x`+`y`+`z` on a body's ONLY
  grounded block grounds all 6; `["z"]` stays a pin; any of
  `rx`/`ry`/`rz` unclamps, grounding exactly those (axle
  bearing on a lone hub: `["x","y","z","rx","ry"]`); `pinned=true` keeps
  all three free (ball joint). Pin-node rotation unmodelled; `c` = grounded.
- Verdicts: `rigid` · `prestress-stabilized` · `first-order mobile —
  NOT stabilized …` · the not-checked tripwire line.
- Declared preloads are checked as a self-stress state (out-of-balance
  vs a preload-scaled tolerance); all-zero preloads read "no preload
  declared — nothing to verify", never a pass.
- A loaded block outside the analysed subgraph is NOT flagged — check
  membership yourself (j of N blocks in the header).

## Checking views — drc, fasten, clearance

`view='drc'`: capacity vs declared load ("X N compression against a Y N
buckling/crush ceiling"), mechanism-implied BOM demands, undeclared
interpenetration, dof_disagreement, unconnected ports, and **connect
plausibility** (all warn): `connect_envelope_disjoint` (envelopes never
touch), `mechanism_no_interference` (`press`/`snap`, zero overlap),
`captive_not_contained`, `screw_axis_no_overlap` (the `screw` *class*; the
mechanism is `view='fasten'`'s), `axis_not_coaxial`/
`axis_not_radially_contained` (`bearing`/`revolute`/`cylindrical` axes not
lined up or nested). A declared bearing/press interference stays clean.
**Joining precedent** (a `bonded` transition's `driver_ref` names the
reaction): a known joining (`CuAAC`, ...) with no `reaction` transition is
`joining_unnamed` (info); a named rxn without `reaction_class` is
`joining_class_unknown` (warn); a class with no yield rows is
`joining_unprecedented` (warn), with rows `joining_precedent` (info) — the
read is `search(kind='rxn', property='yield', reaction_class=...)`. The
header's counts ignore `info` rows.
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

**Finding ids and deltas.** `view='validate'`, `'drc'` and the
`'clearance'` digest carry a 12-hex `id` per row: the `pcb` DRC hash over
`(rule, sorted participants)` (`precis-pcb-help`), so `a—b` and `b—a` match
and detail, depth or row order never change it. `se` stores nothing: pass
an earlier report's ids as `args={'previous': [...]}` (list or comma
string) and the head reads `vs previous: N new, M still, K gone`, rows gain
a `delta` column (`new`/`still`) and `## gone since previous` lists the
vanished ids. A malformed id is refused. Rows sharing rule and subject
share an id (diffed as a multiset).

`view='sweep'` — every declared state at once: `precis-se-states-help`.

## Build a block tree over atoms (atomic mode)

See [[precis-se-atomic-help]].

Atomic mode applies when a block's realization is chemistry rather than a
solid — nested envelopes down to bond-level threading, degrees of freedom,
and a binding into a real `structure` design.

## Nucleic-acid chains (DNA/RNA design)

See [[precis-se-chain-help]].

Seven pure ops over ordinary blocks: `declare_helix` (geometry — centre
line, motif, unit count) · `declare_strand` (route chemistry — sequence
optional) · `add_domain`/`set_domain`/`remove_domain` (a strand's ordered
route along a helix; `set_domain` edits one row in place, `remove_domain` is
destructive) · `clear_chain` (un-declare,
cascading) · `layout_chain` (materialise `<helix>.s<k>` segment children —
what the 3D viewer draws). Pairing is DERIVED from two strands occupying
one helix offset running opposite ways, never declared. Three
handler-level ops, all proposals and never auto-applied: `relax_chain`
(settle those segments — hinges, loop springs, excluded volume; poses
back as `origin='proposed'`, each placed loop's curve onto its domain
row), `fold_layout` (a ViennaRNA MFE fold as helix/strand/domain records;
needs the optional `[chain]` extra) and `realize_chain` (Arnott B-DNA
fibre atoms for one segment's region, minted and bound as a `structure`
design). `view='chain'` reads helices/strands/pairing; `view='topology'`
gains the domain rows; `view='export'` writes the design to
scadnano/caDNAno/oxDNA/PDB.

## See also

- [[precis-se-atomic-help]] — atomic-mode block trees over real chemistry
- [[precis-se-chain-help]] — nucleic-acid helices, strands, domains, pairing
- [[precis-se-states-help]] — discrete states, transitions, the derived swing, view='sweep'
- [[precis-se-fret-help]] — FRET links as a comm channel: chromophores, optical links, view='fret'
- [[precis-se-flatpack-help]] — flat-pack boxes with shelves from one sheet: finger joints from t, nesting, laser SVG / CNC DXF
