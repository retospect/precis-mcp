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

## Generalize the fisheye discovery affordance beyond draft chunk reads

_Grouped 2026-09-26; was `fisheye-affordance-generalize`._

The `→ view='fisheye'` footer exists only in `DraftHandler._render_chunk`;
paper/patent/web/datasheet/cfp/memory/finding chunk reads also have fisheye
eyes (`src/precis/utils/eye_render.py::render_eye`) but never advertise it —
an agent reading those kinds unprompted can't discover fisheye. Generalize
the teach-at-render affordance; optional: a session damper if it proves noisy
in read loops, and a one-line mention in the server-instructions string
(`src/precis/server.py`). Mechanical.

test: per-kind assertion that a plain single-chunk get carries the affordance
line (parallel to tests/test_draft_handler.py's).
