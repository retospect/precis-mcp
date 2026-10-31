---
id: precis-job-help
family: work
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
| `diagnose_gripe`    | `claude_inproc` | Read-only root-cause diagnosis of a gripe — clones the repo (never branches/commits/pushes), asks claude for a structured `DIAGNOSIS:` block (root cause + evidence + a proposed-fix sketch + `Confidence: 0.NN`), and appends it as one `DIAGNOSIS (auto, job <id>):` `gripe_comment`. Never flips the gripe's `STATUS`. Params: `gripe_id`. **Off on prod** (the `diagnose_scan` minter pass is switched off by ruling, 2026-10-02); a hand-`put` diagnose job runs regardless. `PRECIS_DIAGNOSE_CLAUDE_MODEL` overrides the model (default: `Tier.BIG`, cheaper than `fix_gripe`'s FRONTIER). When auto-promote is enabled and confidence ≥ 0.8, the gripe is tagged `auto-fix`, now inert (the `fix_gripe` lane is off) — default off. |
| `plan_tick`         | `claude_inproc` | One planner-coroutine tick of a `meta.llm_tier`-set todo |
| `news_poll` / `briefing` | `claude_inproc` | News ingestion / daily briefing          |
| `money_refresh`     | `claude_inproc` | Refresh the `fx_rates` / `price_index` caches from the ECB and World Bank (deterministic; driven by a weekly recurring todo — minted by ops, never by hand) |
| `draft_export`      | `claude_inproc` | Compile a draft to PDF/DOCX                   |
| `elsevier_abstract_backfill` | `claude_inproc` | Re-arm an operator-confirmed Elsevier preview cohort for re-fetch (stamps the existing `markup_refetch`/`oa_requeued` pins; bodies, hashes and events stay until a validated replacement ingests). Params: explicit `ref_ids`, `expected_count` (must equal `len(ref_ids)`), `dry_run` (default `true`). No provider or model call; never derives the cohort from body length. |
| `good_search`       | `coordinator`   | Deep paper-search campaign — normally minted for you by `search(kind='paper', q=…, good=True)`, not submitted by hand (see `precis-search-help`) |
| `good_search_triage`| `claude_inproc` | A `good_search` triage batch (internal — the campaign spawns these itself) |
| `conflict_sweep`    | `claude_inproc` | One claim hub hunts its opposition: negated-paraphrase ANN over the corpus, `paper_rank`-budgeted LLM verify, `disputes` edge on a confirmed contradicts, `meta.conflict_search` coverage ledger stamped. Minted for you at claim mint and by the approve page's freshness check (dark unless the `conflict_search` service is lit); by hand: params `hub_id` (the `fi<id>` ref_id), optional `refresh` (sweep even if already current — covered passages are still skipped). See `precis-taproot-help`. |
| `deferred_llm_call` | `coordinator`   | A synchronous surface's LLM call run later, after a Claude quota or budget window (internal — the web follow-up mints it when the model is paused; it sleeps until `retry_at`, then appends the real answer to the conv) |

## More registered job_types — the rest of the internal lanes

| `job_type`          | Executor        | What it does                                  |
|---------------------|-----------------|-----------------------------------------------|
| `doctor_tick`       | `claude_inproc` | One LLM "doctor" pass over the fleet's own published health surfaces — the self-healing spine's Layer-3 judgment: gathers, classifies by ratio, diagnoses, authors the daily report, files/annotates gripes (internal; see `precis-health-digest-help`) |
| `quest_tick`        | `coordinator`   | One slice of a quest's perpetual catalyst loop (harvest → propose → dispatch → wait) — internal, minted by `quest_loop_reconcile` (see `precis-quest-help`) |
| `reground_claim`    | `claude_inproc` | Reground a taproot claim hub — audit/prune proxy evidence, re-discover deeper primaries, add-first apply (checkpointed); `mode='verify'` diffs intent vs committed (see `precis-taproot-help`) |
| `draft_refresh`     | `claude_inproc` | Bounded, section-scoped refresh of a living draft — critique + rewrite against corpus + research-arm evidence, growth-gated apply |
| `cad_discuss`       | `claude_inproc` | Discuss a cad design (tool-less claude, threaded, read-only) — answers questions, proposes nothing (see `precis-cad-help`) |
| `diagram_propose`   | `claude_inproc` | One figure/mermaid draw-with-me turn — builds/verifies the diagram from seed chunks, reconciles node→chunk bindings (see `precis-mermaid-help`) |
| `structure_propose` | `claude_inproc` | Turn an instruction into proposed structure ops (tool-less claude, propose-only — a human applies them) |
| `reading_brief`     | `claude_inproc` | Compose the morning reading-brief cast as a dated draft (see `precis-audio-help`) |
| `remarkable_send`   | `claude_inproc` | Export a draft in reMarkable mode and upload the PDF to the tablet — siblings `remarkable_papers_send` (cited-source PDFs) and `remarkable_reading_send` (per-source reading edition) |
| `embed_batch`       | `job_inproc`    | Bounded work order draining the derived embed queue — minted by the `materialize` cadence above the backlog high-water mark |
| `derived_drain`     | `job_inproc`    | Bounded work order draining a SMALL-tier derived LLM queue (summarize/classify) — cloud-only lane |
| `run_slice`         | `ssh_node`      | Dark factory: launch one slice (a todo with a brief) headless on a lane — `claude -p` in worktree `slice-td<id>` cut from `origin/main`; `meta.lane`/`model`/`effort` on the job; the session's last message lands as a `handoff:` comment on the todo; a failure returns the todo to `STATUS:open` with `attempt:<n>`. Params `todo_id`, optional `lane`, `model`, `effort`, `target_node`. Refused while the factory kill switch is on — ask a human operator to clear it (recipe: `docs/runbooks/dark-factory-arming.md`) |
| `plan_dispatch`     | `job_inproc`    | Dark factory: one pass over every plan subtree (`plan:<id>` tags, gate/loop/generate/auto_close nodes) — evaluate gates, re-mint loop bodies, close finished phases, mint one job per doable task (`meta.op`, default `run_slice`), release claims older than 4 h, file an `ask-user` + `showstopper` todo when a task hits `meta.retries` (default 2). Params: optional `dry_run`. Read-only while the factory kill switch is on or a `service_config` `plan_dispatch` row has prio 0. Armed by one recurring todo — ask a human operator to arm or disarm it (recipe: `docs/runbooks/dark-factory-arming.md`) |

## Which job types run compute or code? — [[precis-job-observe-help]]

The compute-lane job types (`struct_relax`, `pourbaix_bulk`,
`surface_coverage_scan`, `sandbox_run`, the pathway seed/aggregate
fan-out, pcb/retrosynth lanes) — executors, params, the
`surface_anchors` quest flag and the named-list scan recipe — live in
[[precis-job-observe-help]].

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
GPU-node compute, e.g. `struct_relax`), `job_inproc` (deterministic
in-proc work orders — `embed_batch`, `derived_drain`, `pcb_*`,
`datasheet_pull`, `paper_pull`), and `coordinator`
(yield/resume phase machines that fan out child jobs, e.g.
`good_search`). Each executor has a drain pass in the worker rotation
named after it — `job_claude_inproc`, `job_ssh_node`, `job_inproc`,
`job_coordinator` (one coordinator slice per pass) — plus `minter`
(mints `kind='job'` children under executor-bearing todos), `sweeper`
(fails claim-orphaned `STATUS:running` jobs), `diagnose_scan` (mints
`diagnose_gripe` jobs), and `materialize` (mints `embed_batch` /
`derived_drain` work orders above the backlog bands). Those service
names are what `--only`, `service_config` rows and `worker_logs`
handler fields show.

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
resolves when the pass runs on that node again.

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

## How long did a job run, and where?

Every terminal `STATUS:` (succeeded, failed, cancelled, timeout kill) stamps `meta.finished_at` (UTC), `meta.wall_s` (finished minus first claim; 0 if it never ran) and `meta.node` (the pinned `target_node`, else the claiming host). `get(kind="job", id=N)` prints them. A seed's compute dir is kept until that stamp lands.

## Show me everything queued up

```python
search(kind="job", tags=["STATUS:queued"])
```

`get(kind="job", id=N)` on a queued job adds `queue:` lines: its origin
class, its position in the executor's claim order (class, then priority,
then time queued) with the per-class count of rows ahead, what is running
on that executor now, and holds it can see (operator suspension, a
`target_node` pin, a live lease).

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
get(id="jo101")  # the job's universal handle resolves the same way
```

A `STATUS:failed` card leads with `error:` — the one-line failure
reason (`meta.error`, else the newest `job_event` head; `retry_after:`
beside it when a transient backoff was stamped) — printed right under
`status:` so the card cap can't cut it on jobs with long event
timelines. The full reason stays in the `job_event` chunks below.

The `job_summary` chunk is the human-readable account ("Fix
landed on main at <remote> as abc123 (squash of gripe_42 onto
def456), confirmed by ls-remote. 3 files changed, 47 insertions(+),
12 deletions(-). Took 84s."), searchable via `search(kind='job', q=...)`.
`job_event` chunks (lease renewals, llm_output excerpts,
commit_made markers) are forensic; default search excludes them.
A `job_result` chunk (a job's structured deliverable — a ranking or
proposal JSON) shows as a capped head on the card;
`get(kind='job', id=<job>, view='result')` returns it in full.
`view='transcript'` is the compact forensic read (one line per tool
call + the final report); a paged full-transcript view is not
implemented — `view='raw'` is the only uncapped transcript read today.

## A finished topo pcb job's run files — view='run-files'

A finished `placer='topo'` `pcb_place` job keeps its run artifacts
(`summary.txt`, `manifest.json`, `elites.json`, `pairs.json`,
`pins.json`, `placement.svg`) and the route-stage meta on its own
`refs.meta.topo_run`. Read them through the view — never by digging
`refs.meta` paths by hand (gr524274):

```python
get(kind='job', id=<job>, view='run-files')
# → file names + sizes, the summary text, the route meta
get(kind='job', id=<job>, view='run-files', args={'file': 'manifest.json'})
# → one file's content (JSON sorted, text/SVG verbatim, capped with a note)
```

An unknown `file=` refuses listing the available names; a job with
no `topo_run` gets a clear empty answer. For the board-side drill-down
(run pick, diffs) see `precis-pcb-topo-help` (`view='topo'`).

## What jobs have run on this gripe?
## History of fix attempts for a gripe

```python
search(kind="job", link="gripe:42")  # most recent first
```

## What jobs have run on this paper / ref / parent?

Same shape: `search(kind='job', link='<kind>:<id>')`.

## Which jobs belong to this quest / todo / board / structure?

`under=` scopes by the parent_id tree AND subject links — jobs under a
quest's todo tree, a coordinator's children, a board's build jobs.
`view='tree'` renders the hierarchy, `depth=`/`status=`/`since=` narrow:

```python
search(kind="job", under="qu459585")                # jobs beneath it
search(kind="job", under="qu459585", view="tree")   # the skeleton
search(kind="job", under="qu459585", view="count",
       args={"group_by": "meta.job_type,state"})
search(kind="job", under="qu459585", status="failed", depth=3)
```

The answer for "is the queue healthy *here*?" — never raw `refs`/`links`
SQL. `link=` stays right for jobs pointing AT a ref; `under=` is the
hierarchy question.

## Jobs of one type — how many ran, finished, failed?

`args={'job_type': ...}` filters on `meta.job_type` — listing, count, and
`under=` all honour it, and `view='count'` + `group_by='state'` gives the
status facets ("minted/finished/failed since <t>"):

```python
search(kind="job", args={"job_type": "graph_garden"})                 # list, recency
search(kind="job", args={"since": "24"})                              # minted last 24h, any type
search(kind="job", args={"job_type": "graph_garden", "since": "48h"}) # one type, age spelling
search(kind="job", view="count",
       args={"job_type": "graph_garden", "group_by": "state"})        # counts by status
search(kind="job", view="count", status="*",
       args={"job_type": "graph_garden", "since": "2026-10-20",
             "group_by": "state"})                                    # in a window
search(kind="job", under="qu459585", args={"job_type": "graph_garden"})
```

`status=` narrows to one STATUS; `since=`/`until=` bound `created_at`
(mint time) — an ISO timestamp or an age (`'24'`/`'6h'`/`'7d'` =
hours/days ago). A window with no `job_type=` and no view lists jobs
of every type minted in it — "what ran in the last day". `q=` is
title-lexical on this path; `sort=`/`mode=` are rejected — they belong
to the unfiltered ranked path. This is the MCP answer for what used
to need `refs.meta->>'job_type'` SQL.

Top-level `since=`/`until=` also work on the queryless plain listing —
`search(kind='job', since='2026-10-20', status='*')` is the same
recency-ordered listing without a `job_type=` filter (adding `q=` makes
it a ranked search and `sort=`/`mode=` legal again).

`view='count'` facet rows also name each group's newest/oldest member
(`jo<N>` + created bounds) — "when did the last <type> run, how far
back does the streak reach". The bare `group_by='job_type'` spelling
reads as `meta.job_type`, so
`args={'group_by': 'job_type,state'}` is the one-call diagnosis table
(counts by type × status, per-group span, error-heads trailer for the
failed rows).

## When does a cadence fire next? — [[precis-job-observe-help]]

`get(kind='job', id='/cadence…')` lives in
[[precis-job-observe-help]].

## Cancel a running job
## Stop a job that's taking too long

```python
tag(kind="job", id=101, add=["STATUS:cancel_requested"])
# worker SIGTERMs at the next safe point; final tag is STATUS:cancelled
# a still-queued (never claimed) job goes straight to STATUS:cancelled
```

Agents drive only these `tag` edges: queued|yielded ->
cancel_requested|cancelled;
running/submitted/waiting_* -> cancel_requested|failed; cancel_requested ->
cancelled. Any other STATUS (running, succeeded, queued) is
worker-owned and refused; re-run a failed job with `mode='retry'`.

## Move a queued pinned job to another node

A queued job pinned by `params.target_node` waits for that node even when
another is idle. Move the pin (queued jobs only):

```python
put(kind='job', id=<queued_job_id>, mode='retarget',
    params={'target_node': '<host>'}, text='<why>')
```

It refuses a job that is not `STATUS:queued` or not pinned, a node whose
executor lane is retired, and a node that doesn't advertise every resource
the job requires. `text=` is required; the old and new node, the reason and
the actor land in a `job-retarget` event on the job.

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

# The parent's meta.llm_select pin (placement/thinking/effort/
# temperature) survives a plain retry — the ack names it when it
# does. A surviving placement='local' pin re-mints the same failure;
# override it in the same call:
put(kind='job', id=<failed_job_id>, mode='retry', placement='cloud')
# select={...} is the full-knob form; placement= is its one-key
# shorthand and wins the placement key when both are given.

# Retry rejects a job that isn't terminal (STATUS:failed/cancelled),
# an orphan job with no todo parent, and model=/select=/placement=
# on a non-LLM parent.

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
shapes for the LLM-facing call.

## Status vocabulary

| Tag                       | Meaning                                |
|---------------------------|----------------------------------------|
| `STATUS:queued`           | Filed, waiting for a runner            |
| `STATUS:yielded`          | Sliced job released mid-run; claimable |
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

## What is the cluster actually doing right now? — [[precis-job-observe-help]]

Fleet-introspection views `get(kind='job', id='/…')` — `/logs`
(worker_logs, filterable by handler/host/process/job/since/level/q,
also via `args=`), `/limits` (provider pacing), `/delta` (since the
last doctor tick), `/builds` (per-process code + `contains_sha`),
`/throughput` (claims + queue depth) — live in
[[precis-job-observe-help]].

## See also

- [[precis-job-observe-help]] — compute-lane job types + the `/…` fleet views
- [[precis-gripe-help]] — the bug tracker
- [[precis-fix-gripe-help]] — fix_gripe recipe
- [[precis-search-help]] — find jobs by link/status
