---
id: precis-se-print-help
title: precis — turning a block into a printed part (realize, build orientation, process DRC, STL/3MF)
summary: mint a block's first cad implementation (realize — the envelope seed, or strategy='simp' for an enqueued topology solve bound back as a field leaf), let view='print' pick and pin a build orientation, read the process-DRC findings (overhang, bridge, bed contact, undersize hole, thin feature, load vs layer), write the STL/3MF a slicer opens, print a whole assembly as a fit-test model (a print group with intent='model' — one frame, one 3MF, bought parts as catalog stand-ins) or as the real print-in-place part (intent='manufacture' + realize(strategy='manufacture') — rigid members fused, DOF joints gapped, bought parts as cavities with a pause height, fasteners elided), and read view='fab' for the whole design's fabrication plan across every source
answers:
  - how do I print a block I've designed in se?
  - how do I turn an abstract requirements block into something I can export?
  - how do I let a topology solver (SIMP) shape a printed member from its loads?
  - which way up should this part print, and can I pick it myself?
  - why does view='print' say a block is unrealized?
  - what does abstract_joint mean, and how do I clear it?
  - how do I get an STL or 3MF out of an se design?
  - how do I see everything a design needs to be fabricated, not just the printed parts?
  - how do I print a whole assembly as one fit-test model, screws and bearings included?
  - what is a print group, and where does intent='model' go?
  - how do I print a hinge or a linkage in place, as one job, with the pin already inside?
  - what do gap=, fit= and blend= mean on realize(strategy='manufacture'), and why is a fastener missing from the export?
applies-to: get/edit (kind='se'); read precis-se-help first for the op grammar; precis-se-fasten-help for the screw stack a printed member's holes come from
status: active
tags: workflow, design
kinds: se
---

# precis-se-print-help — realize → print → fab

An `se` block as authored is an **abstract requirements block** — an
envelope, ports, joints as class × mechanism. Printing needs its
**implementation**: a bound `cad` design plus real hardware where a joint
names one. `realize` mints the cad half; `view='print'` runs the process
check and writes the file; `view='fab'` is the whole design's index, not
just the printed parts.

Read `precis-se-help` first for the op grammar. Everything below is
`edit(kind='se', id=…, ops=[…])` unless it says otherwise.

## 1 — realize the block

```python
edit(kind="se", id="unicycle-mk2", ops=[
  {"op": "realize", "block": "hub", "mode": "fdm/pla"},
])
```

Mints a stored `cad` design seeded from the block's own effective
envelope as its one solid (the block's local frame, identity pose), named
`<design>-<block>` (a name collision takes a numeric suffix, reported
back), then `set_binding(kind='cad', ...)` + `set_mode(mode)` on the
block. Deterministic — no LLM guessing, the seed only; refine the geometry
afterward through the normal `cad` edit path.

Refused on a block that is already bound (`set_binding(clear=true)`
first) or has no envelope (`set_envelope` first). An array/template
member realizes through its own template — one implementation per
template, the same way instancing already works. **`realize` never mints
fasteners** — a `screw`/`press`/`bearing`/`magnet`/`cable` connect naming
no real hardware yet stays an `abstract_joint` finding (below); which
screw to buy is a design decision, not something this op guesses.

## 1b — `realize(strategy='simp')`: solve the material instead of seeding it

```python
edit(kind="se", id="unicycle-mk2", ops=[
  {"op": "set_load", "block": "fork", "force": [0, 0, -40], "fixed": true},
  {"op": "realize", "block": "fork", "mode": "fdm/pla", "strategy": "simp",
   "pitch": 0.001, "volfrac": 0.35, "load_at": "z+", "fixed_at": "axle",
   "build_dir": "z+", "close": 0.002, "max_iter": 80},
])
```

A SIMP topology solve over the block's envelope (its keep-in, voxelised
in the block frame at `pitch=` metres): `objectives.force` becomes the
load, `objectives.fixed` the support, and the density comes back as a
NEW cad design rooted at a sampled-field leaf (`field:<sha>`), which the
block is bound to — from there it is ordinary geometry (`cut` bores,
`add` seats, export). The op only **validates and enqueues** an
`se_simp` job (the response carries the job handle; the block reads
`unrealized` until it lands, minutes at a real pitch). Args:

- `pitch=` m — required while the house `simp_pitch` capability is null
  (it is, in every fdm row; a `set_process_override(field='simp_pitch',
  value=<mm>)` on the block also satisfies it). Elements ≈ volume /
  pitch³; more than 500 000 is refused with the count.
- `volfrac=` — required, strictly inside (0, 1): the material fraction.
- `load_at=` / `fixed_at=` — required: WHERE on the block the load acts
  and the support holds. `set_load`'s vector carries no position, so name
  an envelope face (`x+`,`x-`,`y+`,`y-`,`z+`,`z-` — the force is shared
  over that face's nodes) or a port that has a pose in the block frame
  (a port without one is refused). The elements under both are passive
  solid, so a loaded face never thins to a skin.
- `build_dir=` — one of the six axis tokens; the AM overhang filter bakes
  it into the solve. Default: the envelope box's largest face down
  (`z+` on a tie), and the echo says which it chose.
- `round=` | `open=`/`close=` m — grid morphology after the solve
  (`open` rounds convex edges and reports every strut thinner than 2r it
  erased; `close` fills necks; `round` = both at one radius). At least
  half a pitch.
- `max_iter=` — 1..300, default 60. An unconverged run says so in its
  notes; it is a snapshot of a descent, not a design.

When the job lands: the block is bound (`kind='cad'`) and moded, its
`build_frame` is pinned with `origin: simp`, and the run summary sits on
the design's `meta.simp` (`last` + `runs`: inputs hash incl. pitch/
build_dir/volfrac/engine version, compliance first→last, achieved
volume fraction, iterations, converged, overhang count, notes). A block
with no `force` **and** `fixed` is refused pointing at `set_load`; a
block bound to a `component` is refused (unbind first). **Re-realize
mints a sibling**: a new cad design (`-2`, `-3`… suffix), the binding
switches to it, the previous design is left in place — named in
`meta.simp.runs[*].cad` and linked `derived-from` the se design.

Advisory tier: the compliance is an estimate from a voxel model under
one linear-elastic load case (the engine's notes spell out what was and
was not checked); it screens layouts against each other, never
certifies one. `view='print'` on a SIMP-realized block **verifies** the
declared `build_dir` (the 45° voxel rule on the stored field) and skips
the orientation search, saying so; `set_build_frame` overrides the pin
and brings the search back.

## 2a — read `view='print'`: the report

```python
get(kind="se", id="unicycle-mk2", view="print")                       # every fdm block, one section each
get(kind="se", id="unicycle-mk2", view="print", args={"block": "hub"}) # one block, full candidate table
```

With no `args`, one section per block whose resolved mode family is
`fdm`: mode, realized/`unrealized`, the proposed (or pinned) build frame
with its score terms, and findings — then a pointer to `view='fab'` for
everything else in the tree. A design with no `fdm` block says so
honestly; nothing is invented.

**`unrealized`** (info) means exactly what `realize` fixes: the block
says `fdm` but has no bound cad design yet — there is nothing to print.
An unbound `screw`/`press`/`bearing`/`magnet`/`cable` connect touching the
block is **`abstract_joint`** (warn): the requirements say a joint
exists, nothing real realizes it. Fix it the `precis-se-fasten-help` way
— mint the fastener from the series registry, bind it
(`set_binding(kind='component', ...)`), then re-issue the connect naming
it.

### the build orientation

Every `fdm` block gets an orientation search over its printed solid — the
6 axis-aligned "down" directions plus a 30° sweep, scored by overhang
area, bed contact, height and (when the block declares a `force` load)
how much of that load runs across layers rather than in-plane. **It is a
proposal, not a verdict**: the view recomputes and reports the best
candidate on every read.

Pin one yourself when you know better than the search (a known-good
orientation from a prior print, a cosmetic face that must face up):

```python
edit(kind="se", id="unicycle-mk2", ops=[
  {"op": "set_build_frame", "block": "hub", "down": [0, 0, -1]},
])
```

The search still runs on every read — it just reports **how much worse**
the pin scores than the best candidate instead of silently switching to
it. `clear_build_frame block=` removes the pin; the search resumes
proposing. **A pin survives a later `set_envelope`** — the block's own
choice is a contract, the slice-4 `origin` rule.

## 2b — the print-DRC findings

| rule | severity | what it means |
|---|---|---|
| `overhang` | warn | a down-facing face steeper than `max_overhang` (40° house, fdm/pla — `se_capabilities.json` `fdm/pla.max_overhang`) |
| `bridge` | warn | an unsupported span longer than `max_bridge` (10 mm house, fdm/pla and fdm/asa; 5 mm for fdm/tpu — `max_bridge`) |
| `bed_contact` | warn | too little of the footprint actually touches the bed (needs `min_bed_contact`, uncharacterized in the shipped rows today — the rule is silent until a shop measures one) |
| `build_volume` | error | the part is bigger than a named printer's bed (needs `max_build`, `null` in every generic row on purpose — printer-specific, a later data-only addition) |
| `min_feature` | warn | a box/cylinder/frustum primitive in the solid's own node set is thinner than `min_wall`/`min_feature` |
| `hole_undersize` | warn | a stamped hole (from `precis-se-fasten-help`'s pass) is smaller than `min_hole` |
| `hole_shrink_absorbed` | info | the printed-hole compensation already folded into every stamped hole's diameter (`hole_diameter_compensation` — e.g. +0.20 mm for fdm/pla, +0.25 mm for fdm/petg/abs/asa, +0.35 mm for fdm/tpu) — a receipt, not a new number |
| `layer_vs_load` | warn | the block declares a load with some component along the chosen build-z (the weak, layer-normal direction); `strength_z_ratio` (uncharacterized today) is the tensile-across-layers ÷ in-plane figure to weigh it against |
| `unrealized` | info | see above |
| `abstract_joint` | warn | see above |

Every threshold above comes from `se_capabilities.json` through
`capabilities.resolve()` — a block override
(`set_process_override(block=, field=, value=)`) beats the house figure,
clamped to the physical floor/ceiling either way. A field nobody has
characterized for a material (most of the `min_*`/`strength_z_ratio` rows
today) means that rule is silently **not run** for it — never a guessed
number standing in.

## 2c — exporting

```python
get(kind="se", id="unicycle-mk2", view="print",
    args={"block": "hub", "fmt": "stl"})                  # temp path
get(kind="se", id="unicycle-mk2", view="print",
    args={"block": "hub", "fmt": "3mf", "path": "/tmp/hub.3mf"})
```

Writes the file rotated so the build-down direction is `-z`, bed at
`z = 0`, in millimetres — `stl` for one part, `3mf` when the block's
implementation has more than one component. `path` defaults to a temp
file named `<design>-<block>.<fmt>`. Needs the `manifold3d` backend (a
core dependency — a missing one is the same `Unsupported` + install hint
every mesh export in this repo raises). **The body echoes the path, size,
build frame, and every error-severity finding** — a file never leaves
without its warnings; an `abstract_joint` or other warn-tier finding does
not block the write.

## 2d — print groups: `intent='model'` on an ancestor block

```python
edit(kind="se", id="unicycle-mk2", ops=[
  {"op": "set_mode", "block": "wheel", "mode": "fdm/pla", "intent": "model"},
])
get(kind="se", id="unicycle-mk2", view="print", args={"block": "wheel"})
get(kind="se", id="unicycle-mk2", view="print",
    args={"block": "wheel", "fmt": "3mf", "path": "/tmp/wheel.3mf"})
```

A **print group** is an ancestor block in an fdm mode that carries a
print `intent`; its members are the blocks below it (`parent` edges — no
schema, no membership list). **A group ends where the next group root
begins**: a descendant that is itself an intent root (the fork group and
the wheel group under one assembly root) owns its own subtree — the outer
group lists it as `nested group '<name>' — printed separately, see its
own row`, never places or exports it, and every block belongs to its
nearest root. `intent` is a `set_mode` parameter and lives
on the block's `build_frame` record next to a pin (`set_build_frame` /
`clear_build_frame` leave it alone; `set_mode(mode=null)` clears it —
no mode, no group). Values: `model` (this section) and `manufacture`
(§2e — the real print-in-place part). A non-fdm mode with an intent is
refused.

**`model` = a fit-test model, any scale.** Every fdm member prints its
realized solid (stamped holes included, compensation kept). Every
**purchase** member prints as a **stand-in**: the analytic catalog solid
(`part <family>:<size>` from the cad catalog, threads dropped, no drive
recess) when its bound `component` was minted from a series the catalog
reads; else a solid from its spec dims (a bearing = outer cylinder minus
bore; anything else the catalog envelope, said so). Fasteners print too —
the fit is the point. Joints with DOF and rigid joints alike stay separate
parts. A purchase member with nothing to draw is a `no_stand_in` finding
(warn) naming what it needs — a `component` binding, or the spec dims —
never a silent skip. Instances/arrays and non-fdm/non-purchase members are
`member_skipped` (info). Loads are never scaled: a 1:6 toy gets its own
`set_load`.

**One build frame for the group**, chosen by the same orientation search
on the **union** of the member meshes in their world poses (the root's
mode names the process). A `set_build_frame` on the root pins it. A
SIMP-realized member (`build_frame.origin == 'simp'`) pins the group to
its baked `build_dir`; the search is skipped and the report says which
member it followed. Two SIMP members that disagree are a
`simp_frame_conflict` finding (error) — the first by name wins, the
finding names both. Every member's frame findings (overhang, bridge, bed
contact, `layer_vs_load`) are judged at the group frame, each on its own
footprint the way a slicer's drop-to-bed places it.

**Output**: `fmt='3mf'` only — one 3MF, one object per member (stand-ins
are objects too, a multi-component member contributes `<member>/<part>`
objects), every member in world pose, one rotation and one shared bed
offset. `view='print'` with no args renders one `## <root> — print group`
section per group (members do not repeat below it); `view='fab'`
collapses the group to **one row** naming the intent, member count,
stand-in count and the 3MF handle. A group root with no intent is not a
group — everything below it reads exactly as before.

## 2e — print groups: `intent='manufacture'` — the real part, print-in-place

```python
edit(kind="se", id="unicycle-mk2", ops=[
  {"op": "set_mode", "block": "hinge", "mode": "fdm/pla", "intent": "manufacture"},
  {"op": "realize", "block": "hinge", "strategy": "manufacture",
   "gap": 0.0004, "fit": 0.0002, "blend": 0.002},
])
get(kind="se", id="unicycle-mk2", view="print", args={"block": "hinge"})
get(kind="se", id="unicycle-mk2", view="print",
    args={"block": "hinge", "fmt": "3mf", "path": "/tmp/hinge.3mf"})
```

Same group rules as `model` (§2d: fdm ancestor + intent, members below
it, nested roots end the group). `realize(strategy='manufacture')` on the
**root** fuses the members into ONE sampled-field solid in the root's
frame — field/CSG ops only, never a mesh — and binds it to the root as a
new cad design `<design>-<root>-mfg` (re-run: `-2`, `-3`… sibling; the
root must not carry a solid of its own). Every connect between two
members is **rigid** (`rigid`/`captive`/`axial`, or no joint declared —
fused, with a `joint_undeclared` note) or **DOF** (revolute, prismatic,
…):

- **rigid pairs fuse**: min-union; `blend=` (m, default 0 = plain min)
  is the smooth-min width at the seam, the DSL's `blend:` — a fillet-like
  seam, not an exact radius.
- **DOF pairs get the in-place gap, seam-locally**: each printed side is
  **carved back by `gap/2` from its partner** (`A' = A \ dilate(B,
  gap/2)`, the dilation an `offset` on the partner's re-distanced sampled
  field, plus half a pitch because the re-distance binarises — `gap` is a
  floor; face-to-face and pin-in-bore come out at `gap`, a re-entrant
  corner's worst case is `gap/2`, and the report quotes the **measured**
  separation per joint). Nothing else about either member moves, so a
  rigid seam it shares with a third member stays intact. A DOF pair a
  rigid path joins anyway is `dof_bridged` (error) and the export is
  refused. `gap=` (m) is required unless the house
  `min_clearance` capability resolves (null in every fdm row today — a
  `set_process_override(block=<root>, field='min_clearance', value=<mm>)`
  supplies it and then also serves as the default). Below the floor →
  `in_place_clearance` **error**; null floor → an info finding saying the
  floor is uncalibrated and the gap was taken as declared. A pair that
  still touches after erosion is `in_place_fused` (error).
- **bought members become cavities**: the §2d stand-in re-distanced,
  dilated by `fit=` (m, required whenever there is a cavity to cut, no
  default; 0 = the exact stand-in) and subtracted. No insertion path is
  searched: the report names each cavity's **top layer above the bed** as
  the mid-print pause height (the `bambuuzle` rung inserts the part
  there). No stand-in → `cavity_missing` (error).
- **fasteners are elided** when their grip stack (the members the screw
  passes through, `view='fasten'`) is two or more printed members of one
  fused body: `joint fused, <bolt> not needed` (info), no cavity, nuts and
  washers in the stack go with it, the elided screw's clearance holes are
  left uncut in the fused members, and a bare `screw`-mechanism connect
  between fused members is no longer an `abstract_joint` — for
  `manufacture` only; `model` still prints every fastener and its holes.
  A fastener across a DOF joint stays a cavity.
- **horizontal bores**: a DOF axis-class joint whose axis lies within 45°
  of the build plate is an `overhang` finding naming the bore (member,
  connect, axis) — no teardrop primitive exists yet, so it is reported,
  not fixed; an undeclared axis says the check could not run.

## 2f — `manufacture`: what the run stores, what the views show

`pitch=` (m) defaults to the house `layer_height`. The op runs **inline**
when the group has no SIMP-realized member and the grid is under 500 000
cells; otherwise it enqueues an `se_manufacture` job (the root keeps its
previous binding until it lands); above 8 000 000 cells it refuses with
the count. The run summary sits on `meta.manufacture` (`last` + `runs`:
members, fused components, joints, cavities, elisions, objects, measured
gaps, findings). `view='print'` on the root reports the frame (root pin >
SIMP member's `build_dir` > search on the fused mesh), the objects, the
gaps, the cavities with pause heights and the elisions; `fmt='3mf'`
writes **one object per connected component** of the fused field — a
DOF-separated pair comes out as two objects, a fused pair as one.
`view='fab'` says `intent manufacture, N objects, K cavities, E elided`.
A member moved after the fuse → `manufacture_stale` (warn) until you
re-realize. `realize(strategy='simp')` on a manufacture root solves the
**fused group as one body** (keep-in = the members' envelopes minus the
cavities, optional `fit=`; loads/supports on the root); a DOF joint
inside the group is refused there.

## 3 — read `view='fab'` for the whole plan

```python
get(kind="se", id="unicycle-mk2", view="fab")
```

One row per implementation-bearing block, **any source** — not just
`fdm`: `block` / `qty` (through the array multiplicities, same arithmetic
`view='bom'` runs) / `source` (the resolved mode, or `—` when unset) /
`status` / `handle` — what to call next to get the thing:

| source | status | handle |
|---|---|---|
| `purchase` | the bound component/part, or `no item` | the slug + `view='bom'` |
| `fdm` | `unrealized` \| `realized, abstract joints: N` \| `realized, print-checked: N finding(s)` \| `realized, pinned`/`proposed` | `realize(block=, mode=)` when unrealized, else `view='print' args={'block': ..., 'fmt': 'stl'}` |
| `fdm` with `intent` (a print group) | `print group (intent model), N member(s), M stand-in(s)` or `print group (intent manufacture), N member(s), intent manufacture, N objects, K cavities, E elided` (`not fused yet` before the realize) + finding count or frame origin — its members have no rows of their own | `view='print' args={'block': <root>, 'fmt': '3mf'}` |
| `atomic` | bound structure design, or `unbound` | `bind_structure` / `generate` |
| an unimplemented family (`sla`/`cnc-2.5ax`/`laser`/`stock-cut`) | `planned, not checked` | the family name — no implementer yet |
| unset | `—` | `set_mode` |

The footer totals one line per source and, when anything is bought, the
same unit-cost/mass line `view='bom'` renders — one BOM total, never two
truths. **`view='fab'` never exports anything itself** — it is an index;
every row's handle is the export route. A family's handle only appears
once its implementer actually ships, so the table never advertises an
export that doesn't run.

## What this does not do yet

Support-structure generation, slicing, g-code — the deliverable stops at
the file a slicer opens. Plate nesting / multi-part layout / cost-time
estimates (`se-feasibility-and-cost.md`'s domain). Mesh thin-wall
(medial-axis) analysis — `min_feature` is primitive-level, cheap and
honest about what it covers, not a general wall-thickness solver. Curved-
ceiling bridge detection reads as overhang (the conservative reading).
In `manufacture` groups: a **mixed root** — the fused `field:` leaf plus
the members' analytic `add`/`cut`/`blend:` nodes — so sub-pitch features
(hole compensation, seats) survive the re-sample (today every member is
re-sampled at the pitch and the report says so); an insertion-path search
for cavities (today a pause height); a teardrop/diamond bore primitive
(today an `overhang` finding); array/instance members (today
`member_skipped`); and the calibration figures every fdm row still
carries as null (`min_clearance`, `simp_pitch`, `strength_z_ratio`). Process-skill rewrites (e.g.
`bridge-closing-a-bored-ceiling`) are filed follow-on items, not built
here.
