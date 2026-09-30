---
status: idea
pillar: memory-graph
title: Widen `experiment` to first-party runs, not only paper-reported ones
---

# Widen `experiment` to first-party runs, not only paper-reported ones

What: `measures-substrate.md` §3 (blocked-by) specs a new `experiment` ref
kind that "belongs to exactly one paper (the one that ran it)". Our own
simulation runs, bench measurements and dogfood results (a `struct_relax`
job, a catpath autocatpath pass, an se DRC sweep) have no comparable record
— no single ref groups their measures rows the way a paper's experiment
groups a reported result.

Why: the frontier/quest machinery already reads `Candidate.measures` at read
time from `struct_runs`/`structure.meta` (`quest/frontier.py::_candidate_from_structure`)
because there is nowhere durable to write a first-party experiment — a
workaround, not a design. Widening `experiment` so it may be owned by a
quest tick, a job, or an se design (not only a paper) lets first-party runs
use the same `measures` table, same evidence-edge discipline, same tier
(measured/computed/derived/asserted) as literature-derived numbers — one
record shape for "how good is this candidate" regardless of source.

Owner anchor: `measures-substrate.md` §3 (the `experiment` kind's home);
`src/precis/quest/frontier.py::_candidate_from_structure` (the workaround
this replaces).

test: a `struct_relax` run's inputs and outputs appear as `measures` rows
against one `experiment` ref owned by the job/quest tick that ran it, not
a paper.

Closest existing items: `measures-substrate.md` (blocked-by — this widens
its `experiment` kind before it ships, not after), `quest-data-table-and-formula-discovery.md`,
`sim-harness.md`.

Note (2026-09-30): `experiment-loop.md`, filed the same day, says "NOT a
new `experiment` kind — the loop is findings + todos + measures + links",
while `measures-substrate.md` §3 owns an `experiment` ref kind. This item
follows measures-substrate; if the loop's reading wins, the widening here
becomes "a `todo`/`job` may own measures rows", same content.
