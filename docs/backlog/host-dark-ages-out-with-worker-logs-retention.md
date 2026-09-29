---
status: draft
title: host-dark stops firing once a dark host's worker_logs are pruned — the detector window and the retention horizon are both 30 days
prio: normal
model: sonnet
---

# host-dark stops firing once a dark host's worker_logs are pruned

## Motivation / why

`nursery._detect_host_dark` bounds itself to hosts seen recently in
`worker_logs`:

```sql
AND EXISTS (
    SELECT 1 FROM worker_logs wl
     WHERE wl.host = hh.host
       AND wl.ts > now() - (%(lookback)s || ' days')::interval
)
```

with `HOST_DARK_LOOKBACK_DAYS = 30`. The docstring states the intent
plainly: a decommissioned host's `host_heartbeat` row "lingers forever"
(it is a latest-snapshot UPSERT with no prune — `store/_heartbeat_ops.py`
has no DELETE), so without the bound it would "alarm critical forever".

The bound is compared against a table that is itself pruned on the same
horizon. `sweeper._gc_worker_logs` deletes `worker_logs` rows older than
`PRECIS_WORKER_LOG_RETENTION_DAYS`, default **30**. So a host that goes
dark and *stays* dark crosses both thresholds together: its last
`worker_logs` row ages past the retention floor, the `EXISTS` goes false,
and the detector can never emit a `host-dark` symptom for it again. The
alert ages out because the evidence was deleted, not because anyone
judged the host decommissioned.

For a genuinely retired host that is the desired behaviour. For a host
that broke and that nobody got to inside 30 days, it is a silent blind
spot that opens exactly when the outage becomes most serious. The two
cases are indistinguishable to the query: both present as "no recent
worker_logs".

**This is a latent mechanism, not an observed incident.** It was found
while investigating host `spark`, and the spark reading was WRONG — see
the decisions log. No host is known to have been lost this way.

## In scope

- Decide and implement a way to distinguish "decommissioned" from "dark
  past retention". Options worth weighing, not yet chosen:
  - an explicit decommission marker on `host_heartbeat` (a `meta.retired`
    stamp, set by an operator or a `precis host retire` verb), with the
    lookback bound replaced by "not retired";
  - keeping the lookback but sourcing it from something not pruned at the
    same horizon — `host_heartbeat.ts` itself already carries the last-seen
    time and survives;
  - making `HOST_DARK_LOOKBACK_DAYS` strictly greater than
    `PRECIS_WORKER_LOG_RETENTION_DAYS` so the window cannot be emptied by
    the pruner, with a test pinning the inequality.
- Whichever is chosen: a test that a host dark for > retention days still
  produces a `host-dark` symptom (or is provably marked retired).

## Explicitly NOT in scope

- Re-firing or escalating a *standing* open alert on age. The push policy
  in `health_digest` notifies on degrade-transitions and the daily
  heartbeat only; changing that is a separate argument with its own blast
  radius.
- The container-ID host exclusion (gr331348) — see the sibling item.
- Any change to `worker_logs` retention itself; 30 days is a storage
  decision, and this item must not smuggle in a storage change.

## Acceptance criteria

1. A host whose newest `worker_logs` row is older than
   `PRECIS_WORKER_LOG_RETENTION_DAYS`, whose `host_heartbeat` row is stale,
   and which carries no decommission marker, still yields a `host-dark`
   symptom from `_detect_host_dark`.
2. A host explicitly marked decommissioned yields no symptom, and the
   marker is discoverable (documented verb or field, not a hand-written
   SQL UPDATE).
3. A test pins the relationship between the detector's lookback and the
   log retention horizon, so a future change to either cannot silently
   re-open the gap.
4. `docs/runbooks/` or the nursery docstring states how a host is retired,
   since today the only way is to let the alert age out.

## Target + blast radius

`src/precis/workers/nursery.py` (`_detect_host_dark`,
`HOST_DARK_LOOKBACK_DAYS`), `src/precis/workers/sweeper.py`
(`_gc_worker_logs`, `PRECIS_WORKER_LOG_RETENTION_DAYS`),
`src/precis/store/_heartbeat_ops.py` (the UPSERT, if a marker lands
there). Alert sources `host-dark` / `dead-worker` — both critical, so a
regression here is a paging change.

## Open questions / decisions log

- **Retracted, 2026-09-29: the spark reading that led here was wrong.**
  The investigation began from "host spark has been dark a month and
  nothing told us". Every part of that is false. spark is **retired**
  (decommissioned 2026-08-29); the live fleet is melchior, balthazar,
  castor, pollux — the only four rows in `host_heartbeat`, all fresh.
  `host-dark` and `dead-worker` **did** fire for spark on 2026-08-29, and
  resolved appropriately. Nothing failed. Two intermediate subagent
  reports also claimed spark had "never reported worker_logs at all",
  which was an artifact of the 30-day prune having already removed them —
  that claim must not be carried forward.
- Open: is an explicit retire marker worth the operator burden, given
  ageing-out has so far only ever retired hosts correctly? A cheaper
  variant is to keep ageing-out but emit one terminal "host aged out of
  monitoring" event, so the transition is journalled rather than silent.
- Open: should `dead-worker` get the same treatment? It shares
  `DEAD_WORKER_LOOKBACK_DAYS` and the same `worker_logs` dependency, so
  the same reasoning probably applies, but it was not examined here.
