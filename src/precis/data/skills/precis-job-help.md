---
id: precis-job-help
title: precis — offline work, addressable
summary: offline-work substrate — submit, poll, cancel; parent-todo contract, status, event timeline
answers:
  - how do I submit an offline job tied to a todo?
  - how do jobs differ from a regular synchronous tool call?
  - how do I check what jobs are currently running or queued?
  - how do I cancel a job that's taking too long?
  - how do I submit a job idempotently so a retry doesn't double-run it?
  - how do I read worker_logs / what is the cluster doing right now?
  - why is my todo parked child-failed (child-failed-parked, child-failed-final) and how do I unpark it?
applies-to: get/search/put/tag (kind='job')
tags: workflow, verbs, troubleshooting
kinds: job, todo
status: active
---

# precis-job-help — submit a job, poll for status, cancel

A **job** in precis is **one execution attempt** of an intent.
The intent itself lives as a `kind='todo'` ref; the job is its
child via `parent_id`. Each job has a numeric id, a `STATUS:` tag,
a parent todo, an optional `link` to whatever it operates on
(e.g. a gripe), and a comment timeline of `job_event` /
`job_summary` chunks.

Submit. Walk away. Come back to a `STATUS:succeeded` row — the
parent todo's `meta.auto_check={'type':'child_job_succeeded'}`
will resolve it to `STATUS:done` on the next auto_check pass.

Not cron. Not celery. Not a subprocess you wait on.

## The contract every job submit must satisfy

* **Every new job must declare its parent todo.** `put(kind='job',
  parent_id=<todo_id>, ...)` is the only legal shape. The handler
  rejects orphan submits with a clear next hint.
* **The canonical path is `meta.executor` on a todo + the
  dispatch worker.** Write the intent as a todo with
  `meta={'executor': ..., 'job_type': ...}`; the dispatch worker
  mints the job under it. Direct `put(kind='job', parent_id=N,
  ...)` works for ad-hoc submits but skips the auto_check
  injection — see `precis-minter-help` for the full pattern.
* **A failed job tags its parent — with bounded self-healing.**
  Infra-class failures (`swept:claim-orphaned`, `infra:child-killed`
  — lease orphan / compute child died by signal or without a result
  file) do NOT park the parent: the dispatcher re-mints a fresh
  attempt, capped (3 per 6 h). Content-class failures tag the parent
  `child-failed:<job_id>`; the sweeper's `unpark` phase then retries
  it autonomously on an escalating cool-down (12 h / 24 h / 48 h,
  cap 3) before latching the terminal `child-failed-final` tag.
  Transient causes retry sooner: a failure reason reading as a
  rate/spend limit or transient API fault stamps `meta.retry_after`
  on the job, and the unpark fires at that time (15 min–2 h) instead
  of the 12 h base — same cap. `child-failed-final` —
  only *that* tag means the substrate has given up and a human (or
  the parent's owner) must decide. The nursery surfaces
  still-recoverable parks per-leaf and finals as one aggregate.
  A terminally parked leaf carries BOTH `child-failed-final` AND its
  last open `child-failed:<job_id>` tag; clearing them takes the
  retry verb or a single two-tag `tag(remove=…)` (see below) —
  removing `child-failed-final` on its own does not stick.

## Why is my todo parked child-failed (child-failed-parked / child-failed-final)?

A content-class job failure tags the parent todo `child-failed:<job_id>`
(the "child-failed-parked" state). The sweeper's `unpark` phase retries it
on a 12 h / 24 h / 48 h cool-down, cap 3; only `child-failed-final` means
the substrate gave up. The full rules are the "A failed job tags its
parent" bullet above; the manual retry verb is under "Retry a failed job"
below. Infra-class failures (`swept:claim-orphaned`, `infra:child-killed`)
never park the parent — a fresh attempt is minted instead.

## What is a job in precis
## How do jobs differ from regular tool calls?

A unit of offline work, addressable by id. Lives in the DB; runs
on whichever host has a runner for its executor. Reports back via
status tags + a `job_summary` chunk that you can search later.

Use a job when:

- The work takes minutes-to-hours.
- You want to come back to it later or hand off.
- Another agent or process needs to find or check it.
- It needs to run on different hardware (cluster, GPU box) — once
  more executors land.

Don't use a job for work that fits inside the current conversation.

## What job types are available?
## List the registered job_types

Agent and internal job types; the compute and sandbox types are in the
next section.

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `fix_gripe`         | `claude_inproc` | Fix a gripe and land it. **Lane OFF since 2026-10-02**: nothing auto-mints it; a hand-submit skips at the push preflight without a credential |
| `diagnose_gripe`    | `claude_inproc` | Read-only root-cause diagnosis of a gripe — clones the repo (never branches/commits/pushes), asks claude for a structured `DIAGNOSIS:` block (root cause + evidence + a proposed-fix sketch + `Confidence: 0.NN`), and appends it as one `DIAGNOSIS (auto, job <id>):` `gripe_comment`. Never flips the gripe's `STATUS`. Params: `gripe_id`. **Off on prod** (the `diagnose_scan` minter pass is switched off by ruling, 2026-10-02); a hand-`put` diagnose job runs regardless. `PRECIS_DIAGNOSE_CLAUDE_MODEL` overrides the model (default: `Tier.BIG`, cheaper than `fix_gripe`'s FRONTIER). With `PRECIS_DIAGNOSE_AUTOPROMOTE=1` and confidence ≥ 0.8, the gripe is tagged `auto-fix`, now inert (the `fix_gripe` lane is off) — default off. |
| `plan_tick`         | `claude_inproc` | One planner-coroutine tick of a `meta.llm_tier`-set todo |
| `news_poll` / `briefing` | `claude_inproc` | News ingestion / daily briefing          |
| `draft_export`      | `claude_inproc` | Compile a draft to PDF/DOCX                   |
| `elsevier_abstract_backfill` | `claude_inproc` | Re-arm an operator-confirmed Elsevier preview cohort for re-fetch (stamps the existing `markup_refetch`/`oa_requeued` pins; bodies, hashes and events stay until a validated replacement ingests). Params: explicit `ref_ids`, `expected_count` (must equal `len(ref_ids)`), `dry_run` (default `true`). No provider or model call; never derives the cohort from body length. |
| `good_search`       | `coordinator`   | Deep paper-search campaign — normally minted for you by `search(kind='paper', q=…, good=True)`, not submitted by hand (see `precis-search-help`) |
| `good_search_triage`| `claude_inproc` | A `good_search` triage batch (internal — the campaign spawns these itself) |
| `conflict_sweep`    | `claude_inproc` | One claim hub hunts its opposition: negated-paraphrase ANN over the corpus, `paper_rank`-budgeted LLM verify, `disputes` edge on a confirmed contradicts, `meta.conflict_search` coverage ledger stamped. Minted for you at claim mint and by the approve page's freshness check (dark unless the `conflict_search` service is lit); by hand: params `hub_id` (the `fi<id>` ref_id), optional `refresh` (sweep even if already current — covered passages are still skipped). See `precis-taproot-help`. |

## Which job types run compute or code?
## Run a relax, a Pourbaix verdict or a sandbox build

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `struct_relax`      | `ssh_node`      | DFT/ML relax of a `structure` on a GPU node   |
| `pourbaix_bulk`     | `claude_inproc` | Bulk Pourbaix verdict (`dissolved` / `leached` / `transformed` / `oxidised` / `unmatched` / `stable`) for a candidate structure's host phase at a U/pH point and over a window, from Materials Project entries; written to the job's `meta.verdict`. Params: `candidate_ref`, `point` (`{U_RHE, pH}`), optional `window` (`{U_RHE: [lo, hi], pH: [lo, hi]}`), `ion_conc_M` (1e-6), `stability_tol` (0.1 eV/atom), `grid` (5). Fails `config` without `PRECIS_MP_API_KEY`. A bulk verdict is necessary, not sufficient: the surface can still restructure under bias. |
| `surface_coverage_scan` | `ssh_node`  | Surface-Pourbaix anchors + inner CHE sweep (slice 1): one MLIP model's ab-initio-thermodynamics coverage scan of a clean slab (catpath `coverage`: γ(θ) per adsorbate, default H/O/OH at 0.25–1 ML, (111) only), stamped with its footing (`meta.anchor_key`, `model`, `engine_version`, `corrections`), then pooled with every succeeded scan on the same `anchor_key` (one per model) into `meta.che_sweep`: the resting termination along U (V vs RHE), every boundary with `band_propagated` (anchor bars through the CHE; "add an anchor here") and `band_model_form` (spread across models; needs ≥2 scans) kept apart, `needs_anchor` ranked by the first. Params: `config` (a catpath config, same shape as the pathway lane; `coverage` block defaults filled), optional `model_index` (0), `force_backend`, `target_node` (the GPU node), `point_U_RHE`, `window_U_RHE` ([-1, 0.5]), `grid` (151), `pH` (7, labels the SHE view only), `sigma_e_eV` (default = `config.search.energy_thresh`). Refuses `slab_extxyz` (a doped or hydride slab cannot be scanned until the engine honours a prebuilt slab) and any facet other than (111). |
| `sandbox_run`       | `claude_docker` | Run an open-ended coding task (`mode:build`) or re-run a prior build's harvested tarball (`mode:run`, no claude/OAuth) in a throwaway, cgroup-capped container on an `agent_sandbox_host`. **Dark** — the `job_claude_docker` pass runs only where its `service_config` row (seeded from `agent_sandbox_hosts` group membership at deploy) has `prio>=1`; `precis service prio <host> job_claude_docker <n>` flips it live. A put on a host without it queues a job nothing claims. Params: `mode` (`build` default \| `run`), `prompt` (`mode:build`, required), `artifact` (`mode:run` — a prior build's harvested `folder` ref id, required), `precis_access` (`none` default \| `read` — `mode:build` only, a per-run token'd read-only MCP callback, needs `PRECIS_SANDBOX_READ_MCP=1`), `target_node` (a sandbox host, never melchior), `resources.wall_seconds`. `validate_submit` rejects an unsupported `mode`, `precis_access:read` without `PRECIS_SANDBOX_READ_MCP`, `secrets`, a non-sandbox target, `mode:build` with no `prompt`, `mode:run` with no `artifact`, and (`mode:build` only) a missing `CLAUDE_CODE_OAUTH_TOKEN`. Recurring `mode:run` (e.g. a dated pipeline) just wraps the same params in a `meta.schedule` todo — no special-casing, see `precis-recurring-help`. `image` (`code-task:<git-sha>`, default `code-task:latest` — the ansible play's movable tag; per-job override via `params.image`) is pinned in the launch argv and recorded as provenance in three places: the job's `meta.image`, the terminal `job_summary` text, and the harvest folder's `meta.image`. |

(More land as new modules under `precis/workers/job_types/`. See
the per-type recipe skills for invocation details.)

## Submit a job
## Enqueue an offline run
## Kick off an agent task

**Recommended: write the intent as a todo; the dispatch
worker mints the job.** (The `fix_gripe` examples below are
shape illustrations; that lane is OFF, so submit it only by hand.)

```python
# 1) Write the intent under whichever strategic it belongs to.
put(kind='todo',
    text='Fix gripe:42 (rate-limit edge case)',
    parent_id=engineering_hygiene_strategic_id,
    meta={'executor': 'claude_inproc',
          'job_type':  'fix_gripe',
          'params':    {}},
    # Dispatch auto-injects this if you omit it, but explicit is
    # tidier: when the child job succeeds, the parent flips done.
    tags=['STATUS:open'])

# 2) Add the link to whatever the job operates on (for fix_gripe,
#    the gripe).
link(kind='todo', id=<that_todo_id>, target='gripe:42', rel='fixes')

# 3) Walk away. The dispatch worker (in the default rotation) mints
#    the job under it on its next pass — minutes, not seconds (one SYS
#    cycle, 15-18 min observed; prio is ascending, lower = hotter, see
#    precis-minter-help). Poll the parent todo's
#    status if you want; the job lives under it.
```

**Ad-hoc (direct submit) — when you want to skip the intent layer:**

```python
put(kind='job',
    parent_id=<some_todo_id>,        # required — no orphan jobs
    job_type='fix_gripe',
    link='gripe:42', rel='fixes')
# → created job id=101
```

The handler validates `job_type`, `executor`, `params`, AND the
parent todo's existence at submit time. If something's wrong, the
`put` call fails immediately rather than queueing an unrunnable job.

Executors: `claude_inproc` (offline `claude -p` on the agent host —
the default, you usually don't set `executor=`), `ssh_node` (remote
GPU-node compute, e.g. `struct_relax`), and `coordinator`
(yield/resume phase machines that fan out child jobs, e.g.
`good_search`).

Parenting is polymorphic (ADR 0044): a job parents on a **todo**
(intent lane — rotation, `child-failed` bubble, `child_job_succeeded`),
on a **build subject** (`structure`/`cad`/`draft` — derived compute,
no todo needed), or on a **coordinator job** (campaign children,
spawned by the coordinator itself — never submit these by hand).

## Submit a job tied to a specific parent

The `link` + `rel` pair anchors the job. For `fix_gripe`:
`link='gripe:42', rel='fixes'`. Other job_types use their own
relations.

## Idempotent submit
## Re-submit safely

`idem_key` defaults to the link target (e.g. `gripe:42` for a
fix_gripe job), so a duplicate submit returns the same job id
while an earlier attempt is still queued or running. Once the
prior attempt is terminal (`STATUS:succeeded` / `failed` /
`cancelled`), a fresh attempt is created.

There is no auto-retry — failures stay failed until you ask for
another attempt.

## What jobs are running right now?
## Show me the active queue

```python
search(kind="job", tags=["STATUS:running"])
```

## Show me everything queued up

```python
search(kind="job", tags=["STATUS:queued"])
```

## Show me failed jobs
## Find jobs that need attention

```python
search(kind="job", tags=["STATUS:failed"])
```

## Show me a specific job
## How did this job go?

```python
get(kind="job", id=101)
# → header + current status + summary chunk (when finished) +
#   recent job_event chunks (telemetry, kept for forensics)
```

The `job_summary` chunk is the human-readable account ("Fix
landed on main at <remote> as abc123 (squash of branch gripe_42
onto def456), confirmed by ls-remote. 3 files changed, 47
insertions(+), 12 deletions(-). Took 84s."). Searchable through the
normal `search(kind='job', q=...)` surface.

`job_event` chunks (lease renewals, llm_output excerpts,
commit_made markers) are kept for forensics; default search
excludes them so they don't pollute results.

## What jobs have run on this gripe?
## History of fix attempts for a gripe

```python
search(kind="job", link="gripe:42")  # most recent first
```

## What jobs have run on this paper / ref / parent?

Same shape: `search(kind='job', link='<kind>:<id>')`.

## Cancel a running job
## Stop a job that's taking too long

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
# worker SIGTERMs at the next safe point; final tag is STATUS:cancelled
```

## Re-submit a failed job
## Retry a failed job
## Re-run a job with a different model

When a job fails, its parent todo gets `child-failed:<job_id>`
tagged. The doable view excludes parents with that tag so they
don't keep getting re-picked. The sweeper auto-unparks bounded
(see above), so manual retry is for *now* rather than *eventually*
— and mandatory only once `child-failed-final` is latched:

```python
# Option A (preferred): the retry verb. One call clears the bubble
# so the dispatch worker re-mints a fresh attempt on its next sweep
# (~1 min). The failed job stays for forensics. If the parent has
# also been terminally parked (child-failed-final latched by the
# sweeper), retry strips that latch too and resets the unpark budget
# in the same call — so this is the one recipe that unparks a
# child-failed-final leaf without knowing the tag names.
put(kind='job', id=<failed_job_id>, mode='retry')

# Change the model at the same time (opus | sonnet | haiku). This
# swaps the parent todo's meta.llm_tier before clearing the bubble,
# so the re-minted tick runs on the new tier. Only valid when the
# parent is an LLM-planner todo (already has meta.llm_tier set) —
# handy when a tick hit an AUP refusal or needs a stronger/cheaper
# model. The web Todo tab exposes the same thing as a "Retry" button
# (with a model dropdown) on failed job rows — turn on "+ show closed
# jobs" to see them.
put(kind='job', id=<failed_job_id>, mode='retry', model='sonnet')

# Retry rejects a job that isn't terminal (STATUS:failed/cancelled),
# an orphan job with no todo parent, and model= on a non-LLM parent.

# Option B (manual equivalent): clear the bubble by hand. A failed
# child is terminal, so it does NOT block re-mint — deleting it is
# optional cleanup, not required.
tag(kind='todo', id=<parent_id>, remove=[f'child-failed:{failed_job_id}'])
# If the leaf is terminally parked (has child-failed-final too), you
# MUST remove BOTH tags in ONE call — removing child-failed-final
# alone does not stick: the sweeper re-latches it on its next cycle
# while the child-failed:<job_id> tag is still open and
# unpark_attempts is at the cap. Prefer Option A, which also resets
# the unpark budget; the manual two-tag removal leaves unpark_attempts
# at the cap, so one more failure re-latches child-failed-final
# immediately.
tag(kind='todo', id=<parent_id>,
    remove=['child-failed-final', f'child-failed:{failed_job_id}'])
# (optionally) delete(kind='job', id=<failed_job_id>)
# Dispatch worker mints a fresh job on the next tick.

# Option C: different executor or job_type — edit the parent's meta
# first, then clear the bubble as above.
# (No direct meta-patch verb today; edit it via the runtime, or
# delete the parent todo and re-create with the new shape.)

# Option D: ask the user (asa-bot pattern).
put(kind='todo',
    parent_id=<parent_id>,
    text='Job #N failed with X — should I retry, switch executor, or skip?',
    tags=['ask-user'],
    meta={'auto_check': {
        'type': 'discord_reply_received',
        'ask_message_id': '<discord msg id>'}})
put(kind='message',
    target='discord/<guild>/<channel>/<thread>',
    text='Hey, parent #N needs your call: ...')
```

`idem_key` defaults to the link target so a stray duplicate submit
returns the in-flight job's id rather than queueing twice.

## Why didn't my job run?
## My put(kind='job', ...) was rejected — why?

Rejection reasons surfaced at `put` time, not later:

- **Missing `parent_id`** — every new job must
  declare its parent todo. The error names the canonical
  dispatch-from-todo pattern.
- **Bad `parent_id`** — the integer doesn't address a live
  `kind='todo'` ref.
- Unknown `job_type` — not in the registry.
- Executor doesn't list this `job_type` in its
  `COMPATIBLE_EXECUTORS` set.
- Executor host doesn't provide everything in the type's
  `REQUIRES` set.
- Bad `params` (jsonschema validation failure).

The error message names the missing piece. Catch it, fix it, re-
submit.

## What does each job_type require to run?

Each `job_type` module declares `PARAMS_SCHEMA`,
`COMPATIBLE_EXECUTORS`, `REQUIRES`, and a `DESCRIPTION`. The
per-type recipe skills (`precis-fix-gripe-help`, …) document the
shapes for the LLM-facing call.

## Status vocabulary

| Tag                       | Meaning                                |
|---------------------------|----------------------------------------|
| `STATUS:queued`           | Filed, waiting for a runner            |
| `STATUS:submitted`        | Handed to an external system (cluster) |
| `STATUS:running`          | A runner has claimed it                |
| `STATUS:succeeded`        | Finished cleanly; check `job_summary`  |
| `STATUS:failed`           | Exited without a usable result         |
| `STATUS:cancel_requested` | Cancellation in flight                 |
| `STATUS:cancelled`        | Runner stopped on cancel request       |

(`STATUS:submitted` is used only by future cluster executors;
`claude_inproc` goes straight queued → running. A `coordinator` job
additionally parks at `STATUS:waiting_children` / `waiting_time` /
`waiting_ask_user` / `waiting_manual_kick` between slices — that's a
legitimate pause, not a stall; the `wake_runner` re-queues it when
the wake condition fires.)

## A coordinator job yields a pending question — `STATUS:waiting_ask_user`
## Why is this job parked waiting on me?

A `coordinator` job_type (long-running campaigns like `good_search`)
can pause on a human decision instead of failing or blocking: its
dispatcher tags itself with an open `ask-user:<phase>:<slug>` pattern
and returns `Yield(state=<checkpoint>, wake_when=WakeWhen('tag_cleared',
{'tag': 'ask-user:<phase>:*'}))`. The executor checkpoints that state
into `meta.coordinator_state`, sets `STATUS:waiting_ask_user`, and
releases the runner slot — parked, not stuck. `wake_runner` polls for
the matching tag to disappear and re-queues the job to `STATUS:queued`
so the next slice resumes from the checkpoint.

Answer the pending question by clearing the matching tag on the job
(`tag(kind='job', id=42, remove=['ask-user:<phase>:<slug>'])`) once
you've decided. This is the job-level sibling of the todo-level
`ask-user`/`ask-user:<question>` open tag a dispatched agent tags on
its own todo to yield the same way — see `precis-todo-tree-help`,
`precis-decomposition-help` for that path.

## Read raw worker logs
## What is the cluster actually doing right now?
## Confirm a pass ran / failed without Bash

```python
get(kind='job', id='/logs')
# → last 24h, WARNING+, newest first — the default "what's wrong?" view

get(kind='job', id='/logs?handler=dispatch&since=24&level=WARNING')
# handler= takes the short pass name ('dispatch', 'embed', ...), the full
# dotted logger ('precis.workers.dispatch'), or the pass named in the
# runner's per-cycle `worker: <pass> claimed=N ok=N failed=N` INFO row
# (payload.handler) — so level=INFO shows a quiet pass's heartbeat too

get(kind='job', id='/logs?host=caspar&level=INFO&q=timeout&since=168&limit=50')
# host=, q= (substring on message), since= (hours, max 168),
# limit= (default 100, cap 200) all compose

get(kind='job', id='/logs?process=precis-worker-agentlane')
# process= is an exact match on the PRECIS_PROCESS env the LaunchDaemon
# plist sets (NULL unless set) — a host running several worker units
# (e.g. melchior's precis-worker, precis-worker-agentlane,
# precis-worker-drain-1, precis-worker-drain-2) needs this, host= alone
# can't tell them apart
```

Read-only view over the centralised `worker_logs` table (migration
0015) — the same table an operator reads via `precis logs` on the
CLI. Header line states the resolved filter + UTC cutoff; each row is
`ts host handler LEVEL message`; the trailer says `N rows shown of M
matching`. No queryable surface exists for this table anywhere else
in the agent surface (no Bash, no raw SQL) — this is it.

## Has a fix reached the fleet yet?

```python
get(kind='job', id='/builds')             # per host/process build, last 24h
get(kind='job', id='/builds?since=168')   # widen to a week
get(kind='job', id='/builds', args={'sha': '<fix commit>'})
# ...and does each build CONTAIN that commit? adds contains_sha=yes|no|unknown
```

Every claim stamps the claiming worker's `version@sha` on the job
(`meta.lease_code`, alongside `lease_host`/`lease_process`), so the jobs
table records what code actually ran where. Each row is
`host process version@sha jobs=N last=<UTC>`.

A fix is live on a process when that process's newest build sha is the fix
or a descendant of it. Do not judge that by eye: pass the fix's commit as
`args={'sha': ...}` (or `id='/builds?sha=...'`) and git answers per row —
`contains_sha=yes` (the build is that commit or descends from it), `no`
(it does not), or `unknown (<reason>)` when no checkout is reachable from
this process, the checkout has never fetched one of the two commits, or the
row carries no build sha. `unknown` is "cannot tell", never evidence either
way. Read per **process**, not per host: one host can run several worker
units, and an env or code difference between two units of the same host is
a common failure shape that per-host reading hides. Two builds for the same
host/process in the window is a restart boundary — read the newest.
`precis-status` answers a different question (the build serving *your own*
process, which in a container is not the fleet's).

## See also

- [[precis-gripe-help]] — the bug tracker
- [[precis-fix-gripe-help]] — fix_gripe recipe
- [[precis-search-help]] — find jobs by link/status
