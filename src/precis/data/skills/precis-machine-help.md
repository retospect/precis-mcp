---
id: precis-machine-help
family: work
title: precis — the machine kind (one ref per fleet host)
summary: kind='machine' — one ref per host (id ma_<host>); stable facts in the body, live load/temperature/last-seen overlaid from the heartbeat at read time; sync mints the refs
answers:
  - how do I point a job or memory at the host it ran on?
  - which hosts are live, stale, or have no heartbeat?
  - how do I list machines by platform or role?
  - how do I create or refresh the machine refs?
  - where do load and temperature come from, and why does a host show 'no heartbeat'?
  - how do I record a host's platform, role or GPU?
applies-to: get/search/put/tag/link (kind='machine')
status: active
tags: [workflow]
kinds: [machine]
---

# precis-machine-help — the `machine` kind

One ref per host in the fleet, `ma_<host>` (also `mh<N>`). The body holds
**stable facts only** — `host`, `platform`, `role`, `gpu` — so jobs,
memories and web pages have something durable to link. Live state (load
average, CPU temperature, last-seen) is **read at request time** from the
heartbeat reporter and overlaid on `get` — it is never stored on the ref,
so a one-minute collector never churns the graph.

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

## List / filter

```python
search(kind='machine')                          # every ref, status per row
search(kind='machine', platform='linux')        # case-insensitive substring on the fact
search(kind='machine', role='worker')
search(kind='machine', status='stale')          # live | stale | 'no heartbeat'
search(kind='machine', q='alpha')               # match on host name
```

## Create or refresh refs — run by hand

```python
put(kind='machine', mode='sync')                # one ref per host the heartbeat knows
put(kind='machine', mode='sync', args={'hosts': ['gamma']})   # + hosts that never beat
put(kind='machine', id='gamma', args={'platform': 'Linux', 'role': 'worker', 'gpu': 'none'})
```

`sync` refreshes `platform`/`release` from the reporter and preserves
hand-set `role`/`gpu`. The single-host form is the upsert for facts the
collector can't see. Nothing runs sync for you — call it when the fleet
changes.

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
