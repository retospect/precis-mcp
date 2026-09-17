---
id: precis-se-fasten-help
title: precis — screwing a design together (fasteners, threaded holes, printed bosses)
summary: pick a real ISO screw by head form and drive (hex socket or Torx), place it as a bound block so the stack-up is measured rather than declared, choose what the far end threads into (nut · nut trap · heat-set insert · thread-forming core hole · cut thread), and read view='fasten' for the holes it stamps, the tool that can reach it, and what is too thin, too short or too deep
answers:
  - how do I make a design actually screw together?
  - which screw should I use, and how do I find one that is stocked?
  - how do I put a threaded hole in a 3D-printed part?
  - what hole does a self-tapping (pointy) screw want?
  - why does view='fasten' refuse to stamp anything?
applies-to: get/edit (kind='se'); put (kind='component', series=…); read precis-se-help first for the op grammar
status: active
tags: workflow, design
kinds: se, component
---

# precis-se-fasten-help — from "the parts touch" to "the parts are bolted"

A screw in this tree is **a block, not an annotation**. You add it, bind
it to a real catalogue part, and pose it; the fastening pass then casts a
ray along the screw's own axis, finds every member it passes through, and
derives the grip, the holes, the engagement and the tool clearance from
the poses. Nothing about the stack is declared, so nothing about it can
be declared wrong.

Read `precis-se-help` first for the op grammar. Everything below is
`edit(kind='se', id=…, ops=[…])` unless it says otherwise.

## 1 — pick the screw

**Drive: hex socket or Torx. Nothing else.** They take torque without
camming out and the tools are on the bench; a cross-recess screw gets a
`drive_not_preferred` finding. Families, all minted from the standards
registry:

| want | series | catalogue family |
|---|---|---|
| the default | ISO 4762 socket cap | `iso-4762` |
| flush | ISO 10642 countersunk | `iso-10642` |
| low dome | ISO 7380 button | `iso-7380` |
| Torx versions | ISO 14579 cap · 14581 countersunk · 14583 pan | `iso-14579` … |
| **pointy** (into plastic) | ISO 14585 pan tapping · 14586 countersunk tapping, ST2.9–ST6.3, Torx | `iso-14585` / `iso-14586` |
| headless | ISO 4026 set screw | `iso-4026` |
| does not vibrate loose | ISO 7040 nyloc nut | `iso-7040` |
| threads for a printed part | brass heat-set insert (no standard) | `insert-brass-heatset` |

Find one and mint it:

```python
get(kind="component", view="series")                 # the whole registry
get(kind="component", id="iso-14585", view="series") # one size table
put(kind="component", series="iso-10642", size="M4x12")   # → iso-10642-m4x12
```

**Prefer what is stocked.** Every size row carries an availability tier —
`universal` (in every drawer), `common`, `specialty` — and the resolver
ranks by it after fit. M4×12 beats M14×55 for a reason. A length that is
not on the standard's own list warns: you would be paying someone to cut
it. Specifying a screw nobody holds is the cheapest mistake to avoid and
the most annoying to discover at assembly.

## 2 — place it as a block

```python
ops=[
 {"op": "add_block", "name": "seat_bolt", "parent": "frame"},
 {"op": "set_binding", "block": "seat_bolt", "kind": "component",
  "design": "iso-10642-m4x12"},
 {"op": "set_pose", "block": "seat_bolt", "pose": [0.02, 0, 0.11],
  "rot": [3.14159, 0, 0]},
 {"op": "add_port", "block": "seat_bolt", "name": "thread"},
 {"op": "connect", "a": "seat_bolt.thread", "b": "post.top",
  "joint": {"class": "rigid", "mechanism": "screw",
            "params": {"fit_class": "house", "thread_strategy": "nut-trap"}}},
]
```

**The connect names the screw**, not the two members. A connect between
`saddle.rail` and `post.top` says they are joined; it does not say by
what, and the grip, the holes and the tool are all read off the screw
block's pose. `view='fasten'` refuses a joint whose endpoints are both
members rather than guessing which screw you meant. The stack itself
ends at the block the connect's other endpoint names — a nut just beyond
it still counts, but anything further along the axis is not part of this
joint.

The envelope and ports come from the catalogue row — you do not draw a
screw. **Pose matters**: the screw drives along its own `+z`, head at the
pose, thread going in. If `view='fasten'` says the axis "passes through
nothing", the `rot` is backwards.

## 3 — say what the far end threads into

This is the decision the pass will not make for you on a printed member,
because all four answers are reasonable and they produce different parts.
Set `params.thread_strategy`:

| strategy | stamps | use it when |
|---|---|---|
| `nut` | clearance through; the nut is a part | both sides reachable — strongest, no thread in plastic |
| `nut-trap` | hex pocket + clearance | one-sided, and you want a steel thread |
| `insert` | stepped pocket for a heat-set brass insert | the joint gets undone repeatedly |
| `thread-forming` | a core hole the pointy screw deforms | cheap, few cycles, no second part |
| `tapped` | `d − P` tapping drill | **metal.** In plastic it is an explicit, short-lived choice |

Metal members default to `tapped` and need no param. A printed member
with no strategy gets **no far-end hole at all** and a
`thread_strategy_undeclared` finding — a plausible-looking hole is worse
than no hole, because it prints.

For `insert` and `nut-trap`, the insert or the nut is a **second part**:
add it to the BOM. A pocket with nothing in it is a hole.

**The head end takes one decision too.** A countersunk head always gets
its 90° cone — the screw does not seat without it. A cap, pan or button
head is left **standing proud** unless you ask: `params.counterbore=true`
stamps a bore deep enough to bury it, and without it you get a
`head_stands_proud` note saying how far it sticks up. Burying a 4 mm head
in a 6 mm plate is too big a thing to do to a part unasked.

### the numbers, if you want to know where they come from

- **Core hole** (thread-forming): 0.8 × the screw's major Ø for rigid
  thermoplastics, 0.75 × for tough ones, never below the thread's own
  minor Ø (ISO 1478 for ST sizes, d − P for metric). A house rule in the
  middle of what the thread-forming screw makers publish — there is no
  ISO for this.
- **Engagement**: ≥ 2×D in thermoplastic against 1×D in steel. Too thin a
  member gets a `thread_engagement` finding.
- **Boss**: ≥ 2×D of material around a formed thread. Reported in prose,
  not stamped — this pass subtracts holes, it never adds material, so
  modelling the boss is yours.
- **Printed holes come out undersize**, so every stamped hole in an
  `fdm/*` member is modelled ~0.2–0.35 mm oversize; the hole's `source`
  line says by how much and how confident that is. Calibrate your printer
  once and edit `se_capabilities.json` rather than arguing with it.
- **A blind tapped or thread-forming hole is drilled deeper than the
  thread it carries**: 2 pitches of tip clearance so the screw never
  bottoms on thread runout, plus (`tapped` only) 3 pitches for the plug
  tap's chamfer — a thread-forming core hole gets the tip clearance alone,
  no tap runs in it. Where that would reach the member's far face, the
  hole comes back through instead and the feature's `source` says so —
  a house rule, not a standard.

## 4 — read `view='fasten'`

```python
get(kind="se", id="unicycle-mk2", view="fasten")
```

It gives you, per screw joint: the **stack** the axis walks (member, from,
to, thickness, made by), the **grip** and whether the screw is long enough,
the **thread as a lead** ("5.2 turns from first thread to seated"), the
**tool** that can reach it, and the **holes** with the provenance of every
diameter. Findings worth knowing by name:

- `thread_strategy_undeclared` — the printed far end, above.
- `screw_too_short` / `thread_engagement` — the stack needs more screw.
- `pocket_too_deep` — an insert pocket or nut trap deeper than the member
  it sits in, i.e. out the far face. A `tapped`/`thread-forming` hole's
  own depth is sized from the engagement it needs rather than the whole
  member thickness (above), and comes back through instead when that
  would reach the far face — a house allowance, not a certification that
  a specific screw can't bottom out.
- `no_tool_access` — no driver clears the assembly. Names the block in the
  way. A hex key sweeps its long arm in a circle, which is usually what
  runs out first; a bit driver needs a straight run instead. (A ratchet
  needing only part of its swing is not modelled, so a tight joint may
  still be buildable.)
- `fastener_axis` — the joint's declared axis and the screw's own pose
  disagree by more than 2.5°. The pose wins; fix one of them.
- `drive_not_preferred` — you specified something that is not hex or Torx.

Findings also fold into `view='drc'`, so a fastening problem shows up in
the design's ordinary check without anyone remembering to look here.

## 5 — the holes are derived, so put them in the realization yourself

Stamped features are **recomputed on every read and stored nowhere**:
`view='fasten'` is the feature list, and there is no second copy to drift.
When you realize a printed member as cad geometry, cut the holes it names
— diameter, depth, origin and axis are all given:

```
seat_hole cut cyl:r1.7mmh8mm @20mm,0mm,110mm
```

Re-run `view='fasten'` after any pose change: the stack is measured, so
moving a block silently changes what the right screw is.

## What this does not do yet

Assembly **order** (can the parts go together in some sequence, and can
you reach a nut trap to drop the nut in); **edge distance** from a hole to
a part's edge; the position-tolerance relation each hole pattern implies
(reported in prose by the multi-hole warning instead). A `screw` joint
between two blocks with no fastener block bound is refused rather than
guessed at — the BOM line says which screw, the block says where it is.
