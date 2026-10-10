---
id: precis-se-states-help
family: se
title: precis — discrete block states, transitions and the swing they derive (se kind)
summary: a block that can be in more than one shape (bistable, photoswitch, conformer, assembly step) declares its states and stimulus-labelled transitions — declare_states/declare_transitions/set_current_state, args={'state':...} to pose one read transiently, view='kinematics' for the axis/angle/arm/tip a transition derives from two port frames plus revolute/prismatic joint sweeps, view='sweep' to check every declared state at once
answers:
  - how do I declare a bistable/photoswitch block's two states, {loaded,bonded} or {trans,cis}?
  - how do I check a block for clash in a specific state, or across every declared state?
  - how do I read the rotation angle or stroke a transition performs — view='kinematics'?
  - how do I declare a revolute or prismatic joint's travel so drc sweeps it for collisions?
applies-to: get/edit (kind='se'); read precis-se-help first for the op grammar
status: active
tags: verbs, design
kinds: se
---

# precis-se-states-help — states, transitions, derived kinematics

Read `precis-se-help` first for the op grammar, units and block
addressing. Everything below is `edit(kind='se', id=…, ops=[…])` or a
`get(kind='se', id=…, view=…)` read, as in that file.

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
  reports the exact port-to-port distance instead. `pose_source` and
  `rot_source` are INDEPENDENT stamps: this op and `add_port` write
  `'declared'` for whichever of `pose`/`rot` they set; `bind_structure`
  measures `'bound'` per field (rot off a port's `axis_atom`/`phase_atom`
  frame) and never overwrites a declared one — `precis-se-atomic-help`.
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

## Kinematics — the derived swing (view='kinematics')

Nothing new to declare: two states already carry a port's frame
(`port_pose_overrides[port].rot` composed onto the port's own `rot`), so
the rotation a transition performs is read straight off them. One table
per design: `axis` (block frame), `angle` (°), `arm (envelope)` — the
port origin to the envelope extent in the plane normal to the axis, an
upper bound on the lever the block offers — and `tip` =
`2·arm·sin(angle/2)`. A port whose frame does not change reads `—`; a
port with **no declared pose** reads `no pose`, never the dash: an
override on a pose-less port is a silent no-op, so `view='validate'`
raises `port_override_unapplied` ("declare set_port_pose first"). A block
with no states gets a one-line note. Sourced `step_angle` /
`rotation_rate` / `rotation_barrier` (rad / Hz / eV, the star-schema
lookup `delta_length` uses) render beside the derived angle; a >10 %
disagreement is named in `note`. A connect whose `joint` is
`class: 'revolute'` (its `axis` in the WORLD frame) is checked against
the port's derived axis after the block's own placement is applied —
past 10° it is `revolute_axis_mismatch` in `view='drc'`: the joint names
the axis, the port carries the rotation, both must agree.

**Joint sweep.** A `revolute` or `prismatic` joint with an `axis` may
declare its travel: `params: {'range': [lo, hi], 'moves': '<block>',
'samples'?: 9}` (radians or metres, displacements from the authored pose,
`lo < hi`, samples ≥ 2, endpoints included). `moves` is required and
names the end that turns/slides; a connect is an unordered pair, so `a`/`b`
order means nothing. `view='drc'` swings/slides that block and its
`parent` subtree about its port's posed origin and checks envelope
overlap at every sample against every unconnected block:
`joint_sweep_interference` (warn) names both blocks and every colliding
run of values. `joint_sweep_clean` (info) counts joints swept without a
hit; `joint_sweep_not_run` (info) says revolute/prismatic joints exist but
none declares a `range`. A block rigidly connected to the moving end (a
`rigid` connect, transitively) moves with it. `joint_sweep_unchecked` (info)
names joints past the
256-sample budget, or whose `moves` names neither end. Discrete states
alone (`view='sweep'`) never see a collision between them.

## Sweep every declared state (view='sweep')

`view='sweep'`: "does anything collide in ANY declared state?" — the
cross product of every state-carrying block's declared states (no
`args`; a block needs 2+ declared states to enter the sweep at all).
Each combination reruns the same undeclared-interpenetration check
`view='validate'` uses; a design with no state-carrying blocks reads as
a clean "nothing to sweep", not an error. The combination count is
capped (64) — a sweep that hits the cap says so and names how many
combinations went unchecked, never truncates silently.


## See also

- [[precis-se-help]] — the call surface: op grammar, units, views
- [[precis-se-design-help]] — the design workflow (abstraction ladder, refine, tradeoffs)
- [[precis-se-atomic-help]] — atomic mode, `bind_structure`, port frames measured off atoms
