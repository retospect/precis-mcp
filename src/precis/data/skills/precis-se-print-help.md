---
id: precis-se-print-help
title: precis — turning a block into a printed part (realize, build orientation, process DRC, STL/3MF)
summary: mint a block's first cad implementation (realize), let view='print' pick and pin a build orientation, read the process-DRC findings (overhang, bridge, bed contact, undersize hole, thin feature, load vs layer), write the STL/3MF a slicer opens, and read view='fab' for the whole design's fabrication plan across every source
answers:
  - how do I print a block I've designed in se?
  - how do I turn an abstract requirements block into something I can export?
  - which way up should this part print, and can I pick it myself?
  - why does view='print' say a block is unrealized?
  - what does abstract_joint mean, and how do I clear it?
  - how do I get an STL or 3MF out of an se design?
  - how do I see everything a design needs to be fabricated, not just the printed parts?
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
Print-in-place groups (one shared frame + in-place clearance across a
`whole-assembly`) and process-skill rewrites (e.g.
`bridge-closing-a-bored-ceiling`) are filed follow-on items, not built
here.
