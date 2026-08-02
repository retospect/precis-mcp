# External dead-man's-switch

`health_digest` (§D, `docs/proposals/health-watchdog.md`) covers every
liveness signal that lives *inside* precis's own Postgres — but a total
fleet/DB outage can't report itself through a mechanism that died with it.
The external dead-man's-switch is the one signal that survives that: an
outbound ping to a service outside the cluster (healthchecks.io-style) that
alarms *you* when the pings stop, not when precis notices they stopped.

## How it works

After every successful `health_digest` eval (hourly, via the
`health_digest` scheduler-lease cadence — `workers/scheduler.py`), the pass
GETs `PRECIS_DEADMAN_PING_URL` if it's set (SSRF-guarded via
`precis.utils.safe_fetch.safe_get`). A failing ping only logs a warning —
it never fails the pass. Dark (no-op) until the operator sets the URL.

If the fleet or the DB dies, the pings stop, and the external service
alarms you out-of-band — a channel that doesn't depend on anything in the
cluster still being alive.

### LAN / self-hosted targets

The SSRF guard is built for *agent-supplied* URLs and refuses
private/loopback/link-local ranges outright — which also blocks a
self-hosted check (a local [Healthchecks](https://healthchecks.io/docs/self_hosted/)
instance, an internal Uptime-Kuma push URL, …) reachable only on the LAN.
`PRECIS_DEADMAN_PING_URL` is an **operator-set env constant**, not
attacker-influenced input, so it doesn't need that guard the same way a
`kind='web'` fetch does. Set:

```yaml
precis_shared_env:
  PRECIS_DEADMAN_PING_URL: "http://10.0.0.5:8000/ping/<uuid>"
  PRECIS_DEADMAN_ALLOW_PRIVATE: "1"
```

to opt in to a private/loopback/LAN target — `health_digest` then issues a
plain, unguarded GET to that one URL instead of routing through
`safe_fetch`'s pinning transport (see
`precis.workers.health_digest._ping_deadman_private`). Without the opt-in
flag, a LAN target is blocked and logs a warning naming
`PRECIS_DEADMAN_ALLOW_PRIVATE` — the ping failing is silent to the pass
(never fails it) but visible in the worker's own logs.

## Setting it up (a human step — account creation isn't automatable)

1. Create a free account at [healthchecks.io](https://healthchecks.io) (or
   an equivalent — any "ping me or I'll alarm you" service works; the code
   only needs a URL to GET).
2. Create one check named something like `precis-health-digest`:
   - **Period**: 1 hour (matches the `health_digest` cadence interval).
   - **Grace time**: 2 hours (tolerates one missed/late fire before
     alarming — the cadence's own `interval_s + margin` staleness check
     already flags a *merely* slow fire; the external switch is for
     "nothing is pinging at all").
3. Configure its alert channel (email / Slack / Discord / SMS — whatever
   reaches you when the cluster itself can't).
4. Copy the check's ping URL (`https://hc-ping.com/<uuid>`).
5. Add it to the cluster's shared env overlay
   (`deploy/inventory/group_vars/all/precis_env.yml`, gitignored —
   see `deploy/inventory.example/`) as:

   ```yaml
   precis_shared_env:
     # ... existing keys ...
     PRECIS_DEADMAN_PING_URL: "https://hc-ping.com/<uuid>"
   ```

   This is the same `precis_shared_env` passthrough `PRECIS_OPS_ALERT_TARGET`
   rides — every `precis-worker`/`precis-worker-agent` unit template already
   loops over it (`{% for env_key, env_val in precis_shared_env.items() %}`),
   so no template edit is needed; adding the key here is the whole change.
6. Redeploy (`ansible-playbook redeploy-precis.yml`, or `/go`'s
   `scripts/deploy`) so the new env var lands on the worker units.

## Verifying it

- Watch the healthchecks.io dashboard for a ping within the next hour
  (`health_digest`'s cadence interval).
- To test the alarm path itself, stop every `precis worker` process (or
  just unset the URL then flip it back after) and confirm the external
  service pages you once the grace window elapses.

## What this does NOT cover

- A degraded-but-not-dead fleet (some checks stale, but `health_digest`
  itself still runs) — that's the normal digest push
  (`PRECIS_OPS_ALERT_TARGET`), not this.
- Any remediation — this is purely the "something catastrophic happened
  and nothing can tell you" backstop. You still have to go look.
