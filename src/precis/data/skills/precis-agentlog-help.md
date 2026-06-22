---
id: precis-agentlog-help
title: precis — the agentlog kind (run attribution + touch graph)
summary: kind='agentlog' — one record per agentic run; carries the assembled prompt and `touched` links to every chunk it wrote; walk a suspicious chunk back to the run that produced it
applies-to: kind='agentlog'; precis.agentlog.open_log / touch_from_env / finalize_log / gc_stale_logs; PRECIS_CURRENT_AGENTLOG; /agentlogs web tab
status: active
---

# precis-agentlog-help — the `agentlog` kind

An **agentlog** is a *run-attribution record*: one row per agentic run
(a `plan_tick` coroutine, an operator-requested change, a chat
follow-up) that touched the corpus. It answers the question **"this
chunk looks wrong — who wrote it, and what were they told to do?"** by
storing:

* the full **assembled prompt** the run was handed (`meta.prompt`) —
  system + user, verbatim;
* the **model + source** (`meta.model`, `meta.source`) and the owning
  `todo` / `job` (`meta.parent_ref_id` / `meta.job_ref_id`);
* a **`touched` link to every chunk the run wrote or moved**, so the
  draft reader's Connections surface shows "written by run N".

It is the structural twin of `alert`: numeric-id, machine-produced,
**not embedded** (body in `title` + `meta`, no `card_combined`), so it
never reaches semantic search. Where an alert is a *condition*, an
agentlog is a *run*. Many runs may touch one chunk over its life — the
link graph carries the many-to-many.

## Shape

```
kind='agentlog'                  # numeric id, NOT embedded
title='plan_tick #<todo> (<model>)'
meta.source='plan_tick' | 'operator' | 'chat'
meta.model='opus' | 'sonnet' | ...
meta.prompt='<full assembled system+user prompt>'
meta.parent_ref_id=<owning todo>     meta.job_ref_id=<the tick's job>
meta.started_at / meta.ended_at / meta.status
tags=[agentlog-source:<source>]
links: agentlog --touched--> <draft chunk>   (one per chunk written/moved)
```

`touched` is registered **symmetric, no inverse** (like `related-to`):
one row per (run, chunk) edge, surfaced from either end. The bulky LLM
*transcript* stays on the owning `kind='job'` ref — the agentlog keeps
only the prompt, one hop from the transcript via `meta.job_ref_id`.

## Producer side (machinery only)

Runs write agentlogs through `precis.agentlog`:

* `open_log(store, source=, title=, model=, prompt=, parent_ref_id=,
  job_ref_id=)` — opens the record at run start, returns its id.
* Thread that id to the run's subprocess via **`PRECIS_CURRENT_AGENTLOG`**
  (same env back-door as `PRECIS_CURRENT_TODO`). The MCP server inside
  the run reads it and attributes every draft write/move to the run.
* `touch_from_env(store, draft_ref_id=, ords=)` — called by the draft
  handler after each write; a no-op when the env var is unset (operator
  console edits, tests), best-effort (never fails the edit).
* `finalize_log(store, log_id=, status=)` — stamps run-end state.

`plan_tick` already does all of this. New agentic write paths get
attribution for free by opening a log and setting the env var.

## Reading (agent side)

```
get(kind='agentlog', id=N)          # one run: prompt + model/source + tags
get(kind='agentlog', id='/recent')  # recent runs, newest-first
search(kind='agentlog', q='...')    # lexical over run titles
tag / link / delete                 # classify / relate / prune
```

No `put` / `edit` — agentlogs are opened by run machinery, not
hand-authored.

## Lifecycle / GC

The **sweeper** reaps agentlogs past `PRECIS_AGENTLOG_RETENTION_DAYS`
(default 30) via `gc_stale_logs`: it deletes the run's `touched` links
(pure attribution, worthless once the run is gone) but **never the
chunks** they point at — body chunks are append-only and survive. The
agentlog ref is soft-deleted, kept for forensics.

## Web

* `/agentlogs` — recent runs, grouped by source.
* `/agentlogs/{id}` — one run's assembled prompt + touched chunks + a
  link to the full transcript on its job.
* On the draft reader, a touched chunk shows an `agentlog:N` Connections
  chip → the run that wrote it.
