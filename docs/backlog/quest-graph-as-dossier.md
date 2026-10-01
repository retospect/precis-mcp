---
status: idea
pillar: memory-graph
prio: normal
---

# Quest graph is the dossier — findings, structures and logbook entries belong to the quest node at the apex; a writer agent linearises on demand

## Ruling (Reto, 2026-10-01, verbatim)

"we want a graph that 'belongs to' the quest, and it 'is' the dossier. We can
linearize it with a writer agent, but we manage the things in the mesh, with a
hierarchy of 'belongs to quest' or something with the quest node at apex."

## What it replaces

The dossier as a maintained `draft` document linked `dossier-of`, rewritten in
place by the quest tick (`src/precis/quest/dossier.py`; glossary "dossier").
Truth moves to the mesh: the quest node is the apex, findings, structures,
papers and logbook entries hang under it, and any linear document is a render.
Superseded as input-only designs: `quest-dossier-dialectic.md`,
`dossier-present-tense-refinement.md`, `topic-report-quests.md`.

## Open design questions

1. **Relation name.** Existing: `serves` (work/knowledge node → quest, already
   used for structures/todos/papers; `src/precis/store/types.py`), `dossier-of`
   (draft → quest, 1:1), `contains`/`part-of` (component BOM edge). Decide:
   reuse `serves` as the membership edge, widen `part-of`, or add `belongs-to`.
   Check `precis-relations` skill and `relation-constraints.md` (kind-scoping
   of an edge) first.
2. **Depth and hierarchy under the apex.** Flat membership, or sub-nodes
   (sub-quest, hypothesis, argument) that own their own members; how a cycle
   guard composes with `serves`.
3. **Linearisation trigger.** On demand by a writer agent, per tick, or on
   export; whether its output is ephemeral (a render, discarded) or persisted
   as a draft the reader can cite and `paper-of` binds to.

## Links

- `knowledge-mesh.md` — the mesh this graph lives in.
- `memory-native-authoring.md` — same principle: graph is truth, text is a render.
- Existing quest-loop consumers of the dossier (`quest-artifacts-in-dossier.md`,
  the weave tick) retarget once 1-3 are decided.
