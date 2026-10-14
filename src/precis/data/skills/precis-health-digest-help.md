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

## Check names — what `get(kind='alert', id='/health')` shows

Each row is one named check; the check `name` is what the health panel,
the `/status` Health sub-tab and (on a finding) the alert's
`meta.rule_id` / fingerprint lead with. Curated Layer-1 outcome checks
plus the bespoke checks that share the `CheckResult` shape:

| check | group | what it watches |
|---|---|---|
| `papers_ingested` | `ingest` | newest paper ref landed within 6h |
| `news` | `ingest` | newest news ref within the live `news_poll` cron gap + slack (≥8h floor) |
| `chunks_extracted` | `ingest` | no live paper newer than the newest chunk, unextracted >6h |
| `chunks_classified` | `discovery` | a ROLE3 classification within 12h while the classify pass is enabled and has eligible chunks |
| `embed` | `discovery` | embed backlog empty or draining within 2h (idle-aware); a stale detail names the first stuck stage — materializer → `embed_batch` → `job_inproc` |
| `chunk_keywords` | `discovery` | chunk_keywords backlog empty or draining within 6h |
| `morning_brief_cast` | `reading` | the briefing pass ran within 26h |
| `cast_audio` | `reading` | cast_audio narrated a cast within 26h |
| `card_forge` | `reading` | a card_forge job completed within 26h while cards were due |
| `taproot_edges` | `knowledge` | eligible chase findings never sit >6h with no new taproot evidence edge |
| `claim_hub_dedup_index` | `taproot` | no live claim hub missing the `finding_body` embedding claim dedup retrieves over |
| `staged_candidates_fresh` | `nanopub` | staged nanopub candidates still pass the current mint gates |
| `agent_jobs_completing` | `autonomy` | jobs claimed in 6h did not all stall short of `STATUS:succeeded` |
| `lanes_idle_beside_queue` | `autonomy` | no executor has queued work while claiming nothing for an hour |
| `doctor_report_fresh` | `autonomy` | the `doctor_tick` pass wrote a report within twice its fresh window |
| `hosts_alive` | `infra` | every recently-active host's `host_heartbeat` row is <15 min old |
| `alert_backlog_rot` | `meta` | no `alert-state:open` row is >7d old — the response loop itself rotting (info) |

Derived groups name their check after the thing they watch:

- `cadence` — the `scheduler_leases` check: each lease row overdue past
  `interval + margin` (≥15 min floor); fingerprint `watchdog:cadence/{cadence}`.
  Its never-seeded half flags a registered cadence with **no** lease row
  on any host — fingerprint `<cadence>/never-seeded` — the eligibility
  gate may be false fleet-wide.
- `coherence` — `<service>`: a registered PASS that resolves enabled
  somewhere but logged nothing in 24h — intended-on but silent.
- `condition` — the `conditions.py` probe registry, each row a named
  condition whose finding keys the alert fingerprint:
  `pass-dead-on-host` → `pass-dead:<host>/<process>/<handler>`;
  `rescue-pass-cadence` → `rescue-gap:<handler>` (a rescue handler has not
  run within budget while a live host has demand); `quest-tick-stalled` →
  `quest-tick-stalled:<quest_id>` (a quest's live quest_tick loop is
  queued or past its `at_time` wake beyond the stall budget with no slice
  run); `pass-wedged`; `llm-degraded`; `agent-ticks-toolless` (agent
  ticks are spending without calling a single precis tool);
  `dead-generation-claims` → `dead-gen-claims:<host>/<process>` (claims
  held by a worker generation that is no longer alive);
  `settings-env-shadowed`; `analysis-stale`.

## See also

- [[precis-nursery-help]] — the `critical`, page-now sibling reviewer.
- [[precis-alert-help]] — the `alert` kind (lifecycle, dedup, tab).
