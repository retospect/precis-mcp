# `good_search` — agentic broad-retrieval as a coordinator campaign

> Status: **design / for review** (2026-07-01; **amended 2026-07-02**
> after a code-verification review and the landing of ADR 0044). The
> agentic tier of the broad-retrieval ladder. Tier 1 (`search(queries=,
> answers=, per_paper=)` RRF fusion) shipped; this doc specifies the
> agentic tier that sits on top of it. No code yet — review this first.
>
> 2026-07-02 amendments: child jobs parent on the **coordinator** (one
> `JOB_PARENT_KINDS` extension, riding ADR 0044's polymorphic parent);
> multi-waiter reuse via `requested` + `derived_job_succeeded`;
> liveness moved to `at_time` heartbeats (a parked `children_done` wait
> can hang forever on a stuck-queued child); the global campaign cap
> moved out of `validate_submit` (which doesn't fire link-less); known
> substrate fixes listed in §Substrate fixes.
>
> Note: because the substrate is a durable yield/resume coordinator,
> this **collapses the old Tier 2 (blocking agentic search) and Tier 3
> (durable deep-research job) into one thing.** The earlier plan split
> them because a *blocking* Tier 2 couldn't scale to thousands of
> candidates; a coordinator campaign is durable by construction, so the
> only difference between "quick" and "deep" is the pool/fan-out/budget
> params — not a different mechanism. "Blocking" survives only as the
> `wait=<seconds>` convenience mode over the same job.

## Motivation

Tier 1 made a single `search(kind='paper', …)` call fuse many
phrasings + hypothetical-answer passages into one ranked pool. It is
fast and synchronous, but it still returns a *flat ranked list* the
caller has to read and judge itself. The gold is often buried: a
paper's most relevant chunk ranks 40th because the query wording drifts
from the author's wording, or the truly-supporting sentence sits two
chunks away from the keyword hit.

What we want (Reto's framing): *"a slow but very clever tool from the
outside"* — hand it a question and some context, and it goes off,
generates queries, fans out cheap sub-agents to triage a large pool,
pokes around inside the promising papers, and comes back with a small
set of **excellent, verified citations** — not 500 raw hits.

The blocker was always the substrate: fan-out → wait-for-children →
gather, durably, without pinning a worker slot for the whole run. That
substrate now exists — the **`coordinator` executor** + **`wake_runner`**
(shipped for precis-dft's `dft_campaign`, see
`docs/design/dft-phase-0-pr-3-coordinator-executor.md`). This doc maps
the search campaign onto it.

## Today's shape (what we build on)

- **Tier-1 fusion.** `store.search_blocks_multi`
  (`src/precis/store/_blocks_ops.py:798`) — app-level RRF over N
  lexical/semantic legs; reached from `PaperHandler.search` when
  `queries=`/`answers=`/`per_paper=` are present
  (`src/precis/handlers/paper.py:914`).
- **Coordinator executor.** `spec.dispatch(ctx, spec)` returns `Done`
  or `Yield(state, WakeWhen(kind, payload))`
  (`src/precis/workers/executors/_yield.py`). `WakeWhen` kinds:
  `children_done` / `at_time` / `tag_cleared` / `tag_added`. The
  executor persists `meta.coordinator_state`, sets `STATUS:waiting_*`,
  releases the slot; `wake_runner` re-tags `STATUS:queued` when the
  condition fires (`src/precis/workers/executors/coordinator.py`,
  `src/precis/workers/wake_runner.py`). 5-min lease per slice,
  unbounded lifetime, cooperative cancel + `ask-user:*` pause built in.
- **Job substrate.** `JobHandler.put(kind='job', job_type=…,
  executor=…, params=…, parent_id=…, idem_key=…)`
  (`src/precis/handlers/job.py:84`). A `JobTypeSpec`
  (`src/precis/workers/job_types/__init__.py`) declares
  `PARAMS_SCHEMA`, `COMPATIBLE_EXECUTORS`, `REQUIRES`, `run`,
  `dispatch`. NB `put(model=…)` is **retry-only** (it swaps the parent
  todo's `LLM:*` tag on `mode='retry'`) — per-child model rides in
  `params.model`, which is how this doc's PARAMS_SCHEMA already
  carries it. Model plumbing to the subprocess exists:
  `call_claude_agent(model=…)` → `--model`
  (`src/precis/utils/claude_agent.py:121,197`). `idem_key` dedupe is
  already built and race-safe (advisory lock + non-terminal lookup,
  `job.py:274-288,487`).
- **Polymorphic job parent (ADR 0044, landed 2026-07-02).** A job's
  `parent_id` may be a `todo` (intent lane: rotation + `child-failed`
  bubble + `child_job_succeeded`) or a build subject
  (`structure`/`cad`/`draft`; compute lane) — `JOB_PARENT_KINDS` in
  `handlers/job.py`, enforced by `check_job_parent_exists`
  (`handlers/_todo_guards.py`). A requester todo that wants to *wait*
  on a job it doesn't parent links `requested`→job (migration 0046);
  `derived_job_succeeded` closes the requester on success, and the
  failure-bubble follows `requested` links when the job's parent is
  not a todo — **a non-todo-parented job with no requester bubbles
  nowhere** (`_job_bubble.py`), which is load-bearing for this design
  (see §Parenting).
- **Citation kind.** `put(kind='citation', text=<claim>,
  source_handle=<slug~N>, source_quote=<verbatim>,
  verifier_confidence=0..1, link='paper:<slug>', rel='cites')`
  (`src/precis/handlers/citation.py:87`). This is the campaign's
  *output currency*.
- **DispatchContext.** `ctx.store`, `ctx.ref_id`, `ctx.meta`,
  `ctx.set_status`, `ctx.append_chunk(kind, text)`, `ctx.set_meta(**)`,
  `ctx.record_failure`, `ctx.is_cancel_requested`
  (`src/precis/workers/executors/_context.py`). Note: **no**
  first-class child-spawn helper yet — see §Gaps.

## Design

### The MCP surface — `good=True`

`search(kind='paper', q='…', good=True, …)` does **not** search inline.
It mints a `good_search` coordinator job and hands the result back via
one of three wait modes (see §How the caller gets the result). The
**default is an async handle**:

```
search(kind='paper', q='oxygen evolution overpotential on NiFe', good=True)
 → { job: 'j8f3', status: 'queued',
     poll: "get(kind='job', id='j8f3')",
     note: "deep search running; poll, wait=, or attach child_job_succeeded" }
```

The caller polls `get(kind='job', id='j8f3')`; on `STATUS:succeeded`
the job carries the curated citations (as `job_summary` text + a
structured `job_result` chunk, and — optionally — real `kind='citation'`
rows linked to the papers). Extra `good=True` inputs mirror Tier 1 and
add campaign controls:

| arg | meaning |
|---|---|
| `q` | the question / information need (required) |
| `queries=` | seed rephrasings (optional; the campaign also generates its own) |
| `answers=` | seed HyDE passages (optional) |
| `context=` | free-text brief: what this is for, what a good source looks like |
| `wait` | `0` (default, async handle) \| `<seconds>` block-poll budget (see below) |
| `max_children` | fan-out ceiling (default e.g. 12 triage batches) |
| `budget_usd` | hard cost cap for the whole campaign |
| `want` | `'citations'` (default) \| `'chunks'` \| `'papers'` |

### How the caller gets the result — three wait modes, one substrate

The campaign runs on the worker (yield/resume); the question is only how
the *caller* learns it's done. The deciding fact: **to truly "waitfor"
you must be a durable entity that can suspend and resume.** precis has
exactly two — `coordinator` jobs and `plan_tick` coroutines — both woken
by `wake_runner` / `dispatch` re-mint. A synchronous MCP call is *not*
durable: it can't yield, so it can only hold a live process open and
poll. That makes the modes one mechanism at different strengths, not
three peers:

1. **Async handle (default, `wait=0`).** Return the job id immediately;
   caller polls `get(kind='job', id=…)`. Safe everywhere, never times
   out, one extra round-trip. The floor.

2. **`wait=<seconds>` — block-poll sugar (interactive / short runs).**
   The MCP handler loops on the job's status up to `wait` seconds (which
   MUST sit well under the caller's MCP tool-call timeout), then returns
   the finished result, or `{partial, job, poll}` on expiry. This is
   *strictly weaker* than yield/resume — it pins a request and is
   timeout-bounded — but it delivers the "ask a question, get citations
   back in one call" UX for a live human/opus who can't be resurrected.
   It is sugar over mode 1's polling, moved server-side; no new
   substrate.

3. **Resurrection (recommended for planner ticks) — durable, ~free.**
   When the caller *is* a `plan_tick`, don't block at all: mint the
   `good_search` coordinator as a child of the tick's parent, attach the
   existing `child_job_succeeded` auto_check, and let the tick yield /
   exit succeeded-non-blocking. `dispatch` re-mints the tick when the
   campaign lands and it reads the citations fresh. No blocking, no
   timeout, fully durable — the "own the agents layer" path: an agent
   fires a deep search and is woken with the answer. Reuses the plan_tick
   coroutine machinery verbatim; no new mechanism.

Note the internal symmetry: the campaign already uses
`WakeWhen(children_done, {child_job_ids})` to wait on its own triage
children — modes 2/3 are the *caller* doing the same wait one level up.
"Wait for a set of ids" and "resurrection" are the same primitive (yield
+ wake condition + re-mint); "blocking" is that primitive with a live
poll-loop swapped in for the yield.

### Parenting (RESOLVED 2026-07-02, per ADR 0044)

Two levels, two different answers:

**The campaign job** rides the **intent lane**: it parents on the
caller's todo (a tick passes its parent), or — for a bare interactive
`good=True` with no ambient todo — on an auto-minted lightweight
`kind='todo'` (`title="deep search: <q>"`, tagged `ephemeral` +
`project:` if in a workspace). Because triage/verify children do NOT
hang under that todo (below), the todo's **only** child job is the
campaign, so `child_job_succeeded` (which walks direct children only —
`auto_check_evaluators/child_job_succeeded.py:94`) closes it exactly
when the campaign succeeds — the auto-close is now correct rather than
premature. A campaign *failure* bubbles `child-failed:` onto that todo,
which is also correct: someone asked, someone should see it.

**Triage/verify children parent on the coordinator job itself.** This
needs one small substrate extension riding ADR 0044's mechanism: add
`'job'` to `JOB_PARENT_KINDS` (or a `frozenset` the good_search
`ctx.spawn_child` passes). Everything downstream already behaves
correctly for a non-todo parent — verified against the landed code:

- `children_done` wakes on the **explicit id list** in
  `meta.wake_when.payload.child_job_ids` (`wake_runner.py`), not a
  parent walk — parenting is free to choose.
- `child_job_succeeded` on the ephemeral todo never sees grandchildren,
  so a succeeding triage batch cannot close the todo early.
- `bubble_job_failure` on a non-todo parent follows `requested` links;
  a child has no requester → logged no-op, **no bubble anywhere**
  (`_job_bubble.py`). That is exactly the tolerated-partial-failure
  semantics: the coordinator reads child terminal status itself on
  resume; a dead triage batch drops its candidates without tagging the
  shared todo and blocking the wait-mode-3 resurrection.
- The sweeper still recovers a *stuck-running* child (1h), whose
  failure then bubbles nowhere but satisfies `children_done` — the
  campaign proceeds without it.

**Multi-waiter reuse (open question 4, RESOLVED).** `idem_key =
hash(q, normalized filters)` on the campaign job: a second identical
`good=True` submit collapses onto the in-flight campaign
(`_lookup_idem` matches non-terminal jobs). The second caller's todo
can't be a second parent — it links `requested`→campaign and arms
`derived_job_succeeded` instead (ADR 0044 fan-in, supported verbatim:
the bubble fans `child-failed:` to every live requester on failure).

### The `good_search` job_type

```python
# src/precis/workers/job_types/good_search.py
PARAMS_SCHEMA = {                      # validated at submit
  "q": {"type": "string"},
  "queries": {"type": ["array", "null"]},
  "answers": {"type": ["array", "null"]},
  "context": {"type": ["string", "null"]},
  "max_children": {"type": ["integer", "null"]},
  "budget_usd": {"type": ["number", "null"]},
  "want": {"enum": ["citations", "chunks", "papers"]},
  "model": {"type": ["string", "null"]},   # triage-child model (cheap default)
}
COMPATIBLE_EXECUTORS = frozenset({"coordinator"})
REQUIRES = frozenset()                 # work happens in the children
SPEC = JobTypeSpec(name="good_search", …, dispatch=dispatch)
```

`dispatch(ctx, spec)` is a **phase machine** keyed on
`ctx.meta['coordinator_state'].get('phase')`:

**Phase `plan` (first slice).**
1. Optionally expand `queries`/`answers` with one cheap LLM call
   (`claude_agent.run(model=<cheap>, …)`) — turn `q` + `context` into
   ~6 rephrasings and ~4 HyDE passages if the caller didn't supply
   enough. (Skippable; seed args alone are valid.)
2. Run **Tier-1 fusion** directly (`store.search_blocks_multi`) to build
   a candidate pool of, say, 200 chunks, `per_paper`-capped for breadth.
3. Partition the pool into **triage batches** of 20–50 candidates
   (the fidelity-ladder rung — *not* one child per hit; 500 concurrent
   `claude -p` is unrealistic, claude_inproc runs only on melchior's
   agent worker, see the topology memo).
4. Mint one child `kind='job'` per batch via `ctx.spawn_child`
   (parented on the coordinator — see §Parenting), collect their
   `child_job_ids`.
5. `return Yield(state={"phase":"triage", "child_job_ids":[…],
   "deadline_ts":…, "pool":<compact>},
   wake_when=WakeWhen("at_time", {"ts": now+HEARTBEAT}))`. **Heartbeat,
   not a bare `children_done`** — see §Liveness: each wake checks child
   terminality itself (the same SQL `children_done` would run) and either
   advances the phase, re-yields another heartbeat, or force-completes
   past the deadline. `WakeWhen` has no compound `children_done OR
   at_time`, and a bare `children_done` parks forever on a child stuck
   at `STATUS:queued` — which the sweeper never rescues (it only fails
   `running`).

**Phase `triage` (resume on heartbeat once all listed children are
terminal — §Liveness).**
1. Read each child's `job_result` chunk — a structured verdict list
   (`candidate_handle`, `keep: bool`, `relevance: 0..1`, `why`,
   optional `best_quote`).
2. Merge + rank kept candidates (weighted by child relevance × the
   original fusion rank — cross-signal fusion again).
3. If `want='papers'`/`'chunks'`: `return Done(summary, meta={results})`.
4. If `want='citations'` and any top candidate needs verification: mint
   a second, smaller fan-out of **verify** children (open the paper,
   read neighbour chunks, extract the verbatim `source_quote`); `Yield`
   on heartbeat again → phase `verify`.

**Phase `verify` (resume).** Gather verified quotes, `put(kind='citation',
…)` for each survivor (or return them inline per `want`), `return
Done(summary=<curated list>, summary_meta={"citations":[…],
"cost_usd":…, "children":…})`.

Budget is checked at every phase boundary (`ctx` carries cumulative
cost via child `job_result` metas); over-budget → `Done(success=True,
summary=<best-effort partial>, meta={budget_hit:True})`. Cancel is
polled via `ctx.is_cancel_requested()` at each slice.

### Child contract (triage + verify)

Children are ordinary `kind='job'` rows under the coordinator, run by
`claude_inproc` with a **cheap model** (`params.model`, e.g. Haiku).

- **triage child** `params`: `{q, context, candidates:[{handle, text,
  paper}], want}`. It reads the batch, returns (as its `job_result`
  chunk) a JSON verdict list — keep/relevance/why/best_quote per
  candidate. No corpus writes. Cheap, one-shot, no tools needed beyond
  the prompt.
- **verify child** `params`: `{q, candidate_handle, paper_slug}`. It
  *pokes around*: `get(kind='paper', id=…)` / `search` scoped to the
  paper / read neighbour chunks, confirms the claim, extracts a verbatim
  `source_quote`, and (optionally) writes a `memory`. Returns the
  citation fields in its `job_result`. This is the agentic rung — it
  gets MCP tools via the same `--mcp-config` wiring plan_tick uses.

Children write results as a `job_result` chunk (structured, per-tick
audit) so the coordinator reads them back by walking its children on
resume. Child failures do **not** bubble (children parent on the
coordinator, have no `requested` requester, and the bubble is a no-op
for that shape — §Parenting); the coordinator tolerates partial child
failure (a dead triage batch just drops its candidates, and the `Done`
envelope reports `children_failed: N`) rather than failing the whole
campaign.

### Fidelity ladder (why batching, not 1-agent-per-hit)

| rung | who | cost | job |
|---|---|---|---|
| 0 batched triage | 1 cheap child / 20–50 candidates | ~cents | keep/drop + relevance |
| 1 per-candidate judge | 1 cheap child / survivor | low | tighter relevance + quote |
| 2 agentic verify | 1 child / citation-worthy hit | medium | open paper, read neighbours, verbatim quote, maybe memory |

The campaign escalates only the survivors down the ladder, so total
cost scales with *quality of the pool*, not its raw size.

## Operational bounds, attribution, result shape

### Liveness / lifetime

The coordinator's unbounded lifetime is a feature for DFT campaigns but a
hazard for search — a campaign whose children never reach terminal status
would wait forever. **The 2026-07-01 draft's guards could not actually
fire: a job parked at `STATUS:waiting_children` gets no slice until the
wake condition is satisfied, so "checked at every phase boundary" never
runs while the campaign is stuck.** The concrete stuck case is a child
at `STATUS:queued` (melchior's agent worker down/wedged — a known
failure mode): the sweeper rescues only `running` children, `WakeWhen`
has no compound condition, and the campaign parks forever. Guards, as
amended:

- **`at_time` heartbeats instead of `children_done`.** While children
  are pending the coordinator yields `WakeWhen("at_time", {ts:
  now+HEARTBEAT})` (e.g. 3 min); each wake it checks child terminality
  itself, then advances / re-yields / force-completes. This makes the
  wall-clock + slice caps enforceable *while parked*, at the cost of a
  few no-op slices on long campaigns. (Future substrate nicety: a
  `deadline_ts` field on the `children_done` selector would restore
  instant wake — see §Substrate fixes.)
- **Max wall-clock + max slices** in `coordinator_state`
  (`started_ts`, `slice_count`); checked on every heartbeat wake and
  forces `Done(success=True, summary=<best-effort>,
  meta={timed_out:True})` when exceeded, counting still-queued children
  as dropped batches. Default e.g. 20 min / 30 slices — tune with the
  cost defaults.
- **Empty pool** (Tier-1 fusion returns nothing) → `Done` immediately
  with an empty result and a "no candidates; broaden `q`/`queries`" note.
- **All triage children failed** → `Done(success=False)` with the child
  failure reasons, not a silent empty result.
- **Sweeper interaction:** a yielded campaign sits at a `waiting_*`
  status, *not* `STATUS:running`, so the stuck-job sweeper
  (`PRECIS_STUCK_JOB_HOURS`, fails `running` > 1h) never touches it —
  correct, since it's legitimately paused. Caveat found in review: a
  slice that crashes mid-run sits at `STATUS:running` with **no lease
  takeover** (the claim SQL requires `STATUS:queued`; the comment in
  `coordinator.py:155-158` claiming takeover is wrong) until the sweeper
  **terminally fails the whole campaign** at 1h, despite the valid
  checkpoint in `meta.coordinator_state`. Slices are short (the heavy
  work lives in children), so exposure is a deploy-restart mid-slice;
  requeue-from-checkpoint is a substrate fix (§Substrate fixes), not a
  good_search blocker.

### Worker contention

`claude_inproc` jobs run **only on melchior's agent-profile worker**
(see the agent-worker topology note). A single campaign minting 12
triage children plus verify children can flood that one queue and starve
`plan_tick`s + reviewers. Bounds:

- A **per-campaign in-flight child cap** (`max_children`, batched so the
  pool is covered in waves rather than all at once) — the coordinator
  mints the next wave only after the prior `children_done`.
- A **global concurrent-campaign cap** (how many `good_search`
  coordinators may be non-terminal at once; over-cap submits queue).
  NOT in `validate_submit` — review found `JobHandler.put` only calls it
  when a link target exists (`job.py:205-210`), and a `good_search`
  submit is link-less. Enforce the `COUNT(*)` on non-terminal
  `good_search` jobs in the `good=True` MCP surface before
  `JobHandler.put` (our code, both entry points), and defensively again
  in phase `plan` (over cap → `Yield(at_time)` until a slot frees).
- Bias children to a **cheap model** so throughput on the one worker is
  high and each child is short.

### Attribution & observability

- **agentlog.** A campaign is precisely the multi-agent run the
  `agentlog` kind records. The coordinator opens one
  (`precis.agentlog.open_log`) at phase `plan`, threads its id to each
  child via `PRECIS_CURRENT_AGENTLOG`, and verify children that write
  `kind='citation'` / `memory` attribute their writes
  (`attach_touch` / `touch_from_env`); `finalize_log` at `Done`. This
  gets the campaign a `/agentlogs` entry (assembled prompts + touched
  chunks) for free.
- **Forensics.** Per-slice detail as `job_event` chunks (hidden),
  agent-readable account as `job_summary`, structured per-phase audit as
  `job_result` — the standard job forensics triple.
- **Health.** Non-terminal campaign count + any `child-failed:*` bubbles
  surface on the existing `/status` Background Health panel.

### Result envelope

`get(kind='job', id=…)` on a finished campaign returns (in `meta` +
`job_summary`):

```
{ status: 'succeeded',
  result: {
    want: 'citations',
    citations: [ { text, source_handle, source_quote,
                   verifier_confidence, paper_slug, citation_id? }, … ],
    considered: 214, kept: 18, verified: 6,
    cost_usd: 0.41, children: 12, wall_seconds: 380,
    partial: false, note: null } }
```

`want='chunks'`/`'papers'` swap the `citations` array for ranked chunk
handles / paper slugs. `partial:true` + `note` on any budget/timeout
best-effort exit (modes carry it through to the `wait=` response too).

## Substrate fixes (ride along with the thin slice; small + test each)

Found in the 2026-07-02 code review; all are one-liners-to-small and
benefit every coordinator campaign, not just search:

1. **`children_done` wake ignores `deleted_at`** — the NOT EXISTS
   subquery in `wake_runner._wake_children_done` doesn't filter
   soft-deleted children, so operator-deleting a stuck child (soft
   delete; tags persist) blocks the wake forever. Add
   `AND c.deleted_at IS NULL`.
2. **Wrong comment in `coordinator.py:155-158`** — claims lease
   takeover on expiry, but the claim SQL requires `STATUS:queued`, so a
   crashed slice is unreachable until the sweeper terminally fails the
   campaign. Fix the comment now; requeue-from-checkpoint (sweeper
   re-queues a `running`-stale *coordinator* job instead of failing it,
   since `meta.coordinator_state` is a valid resume point) is the real
   fix, separate change.
3. **`JOB_PARENT_KINDS` + `'job'`** for coordinator-parented children
   (§Parenting) — with a guard that the parent job carries
   `executor='coordinator'`, so ordinary jobs don't grow child trees.
4. *(nicety, deferred)* `deadline_ts` on the `children_done` selector —
   would restore instant-wake semantics and retire the heartbeat
   workaround in §Liveness.
5. *(found during the thin-slice build, deferred)* `wake_runner._requeue`
   re-tags `STATUS:queued` but leaves the 5-min slice lease in place,
   and the claim SQL requires an expired/absent lease — so a woken
   coordinator slice isn't claimable for up to 5 minutes after its wake
   fires. Latency-only (the heartbeat is 3 min anyway), but clearing
   the lease on `_requeue` (or on Yield persist) would make wakes
   immediate. The e2e test works around it by expiring the lease by
   hand.

## Gaps / deferred (do NOT block this build)

1. **`ctx.spawn_child(...)` helper.** `DispatchContext` has no child-mint
   helper today; the dispatcher would drop to `ctx.store` +
   `JobHandler.put`. Add a thin `ctx.spawn_child(job_type, params,
   model=…) -> job_id` as part of this build — every coordinator campaign
   wants it. It mints with `parent_id=<coordinator ref>` (§Parenting) and
   must NOT auto-inject any `auto_check` onto anything (the coordinator
   reads child status itself).
2. **Provider independence (Qwen).** Executors today: `claude_inproc`
   (subprocess `claude -p`), `ssh_node` (remote-node seam), `coordinator`.
   There is **no** provider-agnostic runner — triage children are Claude
   subprocesses. Build the campaign on `claude_inproc` + cheap model
   first; add an `openai_compatible`/`vllm` executor later (a Qwen/vLLM
   GPU node fits the `ssh_node` seam) **without touching the coordinator
   or the job_type** — only the child's `executor=`/`model=` change.
   That is the "own the agents layer" payoff: the campaign logic never
   names a provider.
3. **Generic `waitfor(ids)` primitive (future, separate).** The bespoke
   `good_search` coordinator is the *compiled* form of a more general
   idea: let a `plan_tick` **be its own coordinator** — dispatch N triage
   jobs itself, call an LLM-facing `waitfor(ids)`, get resurrected when
   they finish, then read/rank the results in-prompt. Mechanically that's
   sugar over "attach a wake condition (an explicit-id-set variant of
   `child_job_succeeded`, which today only waits on *my children*, not an
   id set) to my tick's parent and yield." Attractive — less bespoke
   code, provider-agnostic since the LLM orchestrates — but it only works
   **in tick context** (interactive callers still need mode 1/2), and the
   orchestration is non-deterministic and harder to budget/bound than a
   coded phase machine. Ship the bespoke coordinator first (deterministic,
   testable, hard cost cap, callable from anywhere); revisit `waitfor` as
   a general primitive once the campaign proves the substrate.

## Open questions (for review)

1. ~~**Parent todo** for a bare `good=True` search~~ — **RESOLVED
   2026-07-02**, see §Parenting: campaign on the intent lane (caller's
   todo or auto-minted ephemeral todo, now safe because children don't
   hang under it); children on the coordinator via ADR 0044's
   polymorphic parent.
2. **Output currency** — always write real `kind='citation'` rows, or
   only on `want='citations'` and otherwise return inline? Citations are
   durable + linked to papers (good), but a throwaway triage search
   shouldn't litter the corpus.
3. **Pool size / batch size / fan-out ceiling defaults** — 200 / 30 / 12?
   Tune against real cost once the thin slice runs.
4. ~~**Reuse** — dedupe onto an in-flight campaign?~~ — **RESOLVED
   2026-07-02**: `idem_key` dedupe already exists (`job.py:487`,
   race-safe); extra waiters attach via `requested` +
   `derived_job_succeeded` (§Parenting). Nothing to build beyond
   choosing the key normalization.

## Testing

- Unit: `good_search.dispatch` phase machine with a fake `ctx` +
  stubbed `store.search_blocks_multi` and stub child `job_result`s —
  assert the `plan→triage→verify→Done` transitions and the `WakeWhen`
  payloads (mirror the coordinator's existing dispatch tests).
- Child prompts: golden-ish assertions on the triage/verify param
  shapes; `PRECIS_CLAUDE_BIN` stub binary for the subprocess path (as
  `claude_agent` tests already do).
- End-to-end: a `MockEmbedder`-backed store + stub children, driving one
  campaign through `run_coordinator_pass` + `wake_runner` to `Done`.
- Wait modes: `wait=0` returns a handle without polling; `wait=<n>`
  block-polls and returns `{partial, job, poll}` when the stub campaign
  hasn't finished inside the budget; the tick-resurrection path is
  covered by the existing `child_job_succeeded` + dispatch re-mint tests
  (assert good_search hangs under the tick's parent and the tick re-mints
  on completion).

## Phasing

1. **Thin slice — BUILT 2026-07-02** (`workers/job_types/good_search.py`,
   `handlers/_good_search.py`, `search(good=True)` surface): fuse → fan
   out triage children → heartbeat gather → `Done` with a merged
   verdict; `ctx.spawn_child` landed with it. Wait mode: async handle
   only (`wait=0`). Thin-slice simplifications to revisit in phase 2:
   plan-phase fusion is **lexical-only** (no embedder seam on the
   executor — semantic legs need one), `want` defaults to `'chunks'`
   (`'citations'` would be dishonest without the verify rung),
   `budget_usd` stored but unenforced (children individually capped via
   claude_p), and idem reuse doesn't yet attach the second caller's
   todo via `requested`.
2. **Full ladder** — query/HyDE self-expansion, ranking, verify rung +
   `kind='citation'` output, budget/cancel/partial, the `good=True` MCP
   surface + skill docs. Wait modes: add `wait=<seconds>` block-poll sugar
   + document the tick-resurrection pattern.
3. **Provider seam** (separate) — `openai_compatible`/`vllm` executor so
   triage children can run Qwen on a GPU node.
4. **Generic `waitfor(ids)`** (separate, optional) — the LLM-as-own-
   coordinator primitive (see §Gaps 3); only if the bespoke coordinator
   proves the pattern worth generalising.
