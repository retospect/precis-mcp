---
status: idea
pillar: memory-graph
---

# Fisheye level2

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Turn-taking fisheye Level 2 — focus verb + render loop

_Grouped 2026-09-26; was `fisheye-level2-focus-verb`._

Level 1 (policy-chosen eyes) is live on planner + dreams; reviewers stay
out-of-scope (different render model). Unbuilt: a `focus` verb on the MCP
surface wiring `src/precis/workers/working_set.py`'s WorkingSet/Eye +
render_fisheye so a model places/removes its own eyes; a `--max-turns 1`
render→act→re-render driver behind PRECIS_TURN_LOOP (the decay ladder +
WorkingSet.crunch already exist, nothing drives them); promote-plan-node→todo
(needs TodoHandler `anchor=`; belongs with the render loop). Owner
`src/precis/workers/job_types/plan_tick.py` + `src/precis/utils/fisheye.py`.

## Product-plan review 2026-09-30

Ranked in `threads/graph-memory-consumers.md` (pillar 1, consumers).
Generalizing the affordance shipped: `extent=` on every kind's `get` plus
the `/eye/<handle>` browser focus page.
