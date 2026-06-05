---
id: precis-job-help
title: precis — offline work, addressable
applies-to: get/search/put/tag (kind='job')
status: active
---

# precis-job-help — submit a job, poll for status, cancel

A **job** in precis is one offline run of something — fix this
gripe, run a simulation, benchmark a commit. Each has a numeric
id, a `STATUS:` tag, a parent it's working on (via `link`), and a
comment timeline. Address by numeric id — both `id=101` and
`id='job:101'` are accepted.

Submit. Walk away. Come back to a `STATUS:succeeded` row.

Not cron. Not celery. Not a subprocess you wait on.

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

```python
put(kind='job',
    job_type='fix_gripe',
    link='gripe:42', rel='fixes')
# → created job id=101
```

The dispatcher validates `job_type`, `executor`, and `params` at
submit time. If something's wrong (unknown type, incompatible
executor, missing required env, bad params), the `put` call
fails immediately rather than queueing an unrunnable job.

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

```python
put(kind='job', job_type='fix_gripe', link='gripe:42', rel='fixes')
```

If a prior attempt is still queued/running, you get its id back
(idempotency). If it's terminal, a fresh attempt is created.

## Why didn't my job run?
## My put(kind='job', ...) was rejected — why?

Rejection reasons surfaced at `put` time, not later:

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
