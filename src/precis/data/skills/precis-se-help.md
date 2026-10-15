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

- **`pose`/`rot` input carries units** (storage stays m/N/rad). A bare
  nonzero vector is rejected with a hint echoing it under plausible units.
  Canonical: `"pose": "[3 0 0] nm"`, `"rot": "[0 0 90] deg"`. Also:
  per-component (`["3nm", "0nm", "2Å"]`) or `"pose_unit": "km"` /
  `"rot_unit": "deg"` beside a numeric vector. Bare `0` is legal,
  e-notation fine; mixing united and bare-nonzero components rejects.
  `envelope` DSL: unchanged (`cyl:r0.02h0.01`, metres).
- **Writes acknowledge**: quantity-ingesting ops answer with
  `understood: pose = [3 0 0] nm` lines — the canonical reading of what
  was stored. Reads show units on every pose/rot; a homogeneous table
  column carries one unit in its header (`gap (nm)`) with bare cells.
- **Answers come back in your units** (respond-in-kind), and
  `args={'prefunits': 'nm,deg'}` on a `get` forces a display unit —
  per call only. `prefunits` in a `put`/`edit` payload makes it the
  design's sticky preference (stored `meta.prefunits`); per-call wins
  over sticky, a unit you just typed wins over both. Display only —
  storage is SI either way.
- **Never convert units yourself** — state your unit; `calc` converts.
- **Envelope `box` `w`/`d`/`h` are FULL dimensions**: `box:w0.028d0.240
  h0.020` is a 28 × 240 × 20 mm block — centred in x/y, **base at
  `z=0`** (spans z=0..h, like `cyl`) — verified against the kernel;
  not half-extents.
- `cyl` has its **base at the pose** (not centred); `sphere` is centred.
- `rot` is an Euler vector (composed `Rz@Ry@Rx`), written with `deg` or
  `rad`: `90°` about z is `"rot": "[0 0 90] deg"`.
- `pose` is the block origin in the parent frame, e.g. `"[0 0 4] mm"`.

## put vs edit — put is a full REPLACE

`put(kind='se', id=…, text=<json>)` **replaces the whole design**. Its
only top-level keys are `description`, `ops`, `scenario` and `prefunits` (anything else
is rejected). An empty op list is refused while the design has blocks —
ops go in `text=` or `ops=`; to start over, `delete` the design first. `delete` also retires the
`structure` refs it bound, except ones another live design still binds
(named "stay live") or promoted building blocks. Incremental changes go through
`edit(kind='se', id=…, ops=[…])`. Op batches are **atomic**: one bad op
rolls the whole batch back.

`scenario` names the **production context** that governs the design —
`prototype` (one off, lifetime physics OFF) · `small_batch` (100, indoor
five-year service) · `mass_production` (100 000, full lifetime physics).
It decides which checks are meaningful, so `view='validate'` and
`view='drc'` print the governing scenario — or `scenario: none chosen`.
An unknown name is rejected; an *absent* key keeps the earlier choice.

`view='drc'` ends with a one-line `validate:` digest (error/warning
counts, first finding, or `validate: clean`), and a DRC-clean design with
validate warnings reads `no DRC findings; validate has N warning(s)` — a
clean DRC never implies a clean design.

**Every `put`/`edit` reply ends with the digest** (`validate: clean`,
or `validate: N error(s), M warning(s) — first: <rule> <detail>; see
view='validate'`), and `/se/<slug>` shows it above the findings table —
a write never refuses, but it no longer stays silent. For bound
structures, `atom_clash` fires per block pair when posed atoms come
within 2 Å (error under 1 Å, warn in [1, 2) Å — count, min distance,
closest pair in the text). A `connect` does not excuse it —
re-pose a block (`set_pose`). Blocks generated at the default pose
`[0,0,0]` pile up on each other and trip it.

**Blocks are addressed by label or by uid.** A label (the block `name`)
is unique within a design. A block also carries a stable **uid** — `(uid
#41)` in `view='block'` — that survives edits and re-`put`s. **Anywhere an
op or a view takes an existing block**, `'#41'`/`'uid:41'` addresses it by
uid, including the block half of `'block.port'` (`connect a='#41.bore'`);
an ambiguous label's error lists every matching uid. The label is what
gets stored either way. (A block name may not be `'uid:…'` or contain `'#'`, `'/'`, `'@'`, `'['` or `']'` —
the last four delimit region selectors, `precis-se-regions-help`.)

## Ops — blocks and ports

- `add_block` — `name` (req) · `parent` · `pose` `"[x y z] mm"` · `rot`
  `"[x y z] deg"` (Euler `Rz@Ry@Rx`; see "Units") · `envelope`
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
  `direction` [x,y,z] (normalised) · `pose` `"[x y z] mm"` · `rot`
  `"[rx ry rz] deg"` (block-local; `rot` needs `pose`) · `annotations` dict · **atomic
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
- `flatpack_box` — flat-pack box (`precis-se-flatpack-help`).

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

## Finding and composing library blocks — search(kind='se')

`search(kind='se', wants={...})` ranks library blocks by declared
capabilities; `search(kind='se', compose={...})` proposes a composition
(rotary unit + arms, the lever family) that meets a delta/span ask.
Both, with their worked examples, are in [[precis-se-search-help]].

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

`tree · block · report · ports · topology · chain · measures · revisions · branches · diff · provenance · contracts · datums · validate · clearance · sweep ·
drc · bom · order · fasten · interview · freedom · stability · mechanics ·
literature · fret · links · export · cut · ops`. No `mass` view — mass
goes via `bom`. `interview` elicits what's missing.
Atomic-mode views — `mechanics`/`literature`/`topology`/`sites`/
`catalogue`, and `clearance`'s atomic path (vdW gap, `sweep`, `lining`)
— are documented in `precis-se-atomic-help`; `view='cut'` is the
flat-pack file (`precis-se-flatpack-help`).

The default tree shows, beside each structure-bound block, both halves labelled
by source: the stored build report's error/warning counts (`build report:`) AND
the bound structure's current validator tally (`validator:`) — a clean build
report does not mean the geometry validates. `view='validate'` includes stored
ERROR/WARN findings by block (grouped by error code, warnings summarised) plus
`structure_validator_*` findings re-run on the bound geometry; the build rows
are build-time evidence, the validator rows are the current read.

`view='report'` reads stored generate/join build findings (severity, code,
where, message, span) plus each bound structure's current `validator:` tally
(one cached geometry read per structure — the tally, not the finding list;
full detail stays in `view='validate'`/`view='block'`). Omit args for every
direct block, or use `args={'block': '<label or #uid>'}`. Missing records are
unavailable, not evidence of a successful build; template occurrences are not
expanded. State overrides are not accepted.

`view='ops'` is the only view that reads back into `put`: the design as a
replayable ops list in a fenced `{"ops": [...]}` block, so a design can be
reproduced in another database without a `pg_dump` or prod write access.
It is
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

## Design history and provenance

`view='revisions'` lists every saved revision of the design — rev number,
UTC time, op names, block count. `edit(kind='se', id=…,
ops=[{'op':'restore_revision','rev':N}])` puts the design back to
revision N (a sole op in its call); the restore is itself recorded as a
new revision, so nothing is rewound. An unknown N names the available range.

**Envelope revision.** A save that replaces a surviving block's envelope
(including none → set) mints the next envelope revision; a new block or a
cleared envelope mints none. `view='validate'`/`view='drc'` print
`envelope revision: N`, and stored results (rev snapshots, SIMP runs) carry
it — a result scored under an older N is stale.

**Checkpoints and branches** (each the sole op of its edit call).
`{'op':'checkpoint','label':L,'reason'?}` snapshots the live tree;
`{'op':'restore','label':L}` puts it back as a new revision (labels
`rev-N` are reserved). `{'op':'pin','as':'<new slug>','reason':R,
'ops':[...]}` copies the design (uids kept, scenario/states/envelope
log carried), applies `ops` to the copy only, and replies with a
headline table (blocks, validate errors/warnings, BOM mass/cost when
fully covered), the delta vs parent and `infeasible:` naming the first
new validate error. Both designs stay live; `view='branches'` lists
them one line each; `view='diff', args={'against': <slug>}` gives
per-block deltas joined on uid.

**Provenance.** Numbers carry `{source, fidelity?, solver_id?,
assumptions, library_version?, margin_origin?}` (source: user_stated |
llm_assumed | derived | solver | library | measured; a computed source
needs fidelity). Solvers stamp their own (formfind → pose, relax_chain →
pose, SIMP → geometry); `set_pose`/`set_envelope`/`set_binding` and
`add_measure`/`set_measure` take `provenance=`; `set_provenance
{block, facet: pose|envelope|geometry, provenance|null}` sets one
directly. Overwriting a solver-claimed facet restates it as
user_stated/llm_assumed. `view='provenance'` lists every claim;
`args={'margin': true}` is the margin audit.

**Contracts and insertion cones.** `{'op':'set_contract','a':…,'b':…,
'loads':{'Fz':-40,…},'load_provenance'?:'user_stated'|'llm_assumed'|
'derived_by_statics','provenance'?:{comp: entry}}` merges up to six
components (Fx Fy Fz N, Mx My Mz N·m, design frame); null unsets one,
`clear=true` drops the contract. An absent component is UNKNOWN, never
zero — validate's `contract_component_unknown` names each one.
`{'op':'set_insertion','block':…,'direction':[x,y,z],'half_angle':'15 deg'}`
declares an assembly cone in the block frame (≤ 90°); validate's
`insertion_cones_disjoint` (error) fires when no one direction fits
every cone. `view='contracts'` shows both.

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
- **Load path** (`## load path`): the complementarity solve — per member
  taut/slack/bearing/separated per load case; unrated members skipped
  and named.
- **Load cases**: `args={'load_cases':[{'name':…,'loads':{block:[x,y,z]}}]}`
  on `view='stability'`/`'drc'`/`'validate'` — a tie driven compressive
  is a warn `completeness` finding.
- **Bistability**: `args={'bistable':{'member':'a.p—b.p','trans':m,'cis':m}}`
  probes two free lengths as two stable states.

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
- [[precis-se-search-help]] — ranked library search (wants=), the composition proposer (compose=) and the lever family
