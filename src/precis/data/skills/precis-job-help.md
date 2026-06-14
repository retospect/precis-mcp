---
id: precis-job-help
title: precis — offline work, addressable
applies-to: get/search/put/tag (kind='job')
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

## Slice-5 contract (the important bit)

* **Every new job must declare its parent todo.** `put(kind='job',
  parent_id=<todo_id>, ...)` is the only legal shape. The handler
  rejects orphan submits with a clear next hint.
* **The canonical path is `meta.executor` on a todo + the
  dispatch worker.** Write the intent as a todo with
  `meta={'executor': ..., 'job_type': ...}`; the dispatch worker
  mints the job under it. Direct `put(kind='job', parent_id=N,
  ...)` works for ad-hoc submits but skips the auto_check
  injection — see `precis-dispatch-help` for the full pattern.
* **A failed job tags its parent.** When a job hits
  `STATUS:failed`, the parent todo gets an open tag
  `child-failed:<job_id>`. The nursery digest surfaces this; the
  parent's owner decides next move (retry / switch executor /
  ask user). **The substrate does NOT auto-retry.**

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

| `job_type`   | What it does                                        |
|--------------|-----------------------------------------------------|
| `fix_gripe`  | Prepare a candidate fix branch for a gripe          |

(More land as new modules under `precis/workers/job_types/`. See
the per-type recipe skills for invocation details.)

## Submit a job
## Enqueue an offline run
## Kick off an agent task

**Recommended (Slice 5): write the intent as a todo; the dispatch
worker mints the job.**

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
#    the job under it within one tick. Poll the parent todo's
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

For v1 there is only one executor (`claude_inproc`); it's the
default — you don't need to set `executor=`.

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
search(kind='job', tags=['STATUS:running'])
```

## Show me everything queued up

```python
search(kind='job', tags=['STATUS:queued'])
```

## Show me failed jobs
## Find jobs that need attention

```python
search(kind='job', tags=['STATUS:failed'])
```

## Show me a specific job
## How did this job go?

```python
get(kind='job', id=101)
# → header + current status + summary chunk (when finished) +
#   recent job_event chunks (telemetry, kept for forensics)
```

The `job_summary` chunk is the human-readable account ("Fix
attempt pushed to origin as branch gripe_42 @ abc123. Diff
+47/-12 across 3 files. Took 84s."). Searchable through the
normal `search(kind='job', q=...)` surface.

`job_event` chunks (lease renewals, llm_output excerpts,
commit_made markers) are kept for forensics; default search
excludes them so they don't pollute results.

## What jobs have run on this gripe?
## History of fix attempts for a gripe

```python
search(kind='job', link='gripe:42')   # most recent first
```

## What jobs have run on this paper / ref / parent?

Same shape: `search(kind='job', link='<kind>:<id>')`.

## Cancel a running job
## Stop a job that's taking too long

```python
tag(kind='job', id=101, add=['STATUS:cancel_requested'])
# worker SIGTERMs at the next safe point; final tag is STATUS:cancelled
```

## Re-submit a failed job

When a job fails, its parent todo gets `child-failed:<job_id>`
tagged. The doable view excludes parents with that tag so they
don't keep getting re-picked. The retry decision belongs to the
parent's owner:

```python
# Option A: same executor, fresh attempt.
# Clear the bubble flag + delete (or won't-do) the failed job so
# the dispatch worker's "no existing child job" check passes again.
tag(kind='todo', id=<parent_id>, remove=[f'child-failed:{failed_job_id}'])
delete(kind='job', id=<failed_job_id>)
# Dispatch worker mints a fresh job on the next tick.

# Option B: different executor or job_type — edit the parent's meta
# first, then clear + delete as above.
# (No direct meta-patch verb today; edit it via the runtime, or
# delete the parent todo and re-create with the new shape.)

# Option C: ask the user (asa-bot pattern).
put(kind='todo',
    parent_id=<parent_id>,
    text='Job #N failed with X — should I retry, switch executor, or skip?',
    tags=['asking-reto'],
    meta={'auto_check': {
        'type': 'discord_reply_received',
        'ask_message_id': '<discord msg id>'}})
put(kind='message',
    target='discord/<guild>/<channel>/<thread>',
    text='Hey reto, parent #N needs your call: ...')
```

`idem_key` defaults to the link target so a stray duplicate submit
returns the in-flight job's id rather than queueing twice.

## Why didn't my job run?
## My put(kind='job', ...) was rejected — why?

Rejection reasons surfaced at `put` time, not later:

- **Missing `parent_id`** — Slice-5 requires every new job to
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
v1's `claude_inproc` goes straight queued → running.)

## See also

```python
get(kind='skill', id='precis-gripe-help')         # the bug tracker
get(kind='skill', id='precis-fix-gripe-help')     # fix_gripe recipe
get(kind='skill', id='precis-search-help')        # find jobs by link/status
```
