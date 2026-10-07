---
status: idea
title: MSEP.one atom menu item opens a precis box with the local context
pillar: 3d-design
---

# MSEP.one atom menu item opens a precis box with the local context

Reto, 2026-10-07: "have a way to add a menu item to an atom, and to have a
'precis' box with the local context pop up." MSEP.one (https://msep.one,
Drexler's free open-source molecular design and simulation editor, built on
Godot) is the first target; other editors may follow, so the seam should
not be MSEP.one-specific.

**What.** In the editor, an atom's context menu gains a "precis" entry. It
opens a box next to the atom that shows what precis knows about the local
context: the atom's element and bonded environment, the matching
`structure`/`se` block if the scene came from precis, and the findings,
measures and papers that bear on that environment (ring strain, bond
lengths, seam types, relevant claims).

**Why.** The graph is where the knowledge lives (roadmap pillar 1); the
editor is where the designer is looking. Today the two never meet: a
designer in MSEP.one has no door to the evidence precis holds for the
atoms on screen.

**To be designed (the hard part): context sharing.** How the editor's
selection reaches precis and how precis answers it — a Godot plugin that
talks to the shared MCP HTTP server, a local sidecar, or a file/clipboard
hand-off of the selected neighbourhood (extXYZ + indices). What "local
context" means (radius, bonded shell, whole fragment), and whether the
box is read-only or can write a finding/gripe back. Owner anchor:
`src/precis/structure/` (the atomistic kind) and `precis_se` (scene
blocks). Decide the transport before any editor work.

test: with a precis-generated structure open in MSEP.one, right-click an
atom, choose precis, and the box shows that structure's block, its element
environment and at least one linked finding.
