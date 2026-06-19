---
id: precis-alert-help
title: precis — the alert kind (machine-detected ops/health conditions)
summary: kind='alert' — background passes raise deduped, auto-resolving alerts for spin loops, orphans, stalled recurrings; surfaced by the /alerts web tab
applies-to: kind='alert'; precis.alerts.raise_alert / resolve_stale_alerts; /alerts web tab
status: active
---

# precis-alert-help — the `alert` kind

An **alert** is a machine-detected operational / health condition —
a worker spin loop, an orphaned todo, a stalled recurring, a stale
claim. It is *derived state*: a pure function of the current DB,
raised by a background pass, not hand-authored.

Alerts exist so this telemetry has a home that is **not** the memory
kind. Mixing ops alerts into `memory` conflated them with reflective
*thought*, polluted the namespace, and (when a churning condition like
a spin loop is active) produced thousands of near-duplicate rows a day.
The `alert` kind dedups per *condition* and auto-resolves when the
condition clears.

## Shape

```
kind='alert'                     # numeric id, NOT embedded
title='[<category>] <headline>'
alert_source='<producer>'        # e.g. nursery:spin-loop, sweeper, quota
fingerprint='<stable condition id>'
severity='info' | 'warn' | 'critical'
tags=[alert-state:open, alert-source:<producer>, severity:<sev>]
meta.subject_ref_id=<ref the alert concerns>   # optional
meta.seen_count=<passes that have seen it still open>
meta.resolved_at='<iso>'         # set when resolved
```

Alerts are **not embedded** — no `card_combined` chunk, so they never
reach `search(kind='*', like=...)`. Read them by tag / view / the web
tab, not by semantic neighbourhood.

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

## Reading (agent side)

```
get(kind='alert', id='/open')          # currently-open alerts
get(kind='alert', id='/recent')        # recent (open + resolved)
get(kind='alert', id=N)                # one alert + tags
search(kind='alert', q='spin loop')    # lexical over titles
search(kind='alert', tags=['alert-source:nursery:spin-loop'])
search(kind='alert', tags=['severity:critical'])
```

Or browse the **Alerts** tab in `precis web` (`/alerts`) — open by
default, grouped by source, severity-sorted; `?state=resolved` shows
recent history.

## Triage

To acknowledge / resolve an alert by hand, swap the state tag:

```
tag(kind='alert', id=N, add=['alert-state:resolved'], remove=['alert-state:open'])
```

Most alerts auto-resolve when their producer next runs and the
condition has cleared, so manual resolution is rarely needed — it's
for "I've seen it, stop showing it" while the underlying fix lands.

## Producers

| `alert_source` | Raised by | Severity |
|---|---|---|
| `nursery:spin-loop` | nursery (`ref_events` > 200/24h on one `(ref_id, source)`) | warn |
| `nursery:orphan` | nursery (open todo with no strategic ancestor) | info |
| `nursery:stale-claim` | nursery (`claimed-by:*` > 3h) | warn |
| `nursery:long-wait` | nursery (`waiting-for:*` > 7d) | info |
| `nursery:stuck-doable` | nursery (doable leaf idle > 24h) | info |
| `nursery:stalled-recurring` | nursery (recurring's last child stuck) | warn |

The producer surface is generic (`precis.alerts.raise_alert`): more
passes can adopt it — failed worker passes, the sweeper's claim
orphans, quota exhaustion — without schema changes. The LLM reviewers
(`structural` / `deep_review`) stay on `kind='memory'`: their output is
*reflection*, not a detected condition.

## Related skills

* `precis-nursery-help` — the detector pass that produces most alerts
* `precis-overview` — the master kinds table
