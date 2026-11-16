---
id: precis-machine-help
family: work
title: precis — the machine kind (one ref per fleet host)
summary: kind='machine' — one ref per host (id ma_<host>); stable facts in the body, live load/temperature/GPU/utilisation overlaid from the heartbeat at read time; the first heartbeat auto-creates the ref
answers:
  - how do I point a job or memory at the host it ran on?
  - which hosts are live, stale, or have no heartbeat?
  - how do I list machines by platform or role?
  - how do I see the code SHA each live worker daemon is actually booted on?
  - how do I create or refresh the machine refs?
  - where do load and temperature come from, and why does a host show 'no heartbeat'?
  - how do I record a host's platform, role or GPU?
  - how do I see per-GPU utilisation and memory, and the fleet GPU line?
applies-to: get/search/put/tag/link (kind='machine')
status: active
tags: [workflow]
kinds: [machine]
---

# precis-machine-help — the `machine` kind

One ref per host in the fleet, `ma_<host>` (also `mh<N>`). The body holds
**stable facts only** — `host`, `platform`, `role`, `gpu` — so jobs,
memories and web pages have something durable to link. Live state (load
average, CPU temperature, per-card GPU utilisation/memory, utilisation
means, last-seen) is **read at request time** from the heartbeat reporter
and overlaid on `get` — it is never stored on the ref, so a one-minute
collector never churns the graph.

**Refs maintain themselves**: the first heartbeat from an unseen,
non-retired host creates its `ma_<host>` ref automatically (gr592809) —
there is no manual step when the fleet changes. `put(kind='machine',
mode='sync')` remains the hand-refresh of facts, not a prerequisite.

## Read one host

```python
get(kind='machine', id='ma_alpha')     # or id='alpha', id='mh12'
```

Body shows the stable facts, then the overlay:

- `status: live` — heartbeat within ~10 min · `stale` — older ·
  `no heartbeat` — the host has no reporter row at all. A daemon-free host
  reads `no heartbeat`, never `down`.
- `load (1/5/15m)`, `temp_c` (`n/a (no sensor)` where unreadable), and the
  last-seen timestamp UTC + age.
- Per NVIDIA card, one line from the live sample:
  `gpu 0: NVIDIA GB10 — util 35%, mem 4200/16384 MiB` (`n/a` where the
  card answered `[N/A]` — unified-memory cards have no MiB counters).
  Hosts without `nvidia-smi` show no gpu lines.
- `util 1h/24h mean: cpu 10%/9%, gpu 33%/31%` — the window means of the
  per-beat CPU and per-card-mean GPU utilisation series
  (`host_heartbeat_log`). Absent entirely when no beat in the window
  sampled utilisation.

## List / filter

```python
search(kind='machine')                          # every ref, status per row
search(kind='machine', platform='linux')        # case-insensitive substring on the fact
search(kind='machine', role='worker')
search(kind='machine', status='stale')          # live | stale | 'no heartbeat'
search(kind='machine', q='alpha')               # match on host name
```

## Fleet GPU line

```python
get(kind='machine', view='gpu')
```

One row per GPU host — a ref whose heartbeat carries `gpu_stats` or that
has a `resource_slots` `gpu` row (a host with neither is not listed):
`gpus` (live card count, else slot capacity), `gpu_util_24h` (mean of the
per-beat card-mean utilisation over 24 h), `seed_slots` (GPU slots in use
/ capacity). This is the weekly report's "are the GPUs still idle?" line
(me538374's measurement, now continuous). It takes no `q=` — it is the
whole fleet.

## Daemon cohort — release attestation

```python
get(kind='machine', view='daemons')      # the live release daemon cohort
search(kind='machine', view='daemons')   # same read via search
```

One row per live host that attests for a deploy — the cohort
`release_confirm` counts. With a published `deploy.fleet` `covered`
list only those configured members attest; without one, worker
evidence (installed units, recorded activity, or a pre-snapshot beat)
decides. Each row carries the heartbeat `code_sha` (**the last-beating
process's boot version** — host-wide, last writer wins) and beat time
beside the installed `worker_units` snapshot (**installed metadata**,
not the running version — never read one as the other). The
`processes` column shows each named process's own `@<sha>` and beat
age from `meta.code_sha_by_process`; names marked 'persistent keys'
have no per-process evidence — a stopped process's merge key survives,
so key presence is never liveness. Ephemeral, container-id and
>60-min-stale beats are excluded; live non-attestors show as a note,
never counted. A single host's `get` overlays the same `code_sha` /
units lines. The header shows the ledger's deployed sha when published.

## Refresh refs and facts

```python
put(kind='machine', mode='sync')                # refresh facts for every beating host
put(kind='machine', mode='sync', args={'hosts': ['gamma']})   # + hosts that never beat
put(kind='machine', id='gamma', args={'platform': 'Linux', 'role': 'worker', 'gpu': 'none'})
```

`sync` refreshes `platform`/`release` from the reporter and preserves
hand-set `role`/`gpu`; it never creates refs for retired hosts. The
single-host form is the upsert for facts the collector can't see. The
heartbeat auto-create mints a factless ref, so run `sync` after a host
joins if its facts matter immediately; day to day the beat keeps the
census current with no put at all. Host role/status claims found in
memory prose are the gardener's `host-facts` pass (gr560767): it
proposes the fact moved onto this ref plus the memory sentence
rewritten to a dated event plus your handle.

## Link to it

```python
link(kind='job', id=55, target='machine:ma_alpha')          # "ran on alpha"
link(kind='machine', id='ma_alpha', target='memory:me47')   # host facts -> a note
get(kind='machine', id='ma_alpha', view='links')
```

## Distinct from

`fleet` host rows are agent-quota coordination state, not searchable.
`machine` is the durable, linkable, searchable host identity — the two
share only the heartbeat collector.
