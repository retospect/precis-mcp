---
id: precis-job-find-help
family: work
title: precis — find and read a job
summary: read-side job listing — poll the queue by STATUS tag, jobs on a gripe/paper via link=, jobs under a quest/todo/board via under=, per-type counts via job_type=/group_by/since=, and one job's card (error:, queue: lines, meta.finished_at, result/transcript/run-files/geometry views)
answers:
  - how do I check what jobs are currently running or queued?
  - how do I list failed jobs or find jobs that need attention?
  - what jobs have run on this gripe / paper / parent?
  - which jobs belong to this quest / todo / board / structure?
  - how many jobs of one type ran, finished or failed in a window?
  - how do I read the status, events and meta of a specific job?
applies-to: get/search (kind='job')
tags: workflow, verbs, addressing
kinds: job
status: active
---

# precis-job-find-help — find and read a job

The read side of the offline-work substrate: poll the queue, scope
listings by `link=`/`under=`/`job_type=`/`since=`, facet counts by
status, and read one job's card and run artifacts. Steering a job
is [[precis-job-ops-help]]; submitting one is
[[precis-job-help]]; the deeper fleet views (`/jobs -v`,
`/logs`, `/limits`, `/delta`, `/builds`, `/throughput`,
`/settings`, `/claim`, `/cadence`) live in
[[precis-job-observe-help]].

## What jobs are running right now?
## Show me the active queue

```python
search(kind="job", tags=["STATUS:running"])
```

## How long did a job run, and where?

Every terminal `STATUS:` (succeeded, failed, cancelled, timeout kill) stamps `meta.finished_at` (UTC), `meta.wall_s` (finished minus first claim; 0 if it never ran) and `meta.node` (the pinned `target_node`, else the claiming host). `get(kind="job", id=N)` prints them under `status:` alongside `updated_at` (the row's own timestamp — compare it to your clock when a card looks stale). A seed's compute dir is kept until that stamp lands.

## Show me everything queued up

```python
search(kind="job", tags=["STATUS:queued"])
```

`get(kind="job", id=N)` on a queued job adds `queue:` lines: its origin
class, its position in the executor's claim order (class, then priority,
then time queued) with the per-class count of rows ahead, what is running
on that executor now, and holds it can see (operator suspension, a
`target_node` pin, a live lease).

The card's status is verified against a fresh read before queue text is
rendered: when the row's real state moved on (claimed, parked, finished),
the card prints the fresh status or a `stale-read:` line instead of a
position — a terminal job never renders as queued (gr592853).

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
A guessed spelling with one obvious reading is accepted with an
`interpreted:` line rather than refused (gr635827): `view='compute'`
is the card itself — its compute lines (`footprint:`, `node:`,
`wall_s:`, `executor:`, `wall_seconds:`) are the read callers wanted.

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

## A coverage scan's per-point geometry — view='geometry'

A `surface_coverage_scan` job on an engine with `keep_geometry`
(autocatpath >= 0.45) stores every scan point's final relaxed structure,
and for a failed point (unconverged, detached, placement clash) its relax
trajectory subsampled to at most 50 frames, as `scan_geometry` chunks
with the extxyz in a blob (gr540872). They are never embedded, never
summarized, and never shown on the default job card; read them here:

```python
get(kind='job', id=<job>, view='geometry')
# → the points: id, adsorbate, n, FAILED, stored parts + sizes
get(kind='job', id=<job>, view='geometry', args={'point': '(111):O:4'})
# → that point's final relaxed structure (extxyz)
get(kind='job', id=<job>, view='geometry',
    args={'point': '(111):O:4', 'frames': True})
# → a failed point's relax trajectory (multi-frame extxyz)
```

Append-only: a re-run of the job adds new chunks and the newest record of
a point wins; an unknown point refuses listing the stored ids.

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

`since=`/`until=` ride `args=` like `job_type=` on the queryless plain
listing too — `search(kind='job', args={'since': '2026-10-20'},
status='*')` is the same recency-ordered listing without a `job_type=`
filter (adding `q=` makes it a ranked search and `sort=`/`mode=` legal
again).

`view='count'` facet rows also name each group's newest/oldest member
(`jo<N>` + created bounds) — "when did the last <type> run, how far
back does the streak reach". The bare `group_by='job_type'` spelling
reads as `meta.job_type`, so
`args={'group_by': 'job_type,state'}` is the one-call diagnosis table
(counts by type × status, per-group span, error-heads trailer for
the failed rows — the fastest lane-health read). Facet rows page
like a listing: `page_size` groups per
page (default 10, raise it for the whole table — `page_size=100`
covers ~39 job types), `page` the window; rows past the page fold
into one `other: N groups, M jobs` tail naming the hidden groups,
and its `next:` is the next page's call.

For the *trend* behind the snapshot — minted vs terminal per day,
with mint→terminal percentiles per lane —
`search(kind='job', view='flow', args={'job_type': '<type>'})`
(`precis-search-help`'s flow section).

## See also

- [[precis-job-help]] — submit a job, dispatch a todo to a lane
- [[precis-job-ops-help]] — cancel / retry / wake / extend a job
- [[precis-job-observe-help]] — `/jobs -v`, `/logs`, `/settings`, the fleet views
- [[precis-job-types-help]] / [[precis-job-factory-help]] — the `job_type` names
- [[precis-search-help]] — the general search surface
