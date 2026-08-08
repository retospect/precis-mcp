# Turn-taking fisheye Level 2 — focus verb + render loop

Level 1 (policy-chosen eyes) is live on planner + dreams; reviewers stay
out-of-scope (different render model). Unbuilt: a `focus` verb on the MCP
surface wiring `src/precis/workers/working_set.py`'s WorkingSet/Eye +
render_fisheye so a model places/removes its own eyes; a `--max-turns 1`
render→act→re-render driver behind PRECIS_TURN_LOOP (the decay ladder +
WorkingSet.crunch already exist, nothing drives them); promote-plan-node→todo
(needs TodoHandler `anchor=`; belongs with the render loop). Owner
`src/precis/workers/job_types/plan_tick.py` + `src/precis/utils/fisheye.py`.
