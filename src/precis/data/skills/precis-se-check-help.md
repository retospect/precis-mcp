---
id: precis-se-check-help
family: se
title: precis — checking an se design (stability, drc, fasten, clearance, surface_deviation)
summary: the checking views on an se design — view='stability' (Maxwell/Calladine rigidity + load path), view='drc' (capacity, interference, connect plausibility, joining precedent), view='fasten' (screw stack-up), view='clearance' (envelope gaps), view='surface_deviation' (authored surface vs target) and the finding-id delta grammar shared by validate/drc/clearance
answers:
  - how do I check whether my structure is rigid or a mechanism (stability)?
  - what do view='drc', view='fasten' and view='clearance' each check?
  - how do I diff two check reports — which findings are new, still, gone?
  - how do I measure an authored surface against its generation target?
applies-to: get (kind='se', view='stability'|'drc'|'fasten'|'clearance'|'surface_deviation')
status: active
tags: design
kinds: se
---

# precis-se-check-help — the checking views

Authoring lives in [[precis-se-help]]; this file is what `get` reports
*about* a design. `view='sweep'` checks every declared state at once —
[[precis-se-states-help]]; region/property reads are
[[precis-se-regions-help]].

## Check an authored surface — view='surface_deviation'

S1 authored-surface read: `get(kind='se', id=..., view='surface_deviation',
args={'block':<bound block>, 'target':{'features':[{'name':'tube',
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

## See also

- [[precis-se-help]] — the call surface: units, ops, views, put-vs-edit
- [[precis-se-fasten-help]] — the fasten workflow (screws, threaded holes, printed bosses)
- [[precis-se-design-help]] — the abstraction-ladder walk (checks at every rung)
