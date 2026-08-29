---
status: draft
title: a contradicts edge means two different things, and one of them permanently blocks the wrong hub
prio: high
---

# `contradicts` conflates "evidence opposes this claim" with "the draft misused this claim"

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

## Options

1. **A distinct relation** for cite-misuse (`misused-by`? `overclaimed-in`?)
   pointing hub→draft-chunk, leaving `contradicts` to mean evidence only.
   Cleanest; needs a migration and a writer change wherever the review
   lens emits these.
2. **Discriminate on the source kind at read time** — a `contradicts`
   whose `src_ref_id` is a `finding` review artifact rather than an
   evidence-kind ref (`taproot/hub.py::EVIDENCE_SRC_KINDS`) doesn't count
   toward `disputed` / `check_contradicts`. No migration; risks being a
   heuristic that drifts.
3. **Leave the relation, add an adjudication door** (stage 8) that can
   retire an edge once the prose is fixed. Largest, and wanted anyway —
   see `docs/backlog/taproot-adjudicate-reopened-claims.md`.

Find the writer first: whichever review lens emits these (the
`raises-concern-about` / `cites` fan-out in `quest/review_fanout.py` is
the likely source — dr173020 carries 20 `raises-concern-about` edges)
decides whether option 1 is a one-line change at the write site.
