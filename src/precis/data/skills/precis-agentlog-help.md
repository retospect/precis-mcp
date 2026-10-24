---
id: precis-agentlog-help
family: work
title: precis — the agentlog kind (run attribution + touch graph)
summary: kind='agentlog' — one record per agentic run; the assembled prompt plus `touched` links to every chunk it wrote; walk a suspicious chunk back to its run
answers:
  - which agent run wrote or touched this chunk?
  - what does an agentlog record carry, and how long is it kept?
applies-to: kind='agentlog'; PRECIS_CURRENT_AGENTLOG; /agentlogs web tab
status: active
tags: [troubleshooting]
kinds: [agentlog]
---

# precis-agentlog-help — the `agentlog` kind

An **agentlog** is a *run-attribution record*: one row per agentic run
(a `plan_tick` coroutine, an operator-requested change, a chat
follow-up) that touched the corpus. It answers **"this chunk looks
wrong — who wrote it, and what were they told to do?"** by storing the
full assembled prompt (`meta.prompt`, system + user verbatim), the
model + source + owning `todo`/`job` (`meta.model`, `meta.source`,
`meta.parent_ref_id`, `meta.job_ref_id`), and a **`touched` link to
every chunk the run wrote or moved** — the draft reader's Connections
surface shows "written by run N".

It is the structural twin of `alert`: numeric-id, machine-produced,
**not embedded** (body in `title` + `meta`), so it never reaches
semantic search. Where an alert is a *condition*, an agentlog is a
*run*; the link graph carries the many-to-many.

## Shape

```
kind='agentlog'                  # numeric id, NOT embedded
title='plan_tick #<todo> (<model>)'
meta.source='plan_tick' | 'operator' | 'chat'
meta.model='opus' | 'sonnet' | ...   # the REQUESTED name (tier/pin)
meta.model_actual='claude-sonnet-5' | ...   # the id the router dispatched
meta.models_used=['claude-sonnet-4-5-20250929', ...]  # exact versioned ids
        # (claude_agent runs only, from the stream result's modelUsage)
meta.prompt='<full assembled system+user prompt>'
meta.parent_ref_id=<owning todo>     meta.job_ref_id=<the tick's job>
meta.started_at / ended_at / status
tags=[agentlog-source:<source>]
links: agentlog --touched--> <draft chunk>
```

`touched` is **symmetric, no inverse** (like `related-to`): surfaced
from either end. The bulky LLM *transcript* stays on the owning job —
the agentlog is one hop from it via `meta.job_ref_id`.

## Producer side (machinery only)

Runs write agentlogs through `precis.agentlog`: `open_log` at run
start, **`PRECIS_CURRENT_AGENTLOG`** threads the id to the run's
subprocess (the MCP server inside reads it and attributes every draft
write/move), `finalize_log` stamps run-end state. `plan_tick` already
does all of this — a new agentic write path gets attribution by
opening a log and setting the env var. `stamp_result_meta(store,
log_id=, res=)` writes the resolved-model ids (`meta.model_actual`,
`meta.models_used`) from the run's `LlmResult`; `meta.model` is only the
requested tier/pin.

## Reading (agent side)

```
get(kind='agentlog', id=42)         # one run
get(kind='agentlog', id='/recent')  # recent runs, newest-first
search(kind='agentlog', q='...')    # lexical over run titles
tag / link / delete                 # classify / relate / prune
```

No `put` / `edit` — agentlogs are opened by run machinery, not
hand-authored.

## Lifecycle / GC

The sweeper (`gc_stale_logs`) reaps agentlogs past
`PRECIS_AGENTLOG_RETENTION_DAYS` (default 30): `touched` links go —
they're pure attribution — but **never the chunks** (append-only).
The ref is soft-deleted, kept for forensics.

## Web

`/agentlogs` lists recent runs by source; `/agentlogs/{id}` shows one
run's prompt + touched chunks + the transcript link. A touched chunk
shows an `agentlog:N` Connections chip on the draft reader.

## See also

- [[precis-alert-help]] — the structural twin: a condition, not a run
- [[precis-job-help]] — the owning job's full transcript, one hop from
  `meta.job_ref_id`
