---
status: draft
title: se viewer remainder (gr335242) — argue playbook, surface anchors, stick figure
prio: low
model: sonnet
pillar: 3d-design
---

# se viewer remainder (gr335242)

The argue box back-port this file was named for **shipped 2026-10-15**:
`/se/{slug}`'s 3D page now has ONE always-present box (`#bt3d-argue`,
`blocktree-3d.js` + `detail3d.html.j2`); a block pick inserts the block
name at the caret instead of opening a scoped panel, and the saved note's
`about[]` carries every clicked handle still present in the text —
multi-anchor arguments for free (`POST /se/{slug}/note` takes `about`,
`block=` stays as the one-anchor shorthand). se's rewrite step is kept
verbatim: notes are interview capture, pcb deliberately has no rewrite —
the surfaces converged on the input widget, not the processing.

## Still owed — the gr335242 remainder

- **Argument playbook** (gr335242 comment 1): classify an anchored
  argument as change request (propose an edit, `origin='proposed'`),
  interference concern (run the clearance probe / fasten stack-up on
  the named pair, answer with numbers) or verification question (run
  capacity DRC / stability / member forces; when the question exceeds
  the model, answer "not modelled yet" and name the backlog item it
  waits on). Element names in the text (`screw5`) resolve against the
  block/port/BOM namespace. `pcb-argue-with-design.md` slice 2 is the
  shared job; se supplies the checks.
- **Surface anchors + note markers** (comment 2): anchor grammar
  `block@face:top`, `block@edge:…`, `block@p:(x,y,z)`, the same
  vocabulary the pick emits. The viewer draws note markers at anchors,
  and a collapsed ancestor shows its descendants' notes as a badge.
  Removing a block re-anchors its notes to the parent, tagged "was on
  removed <name>". Curved-face picking needs a per-triangle facet id
  from `precis.cad.tessellate` (blocker noted in `blocktree_3d.py`).
- **Pre-solve stick figure** (comment 5d): a first-class sketch of
  blocks and connects before any solve: the cheapest human checkpoint.
