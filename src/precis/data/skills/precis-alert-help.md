---
id: precis-alert-help
family: work
title: precis — the alert kind (machine-detected ops/health conditions)
summary: kind='alert' — background passes raise deduped, auto-resolving alerts for spin loops, stale claims, stalled recurrings; surfaced by the /alerts web tab
answers:
  - is the embedder (or any subsystem) healthy right now?
  - what does an alert on kind='alert' mean and who raised it?
  - is a specific failure id open, and has it come back before?
  - how do I triage or resolve an open alert?
  - which background passes raise alerts, and for what conditions?
  - does an alert show up in normal search?
applies-to: kind='alert'; precis.alerts.raise_alert / resolve_stale_alerts / resolve_alert; /alerts web tab
status: active
tags: [troubleshooting]
kinds: [alert]
---

# precis-alert-help — the `alert` kind

An **alert** is a machine-detected operational / health condition —
a worker spin loop, a dark host, a stalled recurring, a stale
claim. Project backlog is *not* one: an orphan todo lives in the todo
queue, and the nursery stopped alerting on it 2026-09-18. It is *derived state*: a pure function of the current DB,
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
meta.rule_id='<registered failure id>'   # e.g. nursery:dead-worker/dead-worker:{host}:{process}
meta.subject_ref_id=<ref the alert concerns>   # optional
meta.seen_count=<passes that have seen it still open>
meta.resolved_at='<iso>'         # set when resolved
```

The **failure id** is `<source>/<fingerprint>` — `alert_source` plus
`fingerprint`, split on the *first* `/` (fingerprints may contain one,
sources never do). Every id belongs to a registered **rule**: a source
pattern plus a subject pattern (`watchdog:discovery/embed`,
`nursery:dead-worker/dead-worker:{host}:{process}`), with its meaning,
budget, owning service and a triage line. `get(kind='alert',
id='/rules')` lists them all; `meta.rule_id` on a row names the one it
belongs to.

Alerts are **not embedded** — no `card_combined` chunk, so they never
reach `search(kind='*', like=...)`. Read them by tag / view / the web
tab, not by semantic neighbourhood.

## Health questions: read the panel, never improvise

"Is X healthy / up / working?" has one first move:

```
get(kind='alert', id='/health')                     # every check's verdict, one call
get(kind='alert', id='watchdog:discovery/embed')    # one failure id: open? recurrence? check verdict
```

`/health` is `health_digest`'s last eval (hourly snapshot, age shown)
with the cheap freshness probes re-run live — the same per-subsystem
states `/status` renders. Each line carries its failure id; follow it
with the single-id read, which answers **open / not open** (never
NotFound — "is it broken?" must be answerable "no"), `seen_count`,
first/last seen, **recurrence** (rows sharing the id; a fix is verified
when the id stops *opening*, not when one row is closed), the rule's
budget and triage line, and for a `watchdog:` id the check's own
verdict from the snapshot.

**Process presence is not health.** Lazily-loaded services idle-unload
by design — the embedder (`PRECIS_EMBEDDER_IDLE_S`) has no process to
find while it is perfectly healthy. On 2026-09-24 five agents spent 40
minutes concluding "embedder offline" from `ps` while prod had embedded
595 chunks in the previous hour; `watchdog:discovery/embed` was `ok` the
whole time. Throughput-and-backlog checks (the registry's idle-aware
budgets) are the test; `ps`, `/readyz` polling and log tails are
corroboration at most. Never report a subsystem down without its
failure id's state.

## Reading (agent side)

```
get(kind='alert', id='/health')        # per-subsystem health panel (above)
get(kind='alert', id='/rules')         # the failure-id catalogue: meaning, budget, service, triage
get(kind='alert', id='<source>/<fingerprint>')  # one failure id: state + recurrence + rule
get(kind='alert', id='/open')          # currently-open alerts
get(kind='alert', id='/recent')        # recent (open + resolved)
get(kind='alert', id=42)               # one alert + tags
get(kind='alert', id=42, view='detail')  # triage shape (below); 'full' is an alias
get(kind='alert', id=42, view='links')   # link graph to/from this alert
get(kind='alert', id=42, view='raw')     # verbatim record — every meta key
get(kind='alert', id=[42, 43, 44])       # several at once: one summary block per id (≤50)
search(kind='alert', q='spin loop')    # lexical over titles
search(kind='alert', tags=['alert-source:nursery:spin-loop'])
search(kind='alert', tags=['severity:critical'])
```

`view='detail'` (alias `'full'`) is the one-call triage shape: body +
`detail`, severity/source/state, `fingerprint`, `seen_count`,
`subject_ref_id`, `created_at`/`updated_at`/`resolved_at`, and the link
graph — everything a doctor/health-digest tick needs without stitching
together a bare `get` + `view='raw'` + `view='links'`.

Or browse the **Alerts** tab in `precis web` (`/alerts`) — open by
default, grouped by source, severity-sorted; `?state=resolved` shows
recent history.

## Triage

To dismiss an alert by hand ("I've seen it, stop showing it" while the
underlying fix lands), either use the **dismiss** button on the
`/alerts` web tab or flip the tags:

```
tag(kind='alert', id=42, add=['alert-state:resolved'],
    remove=['alert-state:open'])
```

`id=[42, 43]` resolves several alerts in one call and one transaction.
Both paths flip the state tag *and* stamp `resolved_at` in one
transaction (the dedup unique index keys off `resolved_at IS NULL`, so
the two must move as one — the handler syncs the column on any
`alert-state` tag edit). Re-opening a resolved alert by tag clears
`resolved_at` again, unless the condition has since re-raised as a
fresh alert — then the edit is rejected and points you at the live row.

Most alerts auto-resolve when their producer next runs and the
condition has cleared, so manual resolution is rarely needed.

## Producers

`get(kind='alert', id='/rules')` is the live, complete list — every
rule any producer can emit, grouped by source. The families:

| source family | raised by | what |
|---|---|---|
| `watchdog:<group>` | `health_digest` (hourly) | slow-rot outcome checks: ingest, discovery (`embed`, `chunk_keywords` — idle-aware), reading, knowledge, autonomy, infra; derived `cadence` / `coherence` / `condition` groups |
| `nursery:<category>` | nursery (per minute, SQL) | spin loops, stale claims, dead/restarting workers, dark hosts, dispatch and embed-lane stalls |
| `review:empty:<reviewer>` · `review:tool-starved:<reviewer>` | LLM reviewer pass | silent-empty / tool-starved pass (config or credential failure shape) |
| `disk_check` · `quota_check:auth` · `budget` · `budget:quota` | per-host passes, the LLM breaker | disk under threshold, expired OAuth, spend cap / quota window hit |
| `nanopub_ots` · `nanopub-demote` · `nanopub_mirror` · `watch:*` · `ingest:*` · `inject_scan` · `llm_reconcile:drift` · `scheduler` · `quest_tick` · `admit:oversize` · `fetch_oa:*` · `orcid_enrich` | their owning pass | one or two conditions each — see `/rules` |

The producer surface is generic (`precis.alerts.raise_alert`); a new
condition is a new registry rule plus a raise site — the registry's
totality test refuses an unregistered source. The LLM reviewers'
*digests* stay on `kind='memory'` (their output is *reflection*, not a
detected condition), but their failure modes DO raise alerts and
auto-resolve on the next real digest.

## See also

- [[precis-nursery-help]] — the detector pass that produces most alerts
- [[precis-health-digest-help]] — the hourly checks behind `watchdog:*` and `/health`
- [[precis-status-help]] — the human `/status` page and build-staleness checks
- [[precis-overview]] — the master kinds table
