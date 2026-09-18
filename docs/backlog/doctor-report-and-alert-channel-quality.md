---
status: ready
title: Work the three chronic campaigns down; rule on the doctor-report design
prio: high
model: opus
---

# Work the three chronic campaigns down; rule on the doctor-report design

Approved by Reto 2026-09-18 off the morning doctor report (`dr346343`) and
the reading cast (`dr346440`). Pieces 1–4 of the original five shipped the
same day (doctor-report preamble strip, truncated-summary rejection,
abbreviation allowlist, orphan-alert retirement + the `alert_backlog_rot`
self-count fix); what follows is what is left.

## gr225018 — work the campaigns down, do not bulk-close

**Reto's call, 2026-09-18: work them down.** `gr225018` blames three
chronic campaigns for the orphan pile: the Parkinson draft (`dr43014`)
citation grounding, the CNT/nanobud chunk series, and the GROUNDING AUDIT
campaign. These are real work, not gunk — they get drained, not deleted.
Retiring the orphan alerts removed the noise; this removes the cause. Size
it before starting; it is a campaign, not a session.

## Doctor-report design (proposed, NOT yet approved — Reto to rule)

The doctor's own output has a pathology that let a wrong root cause survive
a week on `gr335305`. The root finding is that **nothing consumes the
doctor's output**: `fix_gripe` jobs have run **0 times in 14 days** while
`diagnose_gripe` ran **249**. The diagnosing lane runs constantly; the
fixing lane never fires. Everything below is downstream of that.

- **Nothing acts on the report, so it restates unchanged state daily.**
  `gr335305` collected 15 doctor comments over six days, each re-reporting
  the same condition with a new stale-hours figure and `seen_count`. That
  volume is why the real cause went unread. Propose: annotate only when the
  *classification*, *localization* or *disposition* changes. "Still
  happening, now N hours" is a `seen_count` bump the alert already tracks —
  it should not become prose.
- **It asks for access instead of having it.** Nearly every comment ends
  "no worker_logs/Bash access this tick to confirm". Two of the three
  "needs a human" items in `dr346343` were answerable in minutes with host
  + code access (see the 2026-09-18 corrections on `gr336604` and
  `gr346342` — both stated root causes were wrong). Propose: grant a
  read-only `worker_logs` query and a host log digest, or drop the ritual,
  because re-filing the same request daily *is* the backlog.
- **"Needs a human" should be a queue row, not prose.** Reto's queue is
  `search(kind='todo', tags=['waiting-for:reto'])`. Four paragraphs at the
  end of a report are not a queue.
