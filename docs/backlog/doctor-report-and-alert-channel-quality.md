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

## gr225018 — the orphan invariant is the question, not the campaigns

**Reto's 2026-09-18 "work them down" was given under a wrong diagnosis
and needs re-ruling.** `gr225018` comment 18 re-measured the 50 open
`nursery:orphan` alerts against their *subject todos* rather than the
alert titles: the three named campaigns are ~10 of the 50 (Parkinson
`dr43014` 4, GROUNDING AUDIT ~6, CNT/nanobud `dc2445xxx` 0 — none of
`al236409`–`al236469` is still open). The other ~40 are live work sketched
this month (AIxMAT abstract, se+hexfold paper `td344088`, pcb place+route
preprint, glass-foam buoyancy study, photonic arm dossier, EWOD-in-oil
sourcing, …); 31 of the 50 are newer than 7 days. Draining the campaigns
clears a fifth of the pile; "triaging" the rest would close this week's
work. The ~10 campaign leaves are a short triage, not a campaign.

**Mechanism.** `_detect_orphans` (`workers/nursery.py`) flags any open todo
whose topmost todo ancestor lacks `meta.rotation_root=true`. That facet is
owner-only (`handlers/_todo_guards.py` `_facet_violation`) and the `put()`
default tier is subtask, so a top-level todo minted the normal way is an
orphan by construction until someone stamps `tier='strategic'` on its
root. The nursery count is silent since 2026-09-18, but the strategic
view and the picks-7d rotation (`handlers/_todo_views.py`) list only
`rotation_root` roots, so those ~40 trees are invisible there.

**Options — Reto to pick one:**

1. Keep the invariant, stamp the roots: one pass over the live top-level
   roots with `tag(kind='todo', id=…, meta={'tier': 'strategic'})`. No
   code. Steady state: every new top-level tree needs the stamp or stays
   out of the rotation.
2. Auto-stamp: a parentless todo minted by a non-worker source gets
   `rotation_root=true` (the `meta.workspace` auto-stamp in
   `precis-todo-tree-help` is the precedent). Small code change; loses
   the "owner explicitly chose this as strategic" signal.
3. Drop the invariant: retire the orphan detector; the strategic
   view/rotation then needs another membership rule.

Recommended: 1 now for the current pile, then rule on 2 by whether
"top-level tree" and "strategic" are the same thing in practice.

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
