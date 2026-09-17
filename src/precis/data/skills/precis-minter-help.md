---
id: precis-minter-help
title: precis — the minter worker (todo intent → kind='job' execution)
summary: bridging intent to execution — meta.executor markers, minter worker, auto-injected resolution
answers:
  - how do I get a todo turned into a running job automatically?
  - when should I set meta.executor on a todo?
  - how does a parent todo know its child job succeeded?
  - what's the difference between the minter worker and putting a job directly?
applies-to: put (kind='todo' with meta.executor); the precis worker --only minter pass
tags: [workflow, troubleshooting]
kinds: [todo, job]
status: active
---

# precis-minter-help — the intent → execution bridge

Set `meta.executor` + `meta.job_type` on a `kind='todo'` to mean "turn this
into a job." A background pass mints a `kind='job'` child under any open
todo with `meta.executor` set and no live job child yet, and auto-injects
`meta.auto_check={'type': 'child_job_succeeded'}` on the parent when you
didn't set one, so it flips `STATUS:done` when the job succeeds.

Direct `put(kind="job", parent_id=N, ...)` still works (`precis-job-help`)
but skips the auto_check injection — use it for one-off submits, not
recurring intent.

## When do I set `meta.executor`?

| You're writing a todo that… | Set `meta.executor`? |
|---|---|
| …the owner / asa will work by hand | no |
| …needs an offline `claude -p` run on a repo | yes (`'claude_inproc'`) |
| …needs to wait for a paper to ingest | no — use `meta.auto_check={'type':'paper_ingested', ...}` |
| …is the umbrella of recurring scheduled work | no — `meta.schedule` set (see `precis-recurring-help`) |
| …is one tick of a recurring (spawned automatically) | usually inherited from the umbrella |

## Toolpath — write an intent, walk away

```python
# 1) Mint the intent under whichever strategic owns the work.
todo = put(
    kind="todo",
    text="Fix gripe:42 — rate-limit edge case",
    parent_id=engineering_hygiene_strategic_id,
    meta={"executor": "claude_inproc", "job_type": "fix_gripe"},
)
# 2) Link to whatever the job operates on, if anything.
link(kind="todo", id=todo.id, target="gripe:42", rel="fixes")
# 3) That's it. Within one tick you'll see:
#    - a kind='job' child of todo.id with STATUS:queued
#    - meta.auto_check={'type':'child_job_succeeded'} on the todo
```

Verify by reading the todo:

```python
get(kind="todo", id=todo.id, view="tree")
# → todo + the spawned job under it (job rendered with ⚙ marker)
```

## What gets rejected at mint time?

An unknown `meta.executor` / `meta.job_type`, or a job_type incompatible
with the chosen executor, is skipped — the todo stays open, no job mints.
Ask a human operator to check the worker log for the reason
(`docs/runbooks/minter-ops.md`).

## Toolpath — failed job, decide next move

A failed job tags the parent todo `child-failed:<job_id>` (the
failure bubble). The doable view excludes parents with that
tag, so they don't keep getting re-picked. asa-bot reads
`view='attention'` (see `precis-todo-tree-help`), sees the stuck
parent, decides:

```python
# Read the failure context.
parent = get(kind="todo", id=98)  # ancestry + tags
the_job = get(kind="job", id=143)  # status + job_event chunks
# the chunks tell you what claude saw before failing.

# Option A — same executor, fresh attempt.
tag(kind="todo", id=98, remove=["child-failed:143"])
delete(kind="job", id=143)
# A fresh kind='job' mints on the next tick because the "no existing
# child job" check now passes.

# Option B — switch executor (once we have more than claude_inproc).
# Edit the parent's meta.executor, then clear + delete as above.

# Option C — ask the owner.
ask = put(
    kind="todo",
    parent_id=98,
    text="Job jo143 failed with X — retry / switch / skip?",
    tags=["ask-user"],
    meta={
        "auto_check": {
            "type": "discord_reply_received",
            "ask_message_id": "<discord msg id>",
        }
    },
)
put(
    kind="message",
    target="discord/<guild>/<channel>/<thread>",
    text="Hey, td98 needs your call: ...",
)

# Option D — give up.
tag(kind="todo", id=98, add=["STATUS:won't-do"])
```

**The substrate does not auto-retry.** Every retry is a deliberate
move — asa-bot or human pulls the lever each time.

## The executor / job_type registry

Job_types pair with a compatible executor at submit — see the
compatibility table in `precis-job-help`. An incompatible pairing is
rejected at mint time (above), not silently coerced.

## See also

- [[precis-job-help]] — the kind='job' surface
- [[precis-fix-gripe-help]] — the first concrete job_type
- [[precis-auto-todo-help]] — the child_job_succeeded evaluator
- [[precis-todo-tree-help]] — the todo tree shape
