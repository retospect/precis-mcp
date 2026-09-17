# Alert producer mechanics (raising `kind='alert'` rows)

**When.** You're writing or debugging a background pass that raises
alerts — a new detector, a nursery condition, a producer that isn't
deduping or resolving the way you expect. Agent-facing reading/triage of
`kind='alert'` lives in `get(kind='skill', id='precis-alert-help')`; this
is the producer (worker) side.

## Lifecycle (producer side — workers only)

Background passes raise alerts through `precis.alerts`:

* `raise_alert(store, source=, fingerprint=, title=, detail=,
  severity=, subject_ref_id=)` — upserts on `(source, fingerprint)`
  among *open* alerts. A repeat sighting bumps `seen_count` +
  `updated_at` (no duplicate). Pick `fingerprint` so the same
  underlying problem always hashes to the same string.
* `resolve_stale_alerts(store, source=, live_fingerprints=)` — flips
  any open alert of `source` whose fingerprint is absent from the
  current live set to `alert-state:resolved` (kept for history).

A detector pass = raise for every current finding, then
`resolve_stale_alerts` with that pass's full fingerprint set, so a
fixed condition leaves the open list on the next pass.
