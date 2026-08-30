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
status: active
---

# precis-minter-help — the intent → execution bridge

Wires the todo tree (intent) to the job substrate
(execution) via three pieces:

* **`meta.executor`** + **`meta.job_type`** on a `kind='todo'` ref —
  the "I want this run" marker.
* The **minter worker** (`precis worker --only minter`, in the
  default rotation; legacy name `dispatch`) — walks open todos with
  `meta.executor` set, mints a `kind='job'` child under each, leaves
  the existing executor pool to run it.
* The **`child_job_succeeded` auto_check evaluator** —
  auto-injected on the parent when the minter worker mints the
  job, so the parent flips `STATUS:done` when the job succeeds.

The minter worker is the **only sanctioned path** for minting new
jobs. Direct `put(kind='job', parent_id=N, ...)` still works
(documented in `precis-job-help`) but skips the auto_check
injection and the minter's logging — use it for one-off submits,
not for recurring intent.

## When do I set `meta.executor`?

| You're writing a todo that… | Set `meta.executor`? |
|---|---|
| …the owner / asa will work by hand | no |
| …needs an offline `claude -p` run on a repo | yes (`'claude_inproc'`) |
| …needs to wait for a paper to ingest | no — use `meta.auto_check={'type':'paper_ingested', ...}` |
| …is the umbrella of recurring scheduled work | no — `meta.schedule` set (see `precis-recurring-help`) |
| …is one tick of a recurring (spawned automatically) | usually inherited from the umbrella |

In short: `meta.executor` means **"this todo IS a thing the
minter worker should turn into a job."**

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
# 3) That's it. Within one minter tick (≤ 1 min) you'll see:
#    - a kind='job' child of todo.id with STATUS:queued
#    - meta.auto_check={'type':'child_job_succeeded'} on the todo
#    - a ref_events row on the todo: source='minter', event='job-minted'
```

You can verify by reading the todo:

```python
get(kind="todo", id=todo.id, view="tree")
# → todo + the spawned job under it (job rendered with ⚙ marker)
```

## What the minter does, step-by-step

1. **Candidate scan.** SQL: every open todo with `meta.executor`,
   no existing live `kind='job'` child, status in `open|doing`,
   not under a paused / recurring ancestor.
2. **Per-candidate claim.** `SELECT … FROM refs WHERE ref_id = …
   FOR UPDATE OF r SKIP LOCKED` so two minter workers (different
   hosts) serialise on the row.
3. **Validate.** Executor must be known (`is_known_executor`);
   job_type must exist; `job_type.compatible_executors` must
   include the chosen executor; `job_type.requires ⊆
   executor.provides`. Bad combinations are logged and skipped —
   the todo stays open, no zombie queued job lands.
4. **Auto-inject auto_check.** If `meta.auto_check` is absent,
   write `{'type': 'child_job_succeeded'}` into the parent's meta.
5. **Mint the child job.** `parent_id` = the todo; `meta` carries
   `job_type`, `executor`, `params`, `dispatched_from_todo`;
   `STATUS:queued` open tag.
6. **Append `ref_events`** on the parent: `source='minter',
   event='job-minted', payload={'job_id': N, ...}`.

Once the job is queued, the `job_claude_inproc` worker (also in
the default rotation) picks it up by `STATUS:queued`, runs the
executor, and flips status to succeeded / failed.

## What gets rejected at mint time?

The minter logs the rejection and moves on; the todo stays
open. The operator notices via worker logs, which carry structured
entries with the rejection reason.

| Cause | Log line |
|---|---|
| Unknown `meta.executor` value | `dispatch: parent #N has unknown meta.executor=...` |
| Missing `meta.job_type` | `dispatch: parent #N has missing meta.job_type` |
| Unknown `meta.job_type` | `dispatch: parent #N has unknown meta.job_type=...` |
| Executor / job_type mismatch | `dispatch: parent #N job_type=X incompatible with executor=Y` |
| Required capability missing | `dispatch: parent #N executor=X missing caps for Y: {...}` |

(Log lines still carry the `dispatch:` prefix — `workers/dispatch.py`
is the still-named module; `registry.py`'s `log_name="dispatch"`
keeps `worker_logs` attribution matched to it.)

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
# Minter worker mints a fresh kind='job' on the next tick because
# the "no existing child job" check now passes.

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

## Host capabilities (what an executor advertises)

Executors live in `src/precis/workers/executors/__init__.py`
(`EXECUTOR_PROVIDES`); job_types live in
`src/precis/workers/job_types/__init__.py` (the `_REGISTRY` +
lazy loaders). Each executor advertises the capabilities it
provides (e.g. `clones_dir`); a job_type's requirements must be a
subset at submit.

Executors today: `claude_inproc` (offline `claude -p`, provides
`{claude_bin, git, clones_dir, claude_config_mount}`), `ssh_node`
(remote GPU-node compute), and `coordinator` (yield/resume phase
machines — empty PROVIDES; the job_type's `dispatch` does the work in
slices and parks at `STATUS:waiting_*` between them).
Job_types pair with a compatible executor at submit; see the table
in `precis-job-help`.

## Running the minter

```sh
precis worker --only minter                # drain alone (debug / backfill)
precis worker                              # default cycle includes minter
precis worker --profile system             # explicit profile (default)
```

The pass is SQL-only and cheap — multi-host safe via `FOR UPDATE
OF r SKIP LOCKED` per candidate parent.

## See also

```python
get(kind="skill", id="precis-job-help")  # the kind='job' surface
get(kind="skill", id="precis-fix-gripe-help")  # the first concrete job_type
get(kind="skill", id="precis-auto-todo-help")  # the child_job_succeeded evaluator
get(kind="skill", id="precis-todo-tree-help")  # the todo tree shape
```
