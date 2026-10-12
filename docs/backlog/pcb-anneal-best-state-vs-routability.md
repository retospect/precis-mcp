---
status: idea
title: pcb anneal keeps the best-cost state, not the best-routing one (gr464240)
pillar: 3d-design
---

# pcb anneal keeps the best-cost state, not the best-routing one (gr464240)

**What.** `optimize.py` keeps the state with the lowest `_total_at_report()`
(`best_total`). Nothing in that total prices routability, so the state the
anneal keeps can be the one the maze router realizes worst. ewod-dogfood-6:
20 of 55 routable nets after the best-state fix (job 464239), 22 in round 3
(job 464668), down from 29 before gr462607. Round 3 had `best_at=0/708`: the
anneal never improved on its start state.

**Build (ruled 2026-10-12, Reto design-fold).** After the anneal, route a
few candidate states — start, best, last — through the maze, and keep
the one with the most realized nets. Cost is a few maze runs per job, and
the job summary shows the routed count per candidate.

**Not building.**
- A routability term in the cost (`cost.py::gap_capacity_term`):
  `view='congestion'` reads 0 over-capacity gaps on dogfood-6 while nets
  fail, so the term would read zero.
- Routing on every anneal move: far too slow.

**Gate.** Open only after ewod-pcb Do-next 0 (re-put ARR1 at generator v4,
then the unconstrained re-route with the warm start). Do this only if
routed/55 stays low there and `best_at` stays near 0. The 2026-10-12 r24
re-route realized 51/55 (job 479966), so re-measure before building.

**Accept.** The summary lists routed count per candidate state, and the
kept state is the one with the most realized nets. `cost_after <=
cost_before` still holds for the stored cost.

Owner: `threads/ewod-pcb.md` Do-next 0b.
