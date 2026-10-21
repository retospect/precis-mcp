---
status: draft
pillar: platform
title: doctor/brief quality residue — attention-count ruling, alert deferral, worker_logs ts index
prio: low
model: sonnet
---

# Doctor report and alert-channel quality — open remainder

The 2026-09-18 rulings all shipped and deployed: doctor-report preamble
strip, orphan-alert retirement (23d6234b), the `worker_logs` `/logs`
view (6686db89), needs-a-human → `waiting-for:reto` todos (64e97785),
the fix-lane arming (367779ca), the agent-lane container env root-cause
fix for gr346813 (86934f6af), and the ceiling-knob/env notes
(48a5e8de3). `git log` is the record. The wrong doctor asks and
gr225018's orphan chores are prod closes tracked as a fleet todo, not
here. What is left:

## Decisions (Reto 2026-10-21)

- The brief's attention count includes `waiting-for:reto` todos (change the
  count query in `src/precis/workers/briefing.py`). Acceptance: a brief with
  N `waiting-for:reto` todos counts N.
- The alert lane stays independent of the doctor's verdicts. Side note kept
  for the next doctor-prompt touch: its "0 log lines" reads suggest the pass
  filter misses `precis.workers.runner` lines that name the pass in the
  message.
- Buildable now: the count change plus the index below.

## Open

1. **`worker_logs` `ts` index.** The table has only a partial level
   index and no plain `ts` btree, so `/logs?level=INFO` with no
   host/handler facet scans unindexed; add the index in a forward-only
   migration when the view gets real use.
