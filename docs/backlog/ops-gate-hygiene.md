# Ops gate hygiene

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## service_config rollback gates need expiry / review

_Grouped 2026-09-26; was `service-config-rollback-gate-expiry`._

An emergency rollback set `(melchior, chase_trigger, prio=0,
actor='phase2-rollback-embedder-warm')` on 2026-08-02 and nothing ever
revisited it — the taproot chase lane sat dark for 10+ days while ~4.5k
papers landed (gr202399), and the only detection was a watchdog gripe
about a downstream symptom. Rollback-actor disables are meant to be
temporary but the table has no TTL, no review queue, and no "intended
state differs from configured state" check. Options (pick at design
time): an `expires_at` column the resolver treats as auto-revert; a
health_digest layer-2 check flagging rows whose actor matches
`*rollback*` older than N days; or a `/status` banner for any prio=0 row
on a registry service whose spec default is ON. Owner
`src/precis/workers/service_config.py` + `health_digest.py`.

## health_digest — gate the "degraded" ops push on severity

_Grouped 2026-09-26; was `health-digest-degraded-severity-gate`._

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
