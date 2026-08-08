# Zero-output turn-exhausted plan_ticks log as success

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
