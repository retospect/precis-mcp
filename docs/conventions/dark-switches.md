# Dark-switch audit — default-off feature flags

precis ships a lot of code **dark**: built, tested, deployed, but gated
behind a `PRECIS_*` env flag that defaults off until an operator (Reto)
flips it. That's a deliberate pattern (Phase-2 provisioning: land the code,
prove it in a container/dry-run, flip the flag once the prerequisite is
ready) — not a bug. The risk this doc guards against is a *different*
failure mode: a flag that was flipped-off during development and then
**forgotten**, where the gated code has since been superseded and nobody
remembers to delete it.

This audit (2026-07-22) classifies each flag on the starter list from
the dark-switch audit into one of three buckets.
**Recommend-only** — nothing here was deleted unless a flag had zero
remaining callers *and* no existing note saying "leave it dark on purpose."

| Flag | Class | Why |
|---|---|---|
| `PRECIS_BACKLOG_GROOM_ENABLED` | off-by-ruling | `workers/backlog_groom.py` (gripe→`fix_gripe`-todo groomer) is fully wired (`cli/worker.py`, `workers/registry.py`) but OFF on prod by Reto's ruling 2026-10-02 (td460703): the `service_config` row <agent-host>/`backlog_groom` is prio 0, so it mints no `fix_gripe` todos (sessions fix gripes; the lane never delivered, the agent host's fix checkout has no push credential). Re-arm only on Reto's say: `precis service prio <agent-host> backlog_groom 5`. Code stays. |
| `PRECIS_FRICTION_REFLECT` | intentional-staged | End-of-run tool-friction footer (`utils/friction_reflect.py`, wired into `utils/claude_agent.py`). the backlog names the explicit prerequisite (a downstream grouping/dedup lane for `friction` gripes) that gates flipping it on — staged on purpose, not forgotten. |
| `ROLE3:own` (chunk-tag classify pass) | intentional-staged | Not itself an env var — a tag value the classify pass (`workers/classify.py`) writes when `PRECIS_CLASSIFY_ENABLED` (also default-off) runs. the backlog (gr690306) documents the activation path (flip `PRECIS_CLASSIFY_ENABLED=1` to drain the corpus) and the gold-set-classify memory notes full-corpus commit awaits a passing model + Reto's go-ahead — a deliberate gate, not an orphan. |
| `PRECIS_AGENT_CONTAINER` | intentional-staged | The §13 container-agent executor. the backlog records: image built, distributed, and smoke-proven on the agent host; "Flip is the window action" — deliberately staged behind the flag pending the Phase-2 deploy window. |
| `PRECIS_SCHEDULER_ENABLED` | **resolved** — flag retired, no longer exists | Was intentional-staged at audit time; the §A cutover (2026-08) flipped the Phase-2 window: the `scheduler` `ServiceSpec` dropped `enable_env` entirely and is default-on for both worker profiles (`workers/registry.py`), and `retire-thin-timers.yml` boots out the standalone launchd timers it replaces. Row kept for the audit's historical record. |
| `PRECIS_MCP_DB_ROLE_ENFORCE` | on in agent paths | `utils/claude_agent.py::_prepare_agent_env` and `workers/executors/agent_container.py::container_env` set it to `1`, so `store/pool.py::_apply_db_role`'s per-transaction `SET LOCAL ROLE` (pgbouncer-safe since slice S1) binds the envelope role on spawned-agent connections. Still defaults off elsewhere — keying on `PRECIS_MCP_DB_ROLE`'s presence would silently arm write-isolation fleet-wide — and fails closed without the `agent_rw`→`agent_ro` grant. |
| `PRECIS_LLM_BACKEND` | intentional, not a "dark" flag | A live mode selector (default `anthropic`, can be set to `openai` to reroute the LLM router at an OSS/hosted endpoint) — actively used across many call sites (`workers/dream_agent.py`, `workers/review.py`, `workers/job_types/*`, `precis_web/ask.py`, …) as the deliberate "switch the whole pass to a different backend without a redeploy" seam (the LLM routing seam). Not a candidate for removal; it's infrastructure, not a forgotten hook. |
| `PRECIS_LLM_FAILOVER` | intentional-staged safety net | When `PRECIS_LLM_BACKEND=openai`, wraps the OSS primary in a `FailoverProvider` that falls back to `claude` on error. Off by default because it only matters once the OSS backend is actually in use; a deliberate safety net for that future state, not orphaned. |

## Budget-guardrails wiring — verified, not a dark-switch item but audited alongside

While auditing flags, this pass also confirmed `src/precis/budget/breaker.py`
(`gate_tier`/`gate_paid`, hourly+daily caps, `/budget` resume-override,
Discord alerting) is **fully wired on `main`**, not a stray dark hook:
`utils/llm/router.py::dispatch` calls `gate_tier` before every paid-tier
dispatch, `handlers/_cache_base.py`'s paid-fetch path calls `gate_paid`, and
`precis_web/routes/budget.py` exposes the web-editable caps + resume control.
`tests/test_budget.py` exercises both gates end to end. See
me690560 for the residual pieces (the
cost-band affordance isn't surfaced to any model prompt yet; per-entity cost
attribution is only partly stamped).
