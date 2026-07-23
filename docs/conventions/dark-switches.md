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
`OPEN-ITEMS.md`'s "Dark-switch audit" entry into one of three buckets.
**Recommend-only** — nothing here was deleted unless a flag had zero
remaining callers *and* no existing note saying "leave it dark on purpose."

| Flag | Class | Why |
|---|---|---|
| `PRECIS_LAYER2_FIXER` | orphaned/superseded (harmless, kept dark by choice) | The Layer-2 chktex LLM-fixer on the `kind='tex'` put path (`utils/tex_llm_fix.py`, one caller in `handlers/plaintext.py`). Drafts are now the authoring source of truth, so this hook is likely superseded — but it's self-contained and harmless, so OPEN-ITEMS already says leave it dark and decide keep-vs-delete deliberately later (removing it also drops the Layer-2 fix-*hint* on tex puts, a real behavior change, not a mechanical rip). |
| `PRECIS_BACKLOG_GROOM_ENABLED` | intentional-staged | `workers/backlog_groom.py` (gripe→`fix_gripe`-todo groomer) is fully wired (`cli/worker.py`, `workers/registry.py`) with an explicit documented activation step in OPEN-ITEMS's "Dark-factory" section: flip it on a system worker once ready to drain open gripes, watch mint count + fixer throughput before widening. |
| `PRECIS_FRICTION_REFLECT` | intentional-staged | End-of-run tool-friction footer (`utils/friction_reflect.py`, wired into `utils/claude_agent.py`). OPEN-ITEMS's "Tool-friction reflection" section names the explicit prerequisite (a downstream grouping/dedup lane for `friction` gripes) that gates flipping it on — staged on purpose, not forgotten. |
| `ROLE3:own` (chunk-tag classify pass) | intentional-staged | Not itself an env var — a tag value the classify pass (`workers/classify.py`) writes when `PRECIS_CLASSIFY_ENABLED` (also default-off) runs. OPEN-ITEMS's "Chunk-tag classifier" section documents the activation path (flip `PRECIS_CLASSIFY_ENABLED=1` to drain the corpus) and the gold-set-classify memory notes full-corpus commit awaits a passing model + Reto's go-ahead — a deliberate gate, not an orphan. |
| `PRECIS_AGENT_CONTAINER` | intentional-staged | The §13 container-agent executor. OPEN-ITEMS's "Track 1" section: image built, distributed, and smoke-proven on melchior; "Flip is the window action" — deliberately staged behind the flag pending the Phase-2 deploy window. |
| `PRECIS_SCHEDULER_ENABLED` | intentional-staged | `workers/scheduler.py`'s own docstring: "Ships DARK (Phase-1)... The Phase-2 window flips the flag on across the fleet and retires the [standalone launchd] timers." Explicitly phased, not forgotten. |
| `PRECIS_MCP_DB_ROLE_ENFORCE` | intentional-staged | `store/pool.py::_apply_db_role`'s docstring names its own prerequisite (`GRANT agent_ro TO agent_rw` provisioned out-of-tree in prod) and fails closed by design — a Phase-2 flag waiting on a cluster-side grant, not dead code. |
| `PRECIS_LLM_BACKEND` | intentional, not a "dark" flag | A live mode selector (default `anthropic`, can be set to `openai` to reroute the LLM router at an OSS/hosted endpoint) — actively used across many call sites (`workers/dream_agent.py`, `workers/review.py`, `workers/job_types/*`, `precis_web/ask.py`, …) as the deliberate "switch the whole pass to a different backend without a redeploy" seam (ADR 0046). Not a candidate for removal; it's infrastructure, not a forgotten hook. |
| `PRECIS_LLM_FAILOVER` | intentional-staged safety net | When `PRECIS_LLM_BACKEND=openai`, wraps the OSS primary in a `FailoverProvider` that falls back to `claude` on error. Off by default because it only matters once the OSS backend is actually in use; a deliberate safety net for that future state, not orphaned. |

## Budget-guardrails wiring — verified, not a dark-switch item but audited alongside

While auditing flags, this pass also confirmed `src/precis/budget/breaker.py`
(`gate_tier`/`gate_paid`, hourly+daily caps, `/budget` resume-override,
Discord alerting) is **fully wired on `main`**, not a stray dark hook:
`utils/llm/router.py::dispatch` calls `gate_tier` before every paid-tier
dispatch, `handlers/_cache_base.py`'s paid-fetch path calls `gate_paid`, and
`precis_web/routes/budget.py` exposes the web-editable caps + resume control.
`tests/test_budget.py` exercises both gates end to end. See
`OPEN-ITEMS.md`'s "Budget guardrails" section for the residual pieces (the
cost-band affordance isn't surfaced to any model prompt yet; per-entity cost
attribution is only partly stamped).
