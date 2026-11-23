# Graph gardener on prod — pause, resume, re-route, read the logs

**When.** The graph gardener (`graph_garden` service; spec
me693885 (me693885),
skill `precis-gardener-help`) is running on prod with its proposer on a
local model and you need to stop it, restart it, change where its LLM calls
go, or find out what it did. State as of 2026-11-01: lit since 2026-10-21,
runs every 2 h from the recurring todo td504852, proposer on the local-serving node
(`openai/gpt-oss-120b`, SGLang, ctx 131072) with **no cloud fallback by
design** (local-serving-node failures must be visible), consumer on cloud Opus.

Addresses live in the gitignored overlay; none appear here.

## Pause / resume

```
scripts/prod-precis service prio <agent-host> graph_garden 0   # pause
scripts/prod-precis service prio <agent-host> graph_garden 1   # resume
```

The service row is `service_config` for the agent host worker; prio 0 stops
the scheduler minting runs, in-flight runs finish.

## Re-route or revert the proposer's LLM chain

The proposer reads the `app_settings` key `llm.op.graph_garden:propose`
(local chain only: the local-serving node `openai/gpt-oss-120b`, max_tokens 4000,
reasoning medium). To revert to the default routing, delete that row:

```
scripts/prod-psql "DELETE FROM app_settings WHERE key = 'llm.op.graph_garden:propose'"
```

To set a chain from the CLI: `scripts/prod-precis llm op set
graph_garden:propose --chain … --local` (`--chain`/`--local` write a chain
since 87472364e; before that the CLI could not, gr502328). There is no MCP
read for the key yet (gr548280); until then `scripts/prod-psql --ro
"SELECT key, value, updated_at FROM app_settings WHERE key LIKE
'llm.op.graph_garden%'"`.

## Read what it did

- Runs: `search(kind='job', q='graph_garden')` / `get(kind='job', id=…)`
  for a run's candidates, proposals and merges applied.
- Verdict counts (approved / rejected / held-for-human) live in the
  `reviews` ledger, actor `garden-consumer` (consumer) and `graph-garden`
  (proposer); CLI `scripts/prod-precis garden stats`. No MCP door yet
  (gr548280).
- Logs: the agent host workers run under launchd — grep the
  `precis-worker*.log` files on the host, not `journalctl`; the agent
  lane's stdout is `/var/log/precis-worker-agentlane.log` (the old
  `precis-worker-agent.log` is a retired unit's frozen 0-byte file —
  gr508975, gr638954).

## Trial and schedule memory-change

After the worker serves the code with the `memory-change` pass, queue a
single bounded job under the rollout todo between recurring ticks. The
normal service and cooling gates still apply.

```python
put(kind='job', args={'parent_id': rollout_todo_id, 'job_type': 'graph_garden',
                      'params': {'pass': 'memory-change', 'consume': False}})
```

Check the completed job's `meta.garden` for candidates, per-detector
proposals, errors, skipped or unavailable checks, and cursor progress.
Inspect the proposal evidence for the recorded source or entity changes.
This trial runs the proposer and can write proposals for later review.
If a check was skipped or unavailable, resolve the cause before using its
yield to judge the trial.

After reviewing the trial, read the current `meta.params` on td504852.
Preserve its other keys and append `memory-change` to its pass list. The
observed list becomes `["merge", "relink", "paper-identity", "memory",
"memory-change"]`. Update the whole params object with
`edit(kind='todo', id='td504852', meta={'params': updated_params})`.
Confirm the next spawned job carries the new list and check both
detectors' results. Removing `memory-change` from the list rolls back
cadence selection.

## Undo a merge

A gardener merge keeps the prior in `links.meta.prior_relation` /
`prior_axis` and the merged ref's `superseded_by`; undo is one query per
merge — see the audit procedure on gr512125 (comments 5–6 undid the wrong
and suspect merges of 2026-10-24). The four pre-gate merges from that day
(reviews 8830, 8833, 8845, 8846) were audited 2026-11-01 (gr512125
comment 9; summary me570668): 8845 undone as a version pair, 8846 and
8833 repaired under gr548654 (loser PDF/body attached to the survivor),
8830 left — its survivor was already folded into a digest.

## Reto's frame for what the gardener may write

A link earns its place only where RAG cannot recompute it: stance, a
distant connection, a settled judgment, provenance, identity. Everything
else is a retrieval result, not an edge (`graph-gardener-value.md`, the
argument section).
