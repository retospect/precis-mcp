---
status: draft
title: Quest dashboard — a cadence strip (last tick, next due, rest reason, WIP)
prio: normal
---

# Quest dashboard — a cadence strip (last tick, next due, rest reason, WIP)

## Motivation / why

The quest dashboard (`precis_web/routes/refs.py::_quest_detail`) answers
"what has this quest learned" well — dossier, logbook, frontier scatter,
gaps, retinue — and answers "is it alive" only through the `momentum`
badge, which is a heuristic over *recent activity counts*
(`precis/quest/gaps.py::quest_momentum`), not over the loop's own state.

So the one question a human watching an autonomous loop actually asks —
**when did it last come around, and why hasn't it come around since?** —
has no answer on the page. `stalled` and "resting on a dry-rest backoff,
correctly, tick due in 40 minutes" render identically. The information
exists; it is just CLI-only, in `precis quest status`
(`precis/quest/status.py::gather_quest_status`), which nobody watching a
web dashboard is going to run.

This is the gap the `/manual` chapter *Watching the loop* currently has
to apologise for in prose. When it ships, delete that apology.

## In scope

One strip in the dashboard header, beside the momentum badge, carrying:

- **Last tick** — relative time, linked to that tick's agentlog (the
  "spy on last session" target, already computed by
  `refs.py::_quest_last_agentlog_id`).
- **Next due / resting** — the coordinator's next fire, or the rest state
  and its reason. Both `failed` and `dry` rests back off on the same
  exponential window (`precis/quest/loop.py`), and a quest whose
  `consecutive_dry_rests` has crossed the escalation threshold is being
  skipped entirely — that last state especially must be legible, since
  it is the one that looks like "broken" and isn't.
- **In flight** — the WIP=1 backpressure: whether a dispatched proposal
  is holding the next tick, and how many sims under it are still
  unresolved (`status.py::_sim_job_rows` already assembles this).

Read-only, one cheap query, degrading to a blank strip on any failure —
same defensive posture as `nav.py`'s badges. A quest that has never
ticked shows "never ticked", not an empty box.

## Explicitly NOT in scope

- No **tick-now button**. Dispatching work from the dashboard is a
  different decision with a different blast radius; this item makes the
  loop legible, it does not make it steerable.
- No change to the loop, the reconciler, or any backoff behaviour.
- No new cadence *storage*. If a field turns out to be underivable from
  what the coordinator already writes, cut it from the strip rather than
  adding a column — an underived field is a second source of truth about
  when the loop ran.
- Not the `precis sim` harness (separate machinery, no web surface at
  all — its own item if it ever wants one).

## Acceptance criteria

- A quest resting on a dry-rest backoff and a genuinely stalled quest
  render **differently** in the header, without opening the logbook.
- Last-tick time on the dashboard matches `precis quest status <id>` for
  the same quest.
- A quest holding on WIP=1 backpressure says so, and names the count of
  outstanding sims.
- A never-ticked quest and a quest whose coordinator job is missing both
  render a defined state, not a blank or a 500.
- Strip failure degrades that strip only; the rest of the dashboard
  renders.
- The apology paragraph in `src/precis_web/manual/04-watching-the-loop.md`
  ("What the dashboard does not tell you yet") is deleted in the same
  commit, and the chapter's CLI section keeps only the views the web
  genuinely doesn't cover.

## Target + blast radius

`precis_web/routes/refs.py::_quest_detail`,
`precis_web/templates/refs/quest_detail.html.j2`, read-only helpers off
`precis/quest/status.py` and `precis/quest/loop.py`. No writes, no
migration, no worker change.

## Open questions / decisions log

- Is "next due" cheaply derivable from the coordinator job's own row, or
  does it need the reconciler's scheduling logic re-implemented in the
  read path? If the latter — drop it, show only "resting since / reason",
  and do not fork the schedule calculation.
