# Boundary probes

Each script here is the mechanical check a
`precis.plans.boundary.Boundary` names in its `probe` field: the lander
runs it inside the slice worktree against a diff that hit the boundary,
and a green exit is what clears the hit in `boundary.unprobed` — a hit
whose probe is unset or whose script is absent keeps its security review.

This directory is itself on the boundary list (`scripts/probes/**`), so
writing or weakening a probe is a boundary hit: the check that clears a
review cannot quietly move. No probes exist yet — every `probe` field in
`BOUNDARIES` is `None` until one lands (td574934 A3+).
