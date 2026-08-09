# `good_search` — agentic broad-retrieval as a coordinator campaign

> The agentic tier of the broad-retrieval ladder. Tier 1
> (`search(queries=, answers=, per_paper=)` RRF fusion) shipped; the
> thin coordinator slice shipped 2026-07-02. This file keeps the open
> phases.

Shipped portion: see the `workers/job_types/good_search.py` module
docstring (the phase machine: fuse → fan out triage children →
heartbeat gather → `Done`), `handlers/_good_search.py`, the
`search(good=True)` surface, and `ctx.spawn_child`; full design +
2026-07-02 code-review narrative in git history. Substrate fixes 1–3
(soft-delete-aware `children_done` wake, coordinator comment,
`JOB_PARENT_KINDS` + `'job'` guarded on `executor='coordinator'`)
landed with it. Parenting is decided per ADR 0044: campaign on the
intent lane, children on the coordinator via the polymorphic parent;
idem reuse via `idem_key` + `derived_job_succeeded`.

## Open scope

### Phase 2 — the full ladder

Revisit the thin-slice simplifications:

- Plan-phase fusion is **lexical-only** — the executor slice has no
  embedder seam; semantic legs need one.
- `want` defaults to `'chunks'`; the **verify rung** +
  `kind='citation'` output make `want='citations'` honest.
- Query/HyDE self-expansion + ranking.
- `budget_usd` stored but unenforced (children individually capped via
  claude_p) — enforce + cancel/partial semantics.
- Idem reuse doesn't yet attach the second caller's todo via
  `requested`.
- Wait modes: add `wait=<seconds>` block-poll sugar + document the
  tick-resurrection pattern (interactive callers keep the async
  handle).
- Fidelity ladder constraint (decided): batch triage children (~30
  candidates/child), not 1-agent-per-hit — cost scales with fan-out.

### Phase 3 — provider seam (separate)

An `openai_compatible`/`vllm` executor so triage children run Qwen on
a GPU node. The campaign logic never names a provider — only the
child's `executor=`/`model=` change.

### Phase 4 — generic `waitfor(ids)` (separate, optional)

The LLM-as-own-coordinator primitive: a `plan_tick` dispatches N jobs,
calls `waitfor(ids)`, resurrects on completion. Sugar over an
explicit-id-set variant of `child_job_succeeded`. Only works in tick
context, and non-deterministic orchestration is harder to budget than
a coded phase machine — build it only if the bespoke coordinator
proves the pattern worth generalising.

### Deferred substrate niceties

- `deadline_ts` on the `children_done` wake selector — would restore
  instant-wake semantics and retire the heartbeat workaround.
- `wake_runner._requeue` leaves the 5-min slice lease in place, so a
  woken coordinator slice isn't claimable for up to 5 minutes
  (latency-only; the e2e test hand-expires the lease). Clear the lease
  on `_requeue` or on Yield persist.
- Requeue-from-checkpoint: the sweeper should re-queue a
  `running`-stale *coordinator* job instead of terminally failing it —
  `meta.coordinator_state` is a valid resume point.

## Open questions

- **Output currency** — always write real `kind='citation'` rows, or
  only on `want='citations'`? Citations are durable + paper-linked,
  but a throwaway triage search shouldn't litter the corpus.
- **Pool / batch / fan-out defaults** (200 / 30 / 12?) — tune against
  real cost now that the thin slice runs.
