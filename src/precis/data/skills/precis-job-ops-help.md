---
id: precis-job-ops-help
family: work
title: precis — steer, cancel and unpark a job
summary: job lifecycle steering — cancel via tag, mint-time prio/origin classes, retarget a queued pinned job, claim.* operator overrides, extend a live job's wall budget, wake a waiting_time job, retry a failed job and clear child-failed parks; the STATUS: vocabulary and waiting_ask_user
answers:
  - how do I cancel a job that's taking too long?
  - how do I move a queued job to the front or reprioritise it?
  - how do I move a queued pinned job to another node?
  - how do I set or clear a claim.* operator override?
  - how do I extend a live job's wall budget or wake a waiting_time job?
  - how do I retry a failed job and clear its child-failed tag?
  - what job statuses are there and what does waiting_ask_user mean?
applies-to: put/tag (kind='job'); tag (kind='todo') for child-failed parks
tags: workflow, verbs, troubleshooting
kinds: job, todo
status: active
---

# precis-job-ops-help — steer, cancel and unpark a job

Lifecycle operations on a `kind='job'` ref once it exists: the
`tag`-driven cancel, mint-time `prio=`/`origin=` scheduling,
`mode='retarget'`/`'setting'`/`'extend'`/`'wake'`/`'retry'` modes,
sweeper guards, and the full `STATUS:` vocabulary including
`waiting_ask_user`. Submit shapes live in [[precis-job-help]];
reading the queue in [[precis-job-find-help]].

## Cancel a running job
## Stop a job that's taking too long

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
# worker SIGTERMs at the next safe point; final tag is STATUS:cancelled
# a still-queued (never claimed) job goes straight to STATUS:cancelled
# a parked job (STATUS:waiting_*) is cancelled where it sits by the wake runner
```

A detached `ssh_node` job whose process handle cannot be verified on its
node (missing launch identity — e.g. a handle minted before identity
stamping, gr572654) cannot be signalled: the row holds
`cancel_requested` while the plugin's poll follows the child to its real
outcome — a running `land_slice` ship still lands and notifies. Past
`meta.deadline` the row keeps following: the bound applies to the
compute, never to the record — a `cancelled` stamp while the ship ran on
is what dropped the land's post-steps (gr572654). Only a child proven
gone — its process slot recycled or the host rebooted — cancels after
the poll finds no outcome to record. The remote process is never stopped
by tagging alone.

Agents drive only these `tag` edges: queued|yielded ->
cancel_requested|cancelled;
running/submitted/waiting_* -> cancel_requested|failed; cancel_requested ->
cancelled. Any other STATUS (running, succeeded, queued) is
worker-owned and refused; re-run a failed job with `mode='retry'`.
Under a `plan:<id>` node the tick re-queues a cancelled job free —
`run_slice` and allowlisted read-only ops only (`ci_verdict_poll`); a
cancelled `land_slice`/`release_*`/`pr_action` waits for the rescue
pass. Stop the node: `halt:`/`waiting-for:`, `STATUS:paused`, won't-do.

## Queue behind interactive work — prio= / origin=

Claim order is **class, then priority, then time queued** — the same
`(class_rank, aged_prio, queued_since)` key the `queue:` lines on a
queued job report. Two mint-time knobs (both ride `args=` on the
advertised `put` schema):

```python
put(kind='job',
    args={'parent_id': <todo>, 'job_type': 'survey_extract',
          'origin': 'background',   # meta.origin scheduling class:
                                    # human > agent > recurring > background
          'prio': 8})               # refs.prio, 1..10 — lower = hotter (NULL reads 5)
```

* `origin=` overrides the context-derived class. An agent's put derives
  `agent` by default — batch work it fans out sits *in the interactive
  band*, ahead of every real background job. Pass `origin='background'`
  so interactive work always claims first. `origin='human'` is refused
  (authenticated web writes only), and a job parented on a coordinator
  job inherits the parent's class — a conflicting `origin=` refuses.
* `prio=` writes the `refs.prio` column (the 0014 scale: 1 preempts
  across *all* classes, 2 is the cron band, 5 the default, 8 commodity
  background). Inside a class, an aged job gains one prio band per
  ~10 min queued, so a high-number prio still can't starve — it just
  claims after fresher work first.
* Reprioritise a live job with `tag(kind='job', id=N, prio=M)` — works
  on any non-terminal job (queued, running, `waiting_*`; the write
  lands on the row's next claim/re-queue). A terminal job refuses:
  nothing claims it again. `prio=`/`origin=` on `mode='retry'` /
  `mode='retarget'` / `mode='setting'` / `mode='wake'` /
  `mode='extend'` are refused — those verbs don't mint.

## Move a queued pinned job to another node

A queued job pinned by `params.target_node` waits for that node even when
another is idle. Move the pin (queued jobs only):

```python
put(kind='job', id=<queued_job_id>, mode='retarget',
    args={'params': {'target_node': '<host>'}}, text='<why>')
```

It refuses a job that is not `STATUS:queued` or not pinned, a node whose
executor lane is retired, and a node that doesn't advertise every resource
the job requires. `text=` is required; the old and new node, the reason and
the actor land in a `job-retarget` event on the job.

## Set or clear a claim.* operator override

The claim substrate reads `claim.*` `app_state` keys live:
`claim.max_running.<job_type>` (per-host running cap, int 0..64 — `0`
suspends the type), `claim.slicing.<executor>` (bool),
`claim.slice_s.<executor>` (positive seconds),
`claim.executor_cap.<executor>` (per-host running cap for the whole
executor — `ssh_node` defaults to 8, absent = uncapped) and
`claim.reserve.<executor>` (JSON `{"<job_type>": <slots>}` of slots
inside the cap held back for those job types — a compute may not take
the last reserved slots; `ssh_node` defaults to
`{"run_slice": 1, "land_slice": 1}`, gr603792). Set or clear them with
the audited setter — `reason=` is required and the change (who, old →
new, why) lands in `worker_logs`:

```python
put(kind='job', mode='setting',
    args={'key': 'claim.max_running.derived_drain', 'value': 4,
          'reason': '<why>'})
put(kind='job', mode='setting',
    args={'key': 'claim.slicing.job_inproc', 'unset': True,
          'reason': '<why>'})
```

The key must name a real job_type / executor and the value the family
type. Writes are CLI-only: only the `cli` source may set or unset.
Web sources, every worker source (`precis-worker*`, `asa-*`), and calls
inside a background pass are denied.
Read the live overrides next to their code defaults with
`get(kind='job', id='/claim')` ([[precis-job-observe-help]]).

## Sweeper guards — unschedulable, off-lane pins, control starvation

The `sweeper` also guards executability: a `STATUS:queued` job whose
capability requirements no live host advertises in `resource_slots`
(and no `params.target_node` pin falls back on) can never be placed —
it raises `scheduler/unschedulable:<job ref>` at warn, auto-resolved
when the job leaves the queue or the capability is advertised again.
A job pinned by `params.target_node` to a host that still heartbeats
but no longer runs the executor's drain pass (it left the lane: ran
`job_<executor>` this week, silent past 4h, nothing leased there) fails
as infra, tagged `reaped:off-lane-pin`, with `meta.error` naming the
node; `sweeper/off-lane-pin:<node>/<executor>` opens at warn and
resolves when the pass runs on that node again. The sweeper also
watches for lane starvation: a control-class row — origin
`human`/`agent`/`recurring`, a release-band job_type, or unstamped
prio ≤ 2 — claimable on `job_inproc` for over
`PRECIS_CONTROL_STARVED_MINUTES` (default 15m) opens
`jobstarve/control:<executor>` at warn, auto-resolved when nothing
control-class waits past the bound (gr592211: a ~50-deep
`derived_drain` backlog occupied every in-proc slot, so `release_poll`
and `factory_rescue_tick` queued a full drain per turnover). The fix is
the per-host `claim.max_running.<job_type>` cap — `derived_drain`
defaults to 2 running per host — so the alert means the cap isn't
holding (check `get(kind='job', id='/claim')` and the host's claimants)
or the lane is otherwise wedged.

## Extend a live job's wall budget and deadline
## Give a running job more time without restarting it

An active job's kill bound is `meta.deadline` (stamped at submit/launch
from `params.resources.wall_seconds` + the executor margin). To give a
live job more time **without resetting elapsed time or restarting the
compute**, extend both stored fields in place — one job, or every live
job under a tree (`under=` resolves the same member+link set as
`search(kind='job', under=…)`):

```python
put(kind='job', id=<job>, mode='extend',
    args={'seconds': 86400, 'reason': '<why>'})
put(kind='job', mode='extend',
    args={'under': 'qu164903', 'job_type': 'autocatpath_seed',
          'seconds': 86400, 'reason': '<why>'})
```

Each call adds `seconds` (1..604800 — a week; a bigger push takes a
second audited call) to the stored `resources.wall_seconds` and to the
existing `meta.deadline` — `old + seconds`, never `now + budget`, so
the deadline stays anchored at its original submit origin. `reason=` is
required; the actor, reason and old→new values land in a `job-extend`
event on each job. A terminal job refuses (`id=`) or is skipped and
named (`under=`); a live job with neither field has nothing to extend.
`under=` touches at most 64 live jobs per call — narrow with
`job_type=` — and refuses outright when the member/job resolution
itself truncates rather than extending a silently incomplete set
(narrow the root or use `job_type=`). The response lists every job's
old→new `wall_seconds` and
`deadline`; both fields also ride the `get(kind='job', id=N)` status
card as `resources.wall_seconds:` / `deadline:`.

## Wake a time-parked job now

`put(kind='job', id=N, mode='wake', args={'reason': '<why>'})` re-queues a
`STATUS:waiting_time` job immediately, clearing its stale lease and recording
the caller and reason in a job event.
It refuses other statuses and mint arguments; use `mode='retry'` for a failed job.
Waits whose `wake_when.payload.why` contains `quota` or `pause` (case-insensitive)
are protected unless you explicitly pass `args={'force': True,
'reason': '<why>'}`.

## Re-submit a failed job
## Retry a failed job
## Re-run a job with a different model

When a job fails, its parent todo gets `child-failed:<job_id>`
tagged. The doable view excludes parents with that tag so they
don't keep getting re-picked. The sweeper auto-unparks bounded
(see [[precis-job-help]] §The contract), so manual retry is for
*now* rather than *eventually*
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
put(kind='job', id=<failed_job_id>, mode='retry',
    args={'model': 'sonnet'})

# The parent's meta.llm_select pin (placement/thinking/effort/
# temperature) survives a plain retry — the ack names it when it
# does. A surviving placement='local' pin re-mints the same failure;
# override it in the same call:
put(kind='job', id=<failed_job_id>, mode='retry',
    args={'placement': 'cloud'})
# select={...} is the full-knob form; placement= is its one-key
# shorthand and wins the placement key when both are given.

# Retry rejects a job that isn't terminal (STATUS:failed/cancelled),
# an orphan job with no todo parent, and model=/select=/placement=
# on a non-LLM parent.

# Option B (manual equivalent): clear the bubble by hand. A failed
# child is terminal, so it does NOT block re-mint — deleting it is
# optional cleanup, not required.
tag(kind='todo', id=<parent_id>, remove=['child-failed:<failed_job_id>'])
# If the leaf is terminally parked (has child-failed-final too), you
# MUST remove BOTH tags in ONE call — removing child-failed-final
# alone does not stick: the sweeper re-latches it on its next cycle
# while the child-failed:<job_id> tag is still open and
# unpark_attempts is at the cap. Prefer Option A, which also resets
# the unpark budget; the manual two-tag removal leaves unpark_attempts
# at the cap, so one more failure re-latches child-failed-final
# immediately.
tag(kind='todo', id=<parent_id>,
    remove=['child-failed-final', 'child-failed:<failed_job_id>'])
# (optionally) delete(kind='job', id=<failed_job_id>)
# Dispatch worker mints a fresh job on the next tick.
```

`idem_key` defaults to the link target so a stray duplicate submit
returns the in-flight job's id rather than queueing twice.

## Status vocabulary

| Tag                       | Meaning                                |
|---------------------------|----------------------------------------|
| `STATUS:queued`           | Filed, waiting for a runner            |
| `STATUS:yielded`          | Sliced job released mid-run; claimable |
| `STATUS:submitted`        | Handed to an external system (cluster) |
| `STATUS:running`          | A runner has claimed it                |
| `STATUS:succeeded`        | Finished cleanly; check `job_summary`  |
| `STATUS:failed`           | Exited without a usable result         |
| `STATUS:timed-out`        | Killed at its wall-clock deadline with the job type's timed-out verdict (`land_slice`); the plan tick parks the slice at the re-land ask |
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

## See also

- [[precis-job-help]] — submit shape, dispatch recipes, failure-park contract
- [[precis-job-find-help]] — see the queue and read a job card
- [[precis-job-observe-help]] — `/jobs -v` placement reasons, `/settings`, `/claim`, the fleet views
- [[precis-todo-help]] — `child_failed` ledger, `waiting-for:` parks, leaf lifecycle
- [[precis-minter-help]] — how executor todos mint jobs
