---
id: precis-fleet-help
family: work
title: precis — the fleet kind (agent fleet state, coordinator view)
summary: kind='fleet' — one live row per agent tree plus one per host; collectors push reports, the coordinator reads the fisheye and sets assigned/slice/note
answers:
  - which agents are running, on which host, and what are they doing?
  - which agents need attention (dead, waiting, asking, quiet, context full)?
  - how do I attach to an agent's tmux pane?
  - how do I record what an agent is assigned to?
  - how much quota is left per vendor?
  - why does a host show as stale?
applies-to: kind='fleet'; scripts/fleet-report --push; sweeper dead-row retirement
status: active
tags: [workflow]
kinds: [fleet]
---

# precis-fleet-help — the `fleet` kind

Live state of the agent fleet. Not embedded, not searchable.

## Read

```
get(kind='fleet')                              # EXCEPTIONS / AGENTS / QUOTA
get(kind='fleet', args={'project': 'precis-mcp', 'host': 'melchior'})
get(kind='fleet', id='claude:melchior:precis-mcp:main')   # one row by key
get(kind='fleet', id='fl123')                  # one row by handle
```

AGENTS line: `vendor@host/project/tree · state · quiet · purpose · ctx%`.
`quiet` is derived from `last_active`. A host that has not reported for over
3 minutes marks all its agents `STALE` and appears under EXCEPTIONS.

## Rows

| type | key | fields |
|---|---|---|
| agent | `vendor:host:project:tree` | reporter-owned: `branch purpose state last_active ctx_pct dirty ahead behind pane attach exceptions`; coordinator-owned: `assigned slice note` |
| host | `host:<host>` | `reported_at quota reporter_version exceptions_hash` |

State: `working idle asking waiting dead`. One row per vendor per tree. A
renamed or moved tree gets a new key; the old row goes `dead` and is retired
24 h later.

## Write

```
put(kind='fleet', mode='report', args={'host': 'melchior', 'report': <fleet-report --json>})
edit(kind='fleet', id='fl123', mode='replace', args={'assigned': 'gr12345', 'note': 'blocked on review'})
```

`put` takes only `mode='report'`. `edit` takes only `assigned`, `slice`,
`note` (`''` clears); a report never overwrites them, and reporter-owned
fields are refused.

Collector, on each host: `scripts/fleet-report --push` (set
`PRECIS_MCP_TOKEN`; `PRECIS_FLEET_MCP_URL` overrides the endpoint).
`--remote HOST` pushes another host's report through this one.
