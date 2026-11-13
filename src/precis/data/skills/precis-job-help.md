---
id: precis-job-help
family: work
title: precis — offline work, addressable
summary: offline-work substrate — submit, poll, cancel; parent-todo contract, dispatch an existing todo to a lane, status, event timeline
answers:
  - how do I dispatch an existing todo to a factory lane right now?
  - how do I submit an offline job tied to a todo?
  - how do jobs differ from a regular synchronous tool call?
  - how do I check what jobs are currently running or queued?
  - how do I cancel a job that's taking too long?
  - how do I submit a job idempotently so a retry doesn't double-run it?
  - does tagging a todo STATUS:doing dispatch it to a worker?
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

The satellites carry the detail this skill used to hold:
[[precis-job-types-help]] (the non-factory `job_type` registry),
[[precis-job-factory-help]] (dark-factory / PR lanes),
[[precis-job-release-help]] (release pass and release-fix lanes),
[[precis-job-ops-help]] (cancel, retry, reprioritise, operator
overrides, STATUS vocabulary), [[precis-job-find-help]] (poll the
queue, read a job's card and views), [[precis-job-observe-help]]
(compute job types + the `/…` fleet views).

## Dispatch an existing todo to a lane now
## Send this todo to the factory
## Run a todo on a worker lane

Two shapes. A `STATUS:` tag never dispatches anything — dispatch
reads `meta`, and the plan tick only picks up `STATUS:open` nodes
(the executor minter also reads `doing`). So the tag is a label,
not the trigger: set the meta, keep the leaf `STATUS:open`, and
drop any `waiting-for:*` / `halt` / `claimed-by:` park or live
`blocked-by` link first.

**A. Factory lane (`run_slice`) — a one-node plan.** Give the
todo `meta.op` and self-tag it `plan:<id>`; the `plan_dispatch`
tick (every few minutes) mints the job under it. Works on a todo
that already exists — `meta.op` is one of the plan keys `edit`
accepts:

```python
edit(kind='todo', id=<todo>,
     args={'meta': {'op': {'type': 'run_slice',
                           'params': {'lane': '<lane>'}}}})
tag(kind='todo', id=<todo>, add=['plan:<todo>'])
# → next plan_dispatch tick mints a run_slice job parented on it.
#   Omit 'params' (or 'lane') to let the tick pick the cheapest
#   fit lane. Verify: get(kind='todo', id=<todo>, view='tree').
```

`meta.op` can name ANY registered `job_type` — a bare name runs
`{type: name, params: {todo_id}}` — so this is also the one-off
door for a mechanical op on an existing todo. If the todo carries
`meta.llm_tier`, the dispatcher additionally mints a `plan_tick`
on it — clear it first (`edit(meta={'llm_tier': None})`) or
expect both jobs. A `run_slice` dispatch is a real build: it
launches the lane's vendor, writes `slice/<id>` and, on green,
lands it. For the usual submit
shape — build + pre-land reviewer — use a plan template instead
([[precis-factory-submit-help]]). A lane class with no enabled
offering, or a brief naming a deny path, files a coordinator ask
instead of minting.

**B. Executor lane (`meta.executor`) — for a todo you mint now.**
`meta.executor`/`meta.job_type`/`meta.params` are create-time
fields: the agent edit doors exclude `executor`/`job_type`
(`edit` takes plan keys and `params`; `tag(meta=)` takes
`llm_tier`/`llm_select`/`schedule`/`budget_usd`/…), so mint the
intent fresh — or use shape A, which covers existing todos.
To point an existing todo at the LLM planner instead of a lane,
`tag(kind='todo', id=N, meta={'llm_tier': 'opus'})` — the
dispatcher also picks up `llm_tier`-set leaves and mints a
`plan_tick` job (a tick on the todo itself, not a lane build).

```python
put(kind='todo', text='Diagnose gripe:42 — rate-limit edge case',
    meta={'executor': 'claude_inproc', 'job_type': 'diagnose_gripe'},
    args={'parent_id': <parent_todo>})
link(kind='todo', id=<new_todo>, target='gripe:42', rel='fixes')
# → the dispatch worker mints the job on its next pass — minutes,
#   not seconds (one SYS cycle, 15-18 min observed); see
#   precis-minter-help for the full contract.
```

## The contract every job submit must satisfy

* **Every new job must declare its parent todo.** A submit
  without one is refused at `put` time with a next hint. On the
  advertised `put` schema only the core keys are named — the job
  fields ride `args={...}`:

```python
put(kind='job', link='gripe:42', rel='fixes',
    args={'parent_id': <todo_id>, 'job_type': 'fix_gripe'})
```

  `parent_id`, `job_type`, `executor`, `params`, `idem_key`,
  `requires`, `prio`, `origin` all go inside `args=` (a bare
  top-level spelling still reaches the handler but returns a
  deprecation note — `args=` is the documented channel).
* **The canonical path is `meta.executor` on a todo + the
  dispatch worker** (recipe B above). Direct `put(kind='job')`
  works for ad-hoc submits but skips the auto_check injection —
  see `precis-minter-help` for the full pattern.
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
  retry verb or a single two-tag `tag(remove=…)`
  ([[precis-job-ops-help]]) — removing `child-failed-final` on its
  own does not stick.

## Why is my todo parked child-failed (child-failed-parked / child-failed-final)?

A content-class job failure tags the parent todo `child-failed:<job_id>`
(the "child-failed-parked" state). The sweeper's `unpark` phase retries it
on a 12 h / 24 h / 48 h cool-down, cap 3; only `child-failed-final` means
the substrate gave up. The full rules are the "A failed job tags its
parent" bullet above; the manual retry verb is `put(kind='job', id=N,
mode='retry')` — [[precis-job-ops-help]]. Infra-class failures
(`swept:claim-orphaned`, `infra:child-killed`) never park the parent — a
fresh attempt is minted instead.

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

## Submit a job
## Enqueue an offline run
## Kick off an agent task

**Recommended: write the intent as a todo; the dispatch
worker mints the job** (recipe B above — `meta.executor` +
`meta.job_type` at create time). Walk away; the job appears under
the todo on the next pass.

**Ad-hoc (direct submit) — when you want to skip the intent layer:**

```python
put(kind='job', link='gripe:42', rel='fixes',
    args={'parent_id': <some_todo_id>,   # required — no orphan jobs
          'job_type': 'fix_gripe'})
# → created job id=101
```

(The `fix_gripe` examples are shape illustrations; that lane is
OFF, so submit it only by hand.)

Executors: `claude_inproc` (offline `claude -p` on the agent host —
the default, you usually don't set `'executor'` in `args=`),
`ssh_node` (remote GPU-node compute, e.g. `struct_relax`),
`job_inproc` (deterministic in-proc work orders — `embed_batch`,
`derived_drain`, `pcb_*`, `datasheet_pull`, `paper_pull`), and
`coordinator` (yield/resume phase machines that fan out child jobs,
e.g. `good_search`). Each executor has a drain pass in the worker
rotation named after it — `job_claude_inproc`, `job_ssh_node`,
`job_inproc`, `job_coordinator` (one coordinator slice per pass) —
plus `minter` (mints `kind='job'` children under executor-bearing
todos), `sweeper` (fails claim-orphaned `STATUS:running` jobs),
`diagnose_scan` (mints `diagnose_gripe` jobs), and `materialize`
(mints `embed_batch` / `derived_drain` work orders above the
backlog bands). Those service names are what `--only`,
`service_config` rows and `worker_logs` handler fields show.

Parenting is polymorphic: a job parents on a **todo** (intent lane —
rotation, `child-failed` bubble, `child_job_succeeded`), on a
**build subject** (`structure`/`cad`/`draft` — derived compute,
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
`cancelled`), a fresh attempt is created. Set it explicitly in
`args=` (`args={'idem_key': '<key>'}`) when the default is wrong.

There is no auto-retry — failures stay failed until you ask for
another attempt.

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
- Params that type-check but can't run — a `job_type` may declare a
  put-time `validate(store, params)` check (e.g. `survey_extract`
  requires `measurands` to be live taxa); it refuses the put instead
  of queueing a job that fails at dispatch.

The error message names the missing piece. Catch it, fix it, re-
submit.

## What does each job_type require to run?

Each `job_type` module declares `PARAMS_SCHEMA`,
`COMPATIBLE_EXECUTORS`, `REQUIRES`, and a `DESCRIPTION`. The
per-type recipe skills (`precis-fix-gripe-help`, …) document the
shapes for the LLM-facing call. The registries live in the
satellites: [[precis-job-types-help]] (agent + internal lanes),
[[precis-job-factory-help]] (dark factory, PR
intake), [[precis-job-release-help]] (release pass +
release-fix), [[precis-job-observe-help]] (compute + code).

## See also

- [[precis-job-types-help]] — the non-factory `job_type` registry
- [[precis-job-factory-help]] — run_slice / land_slice / plan ticks / PR lanes
- [[precis-job-release-help]] — release pass / release-fix / release_poll lanes
- [[precis-job-ops-help]] — cancel, retry, reprioritise, wake, extend, claim.* overrides, STATUS vocabulary
- [[precis-job-find-help]] — poll the queue, read a job card, `under=`/`job_type=` listings
- [[precis-job-observe-help]] — compute-lane job types + the `/…` fleet views
- [[precis-minter-help]] — the `meta.executor` → job bridge in full
- [[precis-factory-submit-help]] — mint a whole plan (build + reviewer + land)
- [[precis-gripe-help]] — the bug tracker
- [[precis-fix-gripe-help]] — fix_gripe recipe
- [[precis-search-help]] — find jobs by link/status
