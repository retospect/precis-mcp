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
get(kind='fleet')                              # EXCEPTIONS / READY / AGENTS / QUOTA
get(kind='fleet', args={'project': 'precis-mcp', 'host': 'melchior'})
get(kind='fleet', id='claude:melchior:precis-mcp:main')   # one row by key
get(kind='fleet', id='fl123')                  # one row by handle
```

AGENTS line: `vendor@host/project/tree tmux · state · quiet · purpose ·
ctx%` (tmux target like `0:7`, omitted for pane-less rows). `quiet` is
derived from `last_active`. ctx% comes from the live pane footer when one
shows it (`Context: …`, `N% ctx`, Codex `Context N% left`), else the
transcript's usage against the per-model window. A host that has not
reported for over 3 minutes marks all its agents `STALE` and appears under
EXCEPTIONS.

READY lists trees whose `.claude/purpose` first line reads `<tree> at
<sha>: …` — a session that wrote its done line; the AGENTS line ends
`· done@<sha>` for those.

## Rows

| type | key | fields |
|---|---|---|
| agent | `vendor:host:project:tree` | reporter-owned: `branch purpose state last_active ctx_pct dirty ahead behind pane tmux window_name attach exceptions via`; coordinator-owned: `assigned slice note` |
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
`PRECIS_MCP_TOKEN` or `PRECIS_MCP_TOKEN_FILE`;
`PRECIS_FLEET_MCP_URL` overrides the endpoint). `--remote HOST` pushes
another host's report through this one. Operator install/scheduling of
the push lives in `scripts/fleet-report-install --help` (launchd, macOS).

Session-hook push: `scripts/fleet-report --hook --push` reports only the
calling session (state from `--state` or the `hook_event_name` on stdin;
ctx% from the own pane footer, else the Claude transcript; tmux target
from `$TMUX_PANE`) as a *partial* report. A partial report upserts rows
and keeps the host fresh but never dead-marks absent rows and never
clobbers host `quota`/`exceptions_hash`. Its rows carry `via: "hook"`: a
full host report patches the fields it sees but does not dead-mark a
hook-owned row (the session says `dead` itself on SessionEnd). Wiring:
`.devin/hooks.v1.json` in this repo covers Devin
(SessionStart/UserPromptSubmit → working, PermissionRequest → waiting,
Stop → idle, SessionEnd → dead); the Claude `.claude/settings.json`
entry and the disable knob live in docs/runbooks/fleet-session-push.md.
Hook mode never breaks the session — push failures go to stderr with
exit 0.
