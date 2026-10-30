---
status: draft
pillar: platform
title: claude_inproc lane — plan_tick failures and the hung-call wedge
prio: normal
---

# claude_inproc lane — plan_tick failures and the hung-call wedge

Source gripes: gr262888, gr536350

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

## Dispatch-stall noise and the unclaimable head job (gr536350)

`nursery:dispatch-stall` flapped critical 4× in ~4h on melchior's
agent-profile lane (2026-10-29), each "2 job(s) queued, none running"
self-resolving — short claim gaps while the claude gate reconnected
(~7 s). Reto ruling 2026-10-29: debounce the check so gaps shorter than a
reconnect don't go critical (option (b); a second agent-profile host is a
separate decision — `cluster-scheduling.md` "De-SPOF the melchior agent
worker"). **Shipped** c0230cc96 — `DISPATCH_STALL_DEBOUNCE_S = 60` (~9×
the observed gap) — not yet on `origin/prod` as of 2026-10-30.

Still open (doctor tick, 2026-10-30): a materially bigger recurrence the
debounce does not touch — 7 criticals 11:54→15:11Z reporting 220–305
`claude_inproc` jobs queued, the oldest aging in lockstep with wall-clock
(20.6h→23.9h) while the count drained 305→220 (~1 job/2 min). The lane
was alive throughout (`claimed=1 ok=1 failed=0` every ~2 min) — not
gated, not dead. Reads as a single stuck/poisoned (or wrongly-pinned)
job at the queue head that the claim query skips while newer work drains
behind it: the claim's aged-prio order ranks the oldest first, so a
never-claimed oldest means the eligibility predicate drops it
(`requires` never satisfiable, a wrong executor tag, a `meta` hole) —
identify the job before naming the fix. Blocker named in the gripe: the
session surface has no age-sort/filter on `search(kind='job')`, so the
oldest queued job can't be pinned from it — an affordance gap of its
own.

Two residuals to carry: drain throughput of ~1 job/2 min is thin for a
200+ backlog even with the head job removed; and a slowly-draining lane
may still page — a long-running job with status transitions ~2 min apart
reads as "queued, none running, no life in 60 s" at the sample instant,
so the debounce window sized to a reconnect gap can under-cover a lane
that's merely slow.
