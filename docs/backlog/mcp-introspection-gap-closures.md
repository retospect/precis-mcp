# mcp-introspection-gap-closures

Approved Reto 2026-10-14 with two rulings: job filters are HIERARCHICAL
(jobs for a quest/board/structure include sub-jobs and jobs under its
todo tree), and the widened call ledger includes `arg_sample`. Evidence:
MCP usage mining 24h — prod-psql detours on `refs` x94, `worker_logs`
x35, `ref_tags` x15; `search(kind='job', view='count')` refused since=.
Gripes gr484235 (worker_logs/alert context) and gr484241 (refs/ref_tags
introspection; the information_schema leg was withdrawn as legitimate).

## Slices

1. **Job stats.** `search(kind='job', view='count')` accepts
   `since=`/`until=` (ISO date/timestamp, bounds `refs.created_at`,
   same parser contract as the source-search primitive) and the
   existing `group_by='meta.job_type,state'` facet. The job handler's
   count view additionally renders top-N error heads per failing
   job_type: latest `job_event` first line per failed job in the same
   filtered set, normalised (digits→#), counted, capped.
2. **Hierarchical job filters.** `JobHandler.owns_under = True`:
   `search(kind='job', under='<ref>')` resolves any ref, walks
   `refs.parent_id` down (bounded depth, `refs_parent_id_idx`) and
   subject links (either direction) to member refs, then lists the
   jobs parented under or linked to the member set. `status=` filters
   the job STATUS tag; `view='tree'` renders the compact hierarchy;
   `view='count'` composes by feeding the resolved job-id set into the
   shared count path.
3. **Meta-path view.** `search(kind=K, view='meta')` — per-kind
   `jsonb_each(refs.meta)` inventory: key, jsonb type, live-ref count,
   bounded example. Read-only, cached in-process (~15 min TTL).
   `args={'key': 'params'}` drills one level into an object-valued key.
4. **Tag counts.** `search(kind=K, view='tags')` — per axis/value
   counts over `ref_tags` for live refs of one kind; `status=`/`tags=`
   narrow the counted set; expired ref_tags excluded.
5. **Alert context.** `get(kind='alert', id=N, view='context')` —
   alert summary + `worker_logs` lines from the host/process its
   fingerprint (or meta) names, in a padded window around first/last
   seen. Bounded lines, `mask_secrets` on every message.
6. **Ledger widening (migration 0209).** `tool_calls` gains
   `session_id`, `seq`, `result_bytes`, `error_head`, `arg_sample`
   (JSONB — scalar addressing values only, truncated, mask_secrets'd;
   never text/title/body values). session_id is minted per bound MCP
   session object in `tool_ledger.current_session_key`
   (WeakKeyDictionary, `host-pid-uuid` — never `id()`, GC recycles);
   `seq` is the per-session in-process counter. CLI/inproc calls keep
   NULL session_id — `agentlog_id` is already their correlation.
   mine-sessions/extract maps the new columns (session falls back
   `agentlog:` → day bucket) and `by_session` admits real-session
   ledger rows so session-adjacency detectors can order by actual
   session+seq.
7. **Skills + hints.** precis-job-help / precis-alert-help /
   precis-search-help updated; `next:` hints teach the new views.
   Gripe comments on gr484235/gr484241.

## Out of scope

No deploy, no ship, no prod writes beyond the two gripe comments the
brief requires. Release owner lands.
