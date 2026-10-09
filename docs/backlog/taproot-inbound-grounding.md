---
status: ready
pillar: memory-graph
---

# Ground incoming papers against the claim set (inbound completeness)

Intent (Reto): every new paper is checked support/deny against existing
claims. (a) Evaluate + enable `src/precis/workers/inbound_chase.py` (built,
dark behind PRECIS_INBOUND_CHASE_ENABLED — a genuine in-pass env flag,
`inbound_chase.py::inbound_chase_enabled`, not a `ServiceSpec` gate) —
citation-graph shaped; its cost
backstop, the global spend breaker, is now shipped, so the flip is an
operator judgment (landmark papers with thousands of citers still have no
per-paper breaker). (c) The full papers × claims corpus backfill stays the
deferred batch backstop — `workers/inbound_ground.py` (part b, shipped)
grounds only papers created inside `PRECIS_INBOUND_GROUND_MAX_AGE_DAYS`;
the older corpus is this item's remainder. Type-2 general-similarity
linking (`related-to` + meta.note) is deliberately separate scope.
