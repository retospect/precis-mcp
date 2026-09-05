---
status: draft
title: make-tree vs design-tree — assembly/synthesis order as a separate graph over shared leaves
prio: high
model: opus
---

# Make-tree vs design-tree

Design session 2026-09-04 (Reto + agent), imperative-plotting-hare worktree.
Reto's framing: "We need a way to create the synthesis tree inside the
structure (and the boundaries maybe do not align)." Best-practice grounding:
`perplexity-research:317035` — this is the EBOM/MBOM split (the
*Design–Manufacturing Bipartite Alignment Pattern*) and, on the molecular
side, the *Dual-Graph Molecular–Synthetic Route Representation Pattern*
(LinChemIn SynGraph, AiZynthFinder AND/OR route trees). Both fields converged
on the same answer independently, which is strong evidence it's right.

## The rule

**Two trees over shared leaves, linked, never forced to align.**

- The **design tree** (`contains` links, `design-graph-relations.md`) is
  structure: what the thing IS. Owned by the design.
- The **make-tree** is process: the order things come together. A separate
  ref (`route` for chemistry; a plan-shaped ref for mech assembly) whose
  *step nodes are first-class* — a step carries its conditions (work
  center / fixture / torque spec; reagents / temperature / reaction
  template), not just an edge. Step nodes are where reusable know-how
  lives; leaf-to-leaf links can't carry it (report failure mode: "process
  planning nodes not represented explicitly").
- **Alignment edges** (`made-by`, block → step) are many-to-many. A
  make-step may bundle parts across design subsystems (phantom
  assemblies); a retrosynthetic disconnection may form a bond *inside*
  what the designer drew as one block. Forcing one-to-one is the named
  failure mode.

## The atomic side is the hard case — and has TWO make-orders

Raised by Reto 2026-09-04 ("have you considered this also with the atomic
assembly stuff"). An nm structure tree can be realized by two entirely
different process families, and the schema must hold both against the SAME
design tree:

1. **Placed assembly** (mechanosynthesis / the se-kind "atomic assembler"
   mode, DNA-origami-style staged folding): steps look like mech assembly —
   position block, form bond at a face. The make-tree resembles the design
   tree more closely, but still diverges (scaffold-first orders, sacrificial
   supports).
2. **Bulk synthesis** (solution chemistry): steps are reactions
   (click, SN2 backside attack, protection/deprotection); boundaries cut
   across blocks freely; the route is an AND/OR tree of molecules and
   reactions, LinChemIn SynGraph shape (`linchemin` facade→syngraph is
   already ground-truthed in memory).

Same leaves, two candidate make-trees, each with its own feasibility story.
This is exactly why make-order must not live inside the structure: it isn't
a property of the design, it's a *strategy over* the design.

## Synthesizability / buildability

- A **score on blocks/fragments, not just the whole** (BR-SAScore's move:
  fragment-level building-block and reaction-driven scores). Stored as an
  attached model result (`attached-models-layer.md`) so it carries
  provenance and goes stale like any other computed claim. Cheap
  SAscore-style first; real retrosynthesis search later.
- Routes must **terminate in stock**: purchasable precursors (chemistry) /
  catalog parts (mech — the se_bom store the precious-juggling-map tree
  shipped 2026-09-04). A route ending in a non-purchasable leaf is
  incomplete, and the lint should say so (report failure mode: routes not
  linked to stock collections).
- Mech-side "assemblability" is the same slot: can this contains-subtree
  actually be put together in some order (tool access, insertion
  direction)? cad slice 4's sweep probe is a future consumer.

## Non-goals / sequencing

- Not before `design-graph-relations.md` (needs `made-by`).
- v1 is representation + lint (store a make-tree, align it, check stock
  termination and full coverage), NOT route *search*. Retrosynthesis/assembly
  planning engines plug in behind the same schema later.
- nm/structure wiring follows the se-kind plugin posture: schema shared,
  kernels separate.
