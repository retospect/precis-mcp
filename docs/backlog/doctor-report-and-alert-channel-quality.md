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

## Open

1. **Brief attention count — ruling needed (Reto).** The brief counts
   `OPEN:ask-user*` todos; the doctor's needs-a-human todos are
   `waiting-for:reto`, so the 09-20 brief said "two tasks waiting on
   your input" the morning the doctor had minted twelve. Fold
   `waiting-for:reto` into the brief's attention count, or leave the
   doctor's queue to `/asks`.
2. **Should the alert lane defer to the doctor's verdicts?** The 09-20
   brief re-diagnosed alerts the doctor had already classified
   ("something structural broke in the scheduler" vs the doctor's
   fail-closed config gate, gr179498). With the health line fixed the
   brief carries the doctor's top classification; whether the lane
   should defer to the verdicts is undecided. Related: the doctor's
   "0 log lines" reads suggest its pass filter misses
   `precis.workers.runner` lines that name the pass in the message —
   worth a look when the doctor prompt is next touched.
3. **`worker_logs` `ts` index.** The table has only a partial level
   index and no plain `ts` btree, so `/logs?level=INFO` with no
   host/handler facet scans unindexed; add the index in a forward-only
   migration when the view gets real use.
