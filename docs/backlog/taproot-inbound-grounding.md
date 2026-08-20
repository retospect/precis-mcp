# Ground incoming papers against the claim set (inbound completeness)

Intent (Reto): every new paper is checked support/deny against existing
claims. (a) Evaluate + enable `src/precis/workers/inbound_chase.py` (built,
dark behind PRECIS_INBOUND_CHASE_ENABLED — a genuine in-pass env flag,
`inbound_chase.py::inbound_chase_enabled`, not a `ServiceSpec` gate) —
citation-graph shaped; its cost
backstop, the global spend breaker, is now shipped, so the flip is an
operator judgment (landmark papers with thousands of citers still have no
per-paper breaker). (b) A claim-hub variant: ANN-match a newly ingested
paper's chunks against hub embeddings, verify, attach evidence — finds
support even absent a citation edge. (c) The full papers × claims corpus
backfill stays the deferred batch backstop. Type-2 general-similarity
linking (`related-to` + meta.note) is deliberately separate scope.

test: ingest a paper known to support an existing hub → an evidence edge
appears without a citation path.
