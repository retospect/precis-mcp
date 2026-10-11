---
status: draft
pillar: platform
---

# Plan tick health

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## plan-tick-spin detects but doesn't auto-pause

_Grouped 2026-09-26; was `plan-tick-spin-autopause`._

Auto-pausing (an open tag the doable view excludes) would stop the burn but
risks halting legitimate long-running planners — needs a progress signal, not
a count. Parked behavior extension of the nursery plan-tick-spin detector.

## Zero-output turn-exhausted plan_ticks log as success

_Grouped 2026-09-26; was `plan-tick-exhaustion-visibility`._

~20% of local ticks hit the 60-turn ceiling, emit ≤2 chars after 256–618 s,
and record `errored=false` (`_oss_exit` maps max_turns to resumable
exhaustion — right for the executor, blind for the route-log), so a todo can
burn the ceiling repeatedly while looking healthy. In order: (1) make it
visible in `llm_call_log` (flag or errored semantics separating
failed/exhausted); (2) read the retained `llm_blob` request text for the two
known runs before guessing the shape; (3) pre-mediate — a cheap "is there
anything to decide" pre-check before spending an agentic tick (Reto's
framing). Unknown if qwen-specific: check the claude lane's tail first. Owner
`src/precis/workers/job_types/plan_tick.py::_run_oss_tick` / `_oss_exit`.

test: a turn-exhausted empty tick is distinguishable in llm_call_log.

## Planner re-ticked while its only child is parked (gr252969)

The dispatcher re-ticks a planner todo whose only child is parked on
`waiting-for:*`. The parked-child check in
`src/precis/workers/dispatch.py::_parked_child_still_blocks_sql` is a
time-only cooldown since the parent's last `plan_tick`: once it elapses, the
parent ticks again whether or not anything changed under it, and each tick
spends a planner turn to find nothing to decide.

Fix direction:

- Fingerprint the parked child (its tags, status, last comment id and
  `updated_at`) when a tick finds it parked, and store the fingerprint on the
  parent's tick record.
- Skip the re-tick until the fingerprint changes. The cooldown stays as the
  upper bound for a slow-poll, not the trigger.
- Count no-op ticks per parent (a tick whose only effect was to see the same
  parked child) and surface the count, so the plan-tick-spin detector in the
  first section can use it as the progress signal it lacks.

Files: `src/precis/workers/dispatch.py`,
`src/precis/workers/job_types/plan_tick.py`. Who: agent-code, M.
