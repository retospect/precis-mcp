---
status: idea
pillar: pcb
---

# pcb placer: `risk()` is a max, so cheap terms outvote violations

`src/precis/pcb/cost.py::aggregate_margin` takes the criticality-weighted
MAX over `Family.MARGIN` terms; `money_total` sums. Every non-maximal
graded constraint contributes zero, so any `Family.MONEY` term is a free
tie-breaker (a $0.046 term once overruled `courtyard_overlap`); shrinking
its weight does not help. Related: `_placement_is_legal` gates moves, never
the incumbent (`d2[moving] = math.inf`), so a part that already overlaps is
never rejected.

Options: sum, soft-max (log-sum-exp with a temperature), or keep the max
plus a hard incumbent-legality check. Measure each on the dogfood boards
(violations left, wirelength) before choosing. Pairs with gr504558
(partial-accept phantom pose).

Source: memory me477999 (open since Reto 2026-10-02); the earlier backlog
item was pruned in 34f08cfff. Filed on Reto's go 2026-10-22.
