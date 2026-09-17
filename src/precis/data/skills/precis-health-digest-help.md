---
id: precis-health-digest-help
title: precis — health_digest liveness-net worker pass
summary: hourly outcome-based liveness digest — curated Layer-1 outcome checks + derived cadence-staleness + derived registry coherence, pushed daily/on-degradation as kind='alert' + a Discord digest; persistent findings route to auto-closing gripes
answers:
  - what does the hourly health digest actually check?
  - how does a health-digest finding turn into an alert?
  - when does a health-digest result get pushed to me vs stay quiet?
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

## See also

- [[precis-nursery-help]] — the `critical`, page-now sibling reviewer.
- [[precis-alert-help]] — the `alert` kind (lifecycle, dedup, tab).
