# health_digest — gate the "degraded" ops push on severity

`_maybe_push` sets `degraded` on *any* first-sighting finding regardless of
severity (`degraded = degraded or is_new` in
`src/precis/workers/health_digest.py`), so an `info`-severity condition
(first exerciser: `settings-env-shadowed`) still triggers a
`health_digest: degraded` ops push on first sighting — contradicting the
"info = cleanup visibility, never a page" contract the gripe router already
honors (`_router_budget_hours` returns `None` for info-severity outcome
findings). Decide whether `degraded` should be severity-gated (info never
flips it) or the contract reworded. Pre-existing asymmetry, surfaced by the
db-resident-settings slice-4 pre-ship review. Owner
`src/precis/workers/health_digest.py`. Small; needs a decision first.
