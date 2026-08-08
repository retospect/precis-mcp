# Morning combine runaway fixed (gr192606) — verify + optional hardening

The dispatch brake shipped (a succeeded child job blocks re-dispatch of a
deterministic parent). Verify one post-deploy morning cycle: one episode,
`source="brief"`, full news wire + personal brief ≈ 25–26 min, and the "news
lead-in prepended N segment(s)" log line on spark. Optional
defense-in-depth: version the daily news ref instead of destructive
slug-replace; add the derived-from link from cast draft to news ref.
Rejected: reordering news/brief crons (masks, doesn't fix). Owner
`src/precis/workers/dispatch.py`, `src/precis/workers/cast_audio.py`.
Regression: test_dispatch_worker.py::test_succeeded_child_job_blocks_deterministic_parent.
