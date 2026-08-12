# service_config rollback gates need expiry / review

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
