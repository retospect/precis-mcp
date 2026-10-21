---
status: draft
pillar: memory-graph
title: a contradicts edge means two different things, and one of them permanently blocks the wrong hub
prio: high
---

# `contradicts` conflates "evidence opposes this claim" with "the draft misused this claim"

> **Re-scoped 2026-09-02, blocking defect cured by 426518bb.** Part 1 of
> `docs/backlog/disputes-edge-nonblocking-disagreement.md` (shipped,
> migration 0151) cures the *blocking* defect here: its migration repoints
> all
> claim-graph `contradicts` rows (including the 3 review-critique rows
> below) to non-blocking `disputes`, and repoints the review-lens writer
> so new critiques file `disputes` too — the wrongly-blocked hubs become
> visible open questions instead (its decisions D2/D3/D5). What remains
> *this* item's scope: the inversion — cite-misuse belongs to the
> (draft-chunk, hub) pair, not the hub — and whether a dedicated
> relation (option 1, `misused-by` hub→draft-chunk) should re-target
> those rows so a prose complaint stops even *disputing* the science.
> Options 2/3 below are superseded by Part 1's D1/D2.

Found 2026-08-29 while auditing the 4 disputed hubs behind the nanobud
draft (`docs/backlog/nanobud-claim-remediation.md`). Three of the four
turned out to be contradicted not by opposing evidence but by **review
findings filed against the draft's prose**:

| hub | contradicted by |
|---|---|
| fi191315 | fi255164 — *"citation overstates beam-damage artifact as 'engineering'"* |
| fi191316 | fi192706 — *"claim-strength inflation — 'will ultimately require' vs source's 'could be used'"* |
| fi191329 | fi255165 — *"doesn't cover 'two distinct methods / CO disproportionation' clause"* |

Only fi189542 (contradicted by paper pa5828) is a real evidence conflict.

## Why this is a defect, not just untidy

Both meanings land on the same relation and are read as the first one
everywhere downstream:

- `nanopub/overview.py::hub_rows` computes `disputed` from *any* live
  inbound `contradicts` edge, so the hub renders `disputed` in the search
  table's `flags` column, in `_posture_cells`, and in the fisheye hub
  header.
- `nanopub/gates.py::check_contradicts` **hard-blocks the mint**: "a hub
  carrying a live unresolved `contradicts` edge is unmintable until
  adjudicated."
- `reword.py::_COHORT_SQL` excludes disputed hubs, so the hub can't even
  be reworded.
- `handlers/finding.py::_passes_trust` fails `trust='verified'` on it.

So a claim that is *fine* gets marked as scientifically contested, blocked
from publication, and locked out of the reword path — because someone
cited it sloppily in one draft. And **fixing the prose does not clear
it**: the edge is a persistent row, not a computed property. Stage 8
(Adjudicate) is absent, so nothing removes it.

There is also a scope inversion: the defect belongs to a
(draft-chunk, hub) pair — dc2445944 misused fi191315 — but it is recorded
as a property of the hub alone, so it would keep blocking that hub for
every *other* draft too.

## Decisions (Reto 2026-10-21)

- Build option 1: a new `misused-by` relation (hub → draft-chunk) for
  cite-misuse, so a prose critique no longer even *disputes* the science.
  One forward-only migration adds the relation row and repoints the 3
  review-critique rows above (fi255164, fi192706, fi255165) from `disputes`
  to `misused-by`; the review-lens writer emits `misused-by` from then on.
  Read-time discrimination (old option 2) and the adjudication door (old
  option 3, owned by `disputes-adjudication-workflow.md`) are not this item.
- Buildable now. First find which writer files these critiques today:
  `quest/review_fanout.py` no longer emits `contradicts`/`disputes`; current
  writers are `workers/conflict_search.py`, `inbound_ground.py`,
  `hub_refine.py`, `quest/dossier.py`.
