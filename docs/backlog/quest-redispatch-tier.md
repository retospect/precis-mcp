# redispatch_candidates always redispatches at the neb tier

A deployed-engine re-score ignores a candidate's own tier-ladder rung: a
candidate whose canonical barrier came from `verify` gets re-scored one rung
down. Decide the semantics before the ladder + engine-bump paths interact
again — read the candidate's own `barrier_fidelity`/`tier` (or highest
completed rung) and dispatch at that. Owner
`src/precis/quest/compute.py::redispatch_candidates`.
