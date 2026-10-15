---
status: draft
pillar: platform
title: claude_inproc lane — plan_tick failures and the hung-call wedge
prio: normal
---

# claude_inproc lane — plan_tick failures and the hung-call wedge

> The starvation shipped 2026-10-15: old jobs sat behind fresh ones because
> the claim ordered strictly by `prio` and a steady stream of fresh prio-2
> `news_poll` work always outranked prio-5/8 rows (prod, 30 days: prio-8
> `diagnose_gripe` / `doctor_tick` / `draft_refresh` waited up to
> 186 / 185 / 164 min while prio-2 ran at a median of 0). An ordering
> choice, not repeated claim failure. `claim_executor_jobs` now ages a
> queued job one band per `PRECIS_CLAIM_AGE_STEP_S` (default 600 s),
> floored above the prio-1 preempt band. Repro:
> `tests/test_claim_ageing.py`.

## Left

- `plan_tick` has the worst failed/succeeded ratio on the lane (37 / 105
  on 2026-08-23). Undiagnosed; not the starvation cause.
- Claim skips are still quiet: `_finish_claim` drops a row for a full
  resource slot, memory pressure or the `llm:` affinity grace with at most
  a DEBUG line, so such a wait reads like queueing.

## The lane wedges on a hung call (gr262888)

On 2026-10-04 the main runner stalled inside a crossref call that had no
request timeout, and the `claude_inproc` lane stopped serving. A pass that
blocks on one network call stalls everything behind it, which also looks like
starvation.

The per-pass watchdog shipped (gr486823: `BoundedPass` / `deadline_s` in
`workers/runner.py` abandons a ref-pass past its bound). Left: explicit
request timeouts on every `requests`/`httpx` call in the crossref and
enrich passes under `src/precis/workers/`, so a pass fails fast instead of
running into its deadline. Who: agent-code, S.
