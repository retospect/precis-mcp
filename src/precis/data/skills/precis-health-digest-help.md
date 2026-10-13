---
id: precis-health-digest-help
family: work
title: precis — health_digest liveness-net worker pass
summary: hourly outcome-based liveness digest — curated Layer-1 outcome checks + derived cadence-staleness + derived registry coherence, pushed daily/on-degradation as kind='alert' + a Discord digest; persistent findings route to auto-closing gripes
answers:
  - what does the hourly health digest actually check?
  - how does a health-digest finding turn into an alert?
  - when does a health-digest result get pushed to me vs stay quiet?
  - what does a pass-dead alert mean (a handler dead inside a live worker)?
applies-to: precis worker --only health_digest; kind='alert' (alert-source:watchdog:*); kind='gripe' (origin:health-digest-router)
tags: troubleshooting, workflow
kinds: alert, gripe
status: active
---

# precis-health-digest-help — the §D liveness net

`health_digest` is the hourly, SQL-only liveness digest — the slow-rot
sibling of [[precis-nursery-help]] (which pages immediately on an outage).
It catches degradation that unfolds over hours: a discovery pass going
dark, a cadence that stopped firing, a registered pass with zero activity.
A persistent finding routes to a `kind='gripe'`; fixing the underlying
condition auto-closes it.

## Findings → alerts

Each non-`ok` check raises a `kind='alert'` under
`alert_source="watchdog:<group>"`, severity capped to `info`/`warn` — this
pass never pages. `resolve_stale_alerts` auto-closes whatever goes fresh
again.

Read one open alert in a single call — body, severity/source/state,
fingerprint, seen_count, timestamps, and its links:

```python
get(kind='alert', id=N, view='detail')
```

## `pass-dead` — a handler dead inside a live worker

The `condition` group (alert source `watchdog:condition`) is a
declarative probe registry — `src/precis/workers/conditions.py`, one
row per condition with a severity and an optional whitelisted heal. Its
`pass-dead-on-host` row fires when a handler that logged steadily on a
`(host, process)` over the last 7 days (≥ ~150 rows, so one-off passes
can't false-alarm) has been silent past budget — 4 h default,
`PRECIS_COND_PASS_DEAD_BUDGET_S` — while the host still heartbeats. The
runner logs every registered pass every cycle, idle or not, so silence
means the handler is dead, wedged or removed, not merely quiet.
Deliberate offs are excluded: services disabled via `service_config`,
and worker units the host heartbeat reports both inactive and disabled.
A finding keys as `pass-dead:<host>/<process>/<handler>` at `warn`, and
on the `precis-worker` process it may request a bounded **restart-once**
heal — ssh plus a single-command sudoers grant, dark until an operator
arms `PRECIS_RESTART_ONCE_ENABLED`. Sibling rows: `pass-wedged` (the
heartbeat stays fresh but the pass's `since` goes stale — it has been
running for hours), `rescue-pass-cadence`, `llm-degraded`,
`dead-generation-claims`, `settings-env-shadowed`.

## See also

- [[precis-nursery-help]] — the `critical`, page-now sibling reviewer.
- [[precis-alert-help]] — the `alert` kind (lifecycle, dedup, tab).
