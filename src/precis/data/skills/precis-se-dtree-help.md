---
id: precis-se-dtree-help
family: se
title: precis — walking the design knowledge graph (function to leaf)
summary: name the function each interface must perform, walk the goal tree's asks/when/avoid down to a leaf, sweep every axis on the leaf, run the principles sweep on every part, record leaf + decision + principles notes, and fall back to look-up plus a gap gripe where the tree is shallow
answers:
  - which joint or mechanism should this connect use?
  - how do I pick between threaded, snap, adhesive and the other ways to join?
  - what do I check on every part before calling a design done?
  - what is joint.params.leaf and where do I get a leaf id?
  - what do I do when the design tree has no deep answer for this interface?
applies-to: get/search (kind='finding'); edit (kind='se') connect + add_note; read precis-se-help and precis-se-design-help first
status: active
tags: workflow, design
kinds: se, finding
---

# precis-se-dtree-help — the design knowledge graph

A linked graph of mechanical design knowledge, one tree per **function**
an interface performs. Each tree walks from a goal root through method
branches down to **leaves** — implementable variants carrying rules with
numbers, common issues with fixes, and named sub-choices. Shared
libraries sit beside the trees: **axes** (sub-choices every leaf
considers), **principles** (checks every part gets), **failure modes** and
the **criteria** vocabulary the questions are asked in.

The graph is served as `kind='finding'` refs tagged
`SPACE:design-conventions`. A node's stable id is its path —
`join/service/threaded/heat-set-insert` — stored as `meta.dtree.id`; the
ref's own handle is unrelated, so **find a node by searching its id
string**:

```python
search(kind="finding", tags=["SPACE:design-conventions", "dt:goal"],
       q="join")                       # the goal roots
search(kind="finding", tags=["SPACE:design-conventions"],
       q="join/service/threaded/heat-set-insert")   # one node by id
get(kind="finding", id=<handle>, extent="fisheye+1hop")  # node + edges
```

`fisheye+1hop` shows the node's `specialises` children and parent, its
rules (`part-of`), issues/fixes and `related-to` axes, failure modes,
principles and processes. The tags `dt:goal|method|leaf|axis|principle|…`
and `domain:mech` filter searches.

## 0 — name the function per interface

Before connecting two blocks, ask what the connection must *do* and pick
the goal root for it — the connect's `joint.class` is the kinematics,
the leaf is the mechanism that realises it:

| the interface must… | goal root | an se connect looks like |
|---|---|---|
| hold two parts still, permanent or detachable | `join` | `class: rigid`, mechanism screw/snap/bond |
| turn about an axis | `rotate` | `class: revolute` — bearing, pivot |
| keep a lid/door shut and reopen | `close` | hinged lid, latch, snap cap |
| position two parts relative to each other | `locate` | dowel, key, nesting pocket |
| hold a bought part it cannot change | `mount` | PCB, bearing, battery, motor |
| slide along a line | `slide` | `class: prismatic` |
| keep gas/liquid in or out | `seal` | gasket, o-ring |
| store or return a force | `spring` | `class: compliant` |
| hold position, adjust, damp, carry, transmit | `hold-position`, `adjust`, `damp`, `carry`, `transmit`, `hinge` | per class |
| be made by a process | `process` | `set_mode` choices, not a connect |

A lidded box names three: `join` for the shell seams, `rotate` (or
`close/hinged`) for the lid hinge, `close` for the lid's shut state. A
bracket bolted to a plate names `join` — and `mount` when the thing it
carries is bought.

## 1 — the walk: asks, then when/avoid, then leaf

At the goal root, read its `asks` — the criterion list the branch choice
turns on (`separable`, `load_kind`, `load_N`, `material_pair`, `process`,
`access`, `quantity`, `environment`, `temp_range_C` for `join`). Answer
them from the design — from the loads, the BOM, the printer on the bench,
not from the leaf you already fancy.

At each level, read every child's `when` and `avoid_when` and descend the
one that fits; `avoid_when` vetoes, `when` prefers. Repeat until the node
has `role: leaf` — e.g. one-sided access, service joint, printed far
part, repeated open/close walks
`join → service → threaded → heat-set-insert`.

On the leaf, use: `rules` (each numbered, with confidence and source),
`issues` (the failure modes this variant actually dies of, with `fix`),
`variants`, `tradeoffs`, `howto`, `links` to related nodes. A
`shop-rule` or `unverified` confidence is a hint, not a number to
dimension from.

## 2 — the axis sweep: every axis on every leaf

Five shared sub-choices apply to every leaf, declared in its `axes` map
as `relevant` or `n/a: <reason>`:

- `axis/locking` — what keeps it from loosening under vibration
- `axis/captive` — must the parts stay captured when undone
- `axis/corrosion` — always answered on a metal pair, indoors or not
- `axis/bearing-face` — what the fastener head or face presses on
- `axis/material-direction` — FDM layup, grain, fibre direction against
  the load

Answer all five per interface. `"n/a"` plus a one-line reason is a
complete answer; an axis left unconsidered is not.

## 3 — the principles sweep: every part

Run the principle findings on **every part**, leaf or not — the
`dt:principle` tag lists them:

```python
search(kind="finding",
       tags=["SPACE:design-conventions", "dt:principle"])
```

- `principles/retention-all-dof` — all 6 DOF intended or stopped, **in
  both directions**; friction-only retention under vibration is suspect
- `principles/fail-safe-retention` — when the retainer fails, what falls,
  leaks or flies off
- `principles/load-path` — trace the load from application to ground
  through the parts
- `principles/assembly-and-access` — the insertion path of every part and
  tool, in assembly order
- `principles/over-constraint`, `principles/mistake-proofing`,
  `principles/tolerance-stack`, `principles/thermal-expansion`,
  `principles/look-up-dont-assume` — one line each per part.

## 4 — record what the walk decided

- Per connect: `joint.params.leaf` = the leaf id, validated against the
  packaged export (works whether or not the graph is loaded):

```python
{"op": "connect", "a": "lid.rim", "b": "shell.top",
 "joint": {"class": "compliant", "mechanism": "integral",
           "params": {"leaf": "close/hinged/living-hinge"}}}
```

  plus one `add_note` `kind: decision` (`about:` the connect's blocks)
  carrying the path walked, the `asks` answers, the options rejected and
  the axis answers — `decision-<connect>` is a good name.
- Per part: one note named `principles-<part>` (a `decision` today; a
  dedicated kind lands later) answering each DOF — intended or what
  stops it, both directions — plus fail-safe, load path and insertion
  path.

A soft finish gate that warns on undeclared leaves and missing
principles notes is planned but not shipped; until it lands, the notes
are the record that makes the sweeps auditable.

## 5 — when the walk ends short

`coverage: shallow` or `stub` means the node names the choice but
carries no verified rules. Look the numbers up — standards, supplier
design guides, `principles/look-up-dont-assume` — then file a gap:

```python
put(kind="gripe", text="design-tree gap: mount/pcb-mount is shallow — "
    "needs standoff height and screw size rules (hit walking a board mount)")
```

**If the design graph is not loaded** (the `SPACE:design-conventions`
search returns nothing): the leaf ids still work in `params.leaf` — they
validate against the packaged export, not the live graph. Do the same
discipline by hand: name the function per interface, pick a mechanism
from general knowledge, run the five-axis and principles sweeps, and
record leaf + decision + principles notes exactly as above.
