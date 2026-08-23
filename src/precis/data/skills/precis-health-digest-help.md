---
id: precis-health-digest-help
title: precis — health_digest liveness-net worker pass
summary: hourly outcome-based liveness digest — curated Layer-1 outcome checks + derived cadence-staleness + derived registry coherence, pushed daily/on-degradation as kind='alert' + a Discord digest; persistent findings route to auto-closing gripes
answers:
  - what does the hourly health digest actually check?
  - how does a health-digest finding turn into an alert?
  - when does a health-digest result get pushed to me vs stay quiet?
applies-to: precis worker --only health_digest; kind='alert' (alert-source:watchdog:*); kind='gripe' (origin:health-digest-router)
status: active
---

# precis-health-digest-help — the §D liveness net

`health_digest` (`docs/backlog/self-healing-spine.md` Layer 2) is the
slow-rot sibling of `nursery` (`precis-nursery-help`): nursery's `critical`
lane pages the moment a worker/dispatch outage happens, but many outcomes
degrade over hours-to-days with nothing that urgent watching them (a
discovery pass silently going dark, a cadence that stopped firing, a
registered pass with zero activity). `health_digest` is the periodic,
outcome-based digest that catches that — SQL-only, no LLM anywhere in the
module, so it still sends a plain templated body when the LLM/agent fleet
itself is down.

## Fire mechanics

Fleet-singleton via the `health_digest` **scheduler-lease cadence**
(`workers/scheduler.py` CADENCES, hourly, host-agnostic — any live worker
can win it, mirroring `cron_tick`/`watch_poll`). The `ServiceSpec` row in
`workers/registry.py` carries `ref_pass=True` with **no** `default_profiles`
(mirrors `dream_agent`) — it registers into the normal worker rotation only
for a manual/ad-hoc run:

```
precis worker --only health_digest --once
```

In production it fires once an hour, wherever the cadence lease lands.

## Four check sources, every fire

1. **Curated Layer-1 outcome checks** — ~13 end-to-end outcomes (papers
   ingesting, chunks extracted/embedded/keyworded/classified, news, morning
   brief cast, cast audio, card-forge, agent jobs completing, taproot
   edges, hosts alive, alert-backlog-rot), budgets seeded from the design
   doc's pulse-probe observations. `embed`/`chunk_keywords` are
   **idle-aware**: they read `precis.health_checks.compute_backlog_counts`
   — the same computation `/status` renders — so an empty backlog is `ok`
   no matter how long ago the last batch ran, and only a *non-draining*
   backlog past budget is `stale`. `chunks_extracted` is body-row-only
   (`ord >= 0`, so a card_forge rewrite or a concept/glossary chunk write
   can't mask a stalled extraction pipeline) and input-aware: stale only
   when a paper landed past budget *newer* than the newest body chunk;
   quiet when no new paper is waiting (idle, not stuck). There is
   deliberately **no** curated row for `dream_agent` / `anki_sync` — a
   fixed budget here would contradict the derived cadence-staleness lane
   below the moment an operator raises the live interval (dream's
   DB-overridable knob); cadence staleness is their only check, watching
   the resolved interval automatically.
2. **Cadence staleness (derived)** — every `scheduler_leases` row overdue
   past `interval_s + margin` (`margin = max(interval_s, 300s)`), including
   `dream_agent` / `anki_sync`. Zero per-cadence config — a cadence added
   to `workers/scheduler.py` is watched the moment it seeds its first
   lease row.
3. **Layer-2 coherence (derived)** — every registered `kind=PASS` +
   `ref_pass=True` `ServiceSpec` that resolves enabled (structural
   `default_profiles`/`enable_env`, or a live `service_config` prio
   override on any host) with **zero** `worker_logs` rows in 24h reads
   "intended-on but silent". Straight from the registry × `service_config`
   × `worker_logs` — a new pass needs **zero** edits here.
4. **Condition registry** (`workers/conditions.py`, spine Layer 2) —
   declarative probe rows under group `condition`: `pass-dead-on-host`
   (a handler silent past budget on ONE live host — per-`(host, process,
   handler)`, the resolution source 3 deliberately lacks; exact because
   every registered pass logs a `worker_logs` row every cycle),
   `rescue-pass-cadence` (fleet-wide SLO on sweeper/nursery/
   quest_loop_reconcile), `pass-wedged` (fresh heartbeat + stale
   `meta.activity.since`), `llm-degraded` (per model/transport/placement
   error rate), `dead-generation-claims` (epoch-dead claims outliving the
   claim reaper — the reclaim lane's own watchdog). Findings with a
   whitelisted heal run through `workers/bounded_heal.py`
   (attempts/cooldown/cap-then-gripe); the only action is restart-once
   (`cap=1`), dark until `PRECIS_RESTART_ONCE_ENABLED=1`.

## Findings → alerts

Each non-`ok` check raises a `kind='alert'` under
`alert_source="watchdog:<group>"` (fingerprint = the check name), severity
capped to `info`/`warn` — nursery keeps the `critical` lane, this pass never
pages. `resolve_stale_alerts` auto-closes whatever goes fresh again, same
dedup/lifecycle as nursery (see `precis-alert-help`).

## Remediation router (Phase 2)

An open `watchdog:<group>` alert that outlives its class's **self-heal
budget** (`cadence` 6h · `coherence` 24h · `discovery` 12h · other outcome
groups 24h at `warn`, never at `info`; the `meta` group — alert-backlog-rot
— never gripes) is routed to **exactly one** `kind='gripe'`, tagged
`origin:health-digest-router`. Recognize one by its first body line:

```
watchdog-condition: <alert_source>/<fingerprint>
```

That marker is the dedup AND auto-close key: the router re-scans open
gripes for it every eval, so a repeat sighting never duplicates, and the
moment the underlying check goes fresh the gripe is auto-closed (resolution
comment + soft-delete). **Don't hand-close these against a still-stale
condition** — they'll close themselves when the condition actually clears;
fixing the *cause* is the useful act (each carries a class-specific nudge:
the exact toggle for config drift, the stalled host for a cadence, the
first stuck pipeline stage for a backlog). A stale `embed` finding (and its
gripe) also carries the §F culprit line — which stage of materialize →
`embed_batch` → slot-gated claim is stuck. New gripes are flood-capped at 3
per eval; the overflow files next hour.

## Push policy

A templated digest (`kind='message'` → `PRECIS_OPS_ALERT_TARGET`, the same
`pg_notify('precis.messages')` → asa_bot path `notify_critical_alert` uses)
goes out when:

* the **daily heartbeat** is due (`app_settings['health_digest:last_push']`
  older than 24h) — an all-green push ("✅ all green") IS the internal
  dead-man's proof the watchdog itself is alive, or
* the finding set just **degraded** (any check went stale this eval).

No push when everything's green and the last push was under 24h ago.
Body selection (spine Layer 3 cutover): a degraded/daily push carries the
**doctor's report** (`doctor_tick`'s per-day draft, `meta.author='doctor'`)
when one is fresh; on staleness, absence, or any lookup failure it falls
back to the pure string template — grouped, worst/oldest-first, age shown
— so the digest still sends when the LLM is down. The all-green heartbeat
is always the template (it is the dead-man proof, never LLM-authored).
Detection stays SQL-only either way.

## External dead-man's-switch

After every successful eval, if `PRECIS_DEADMAN_PING_URL` is set,
`health_digest` GETs it (via `safe_fetch.safe_get`, SSRF-guarded) —
healthchecks.io-style. Covers the one failure mode nothing DB-mediated can:
a total fleet/DB outage. Dark by default. See
`docs/runbooks/dead-mans-switch.md` for setup.

## Related skills

* `precis-nursery-help` — the `critical`, page-now sibling tier (also
  carries the `host-dark` detector, gr186752's fix, which `health_digest`'s
  own `hosts_alive` check mirrors as a non-paging digest line)
* `precis-alert-help` — the `alert` kind (lifecycle, dedup, tab)
