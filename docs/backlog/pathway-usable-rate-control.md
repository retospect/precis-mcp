---
status: ready
---
# Gate stored control without changing the engine verdict

For gr565471 and td565472, share one read-only usability predicate across
pathway scalars, kinetics/sweep text, web verdict/bars and explorer step
labels/tooltips. Require a
nonempty finite numeric X_RC map, no missing values, absolute values at most
2.0, and a sum within the engine's inclusive
`degree_of_rate_control(sum_tol=0.05)` bounds. X_TRC requires only a nonempty
finite map with no missing values: it has no sum rule. Preserve source
provenance and stored records; never recompute. Restore the verbatim engine
verdict and its original golden parity gate; apply stored-record checks and
source notes in `src/precis_web/pathway_kinetics.py::stored_kinetics_verdict`.

Cover zero/null, oversized sum, MF/chain/unstamped valid maps, both tolerance
edges, null among values summing to one, empty maps, cancelling oversized
values, and a lone negative X_TRC brake. Sweep optima without a map retain
their named top step with “sum check not stored”; source warnings call
unstamped values unreliable. Revert the unrequested barrier-label rename.
Coordinator runs tests and lands; this lane adds one commit only.
