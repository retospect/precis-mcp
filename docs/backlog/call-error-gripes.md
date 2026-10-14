---
status: in-progress
title: Auto-file gripes from failed MCP calls in the tool ledger (gr484070)
pillar: platform
---

# call-error gripes from the tool ledger (gr484070)

Approved by the gr484070 task brief (Reto 2026-10-14): capture lives
server-side in the tool ledger, not agent self-report — the open
question is closed. Built on ef717dcf3 (mcp-introspect, migration
0209: per-session tool ledger with masked `arg_sample`/`error_head`)
and cb9c5f551 (`scripts/mine-sessions/` offline transcript mining).

## Slices

1. **Caller attribution (migration 0210).** `tool_calls` gains
   `client TEXT` — the MCP client's `clientInfo` `name/version`, plus
   `(model:<id>)` when the client sends a model id in the initialize
   `meta`. Read from the bound `serve_ledger` session in
   `tool_ledger.current_client()`; NULL for CLI/inproc. Store what the
   client sends, never guess.
2. **`call_errors` cadence.** A `workers/call_errors.py` pass on the
   scheduler (daily, off the exact day boundary): read the last 24h of
   `tool_calls` rows with `outcome='error'` and
   `error_type IN ('BadInput','Unsupported')` (rejected before real
   work), cluster by `(verb, kind, normalize_err_head(error_head))`,
   and for each cluster over threshold (`>=3` calls or `>=2` distinct
   sessions) file ONE gripe — or bump the live one by appending a
   count comment. Dedupe on `refs.meta->>'call_cluster'` = sha256 of
   the cluster signature; live = non-retired, non-terminal STATUS
   (open/triaged/ready_for_fix/in_review), so a `done`/`wontfix`
   cluster that recurs re-files.
3. **Classification tag.** Deterministic, ordered: `skill` (a skill
   file documents the rejected call shape — the doc shows a form the
   server rejects), else `hint` (the rendered error was a single line —
   no `next:` — or the same session repeated the identical failing
   call, i.e. the error didn't teach the fix), else `mcp` (the head
   matches a param-gap pattern — unknown key/kind, missing required,
   bad mode/view), else `needs-triage` (never guess). Tag is
   `callerr:<class>`; body carries counts, distinct sessions, clients
   seen, 2–3 masked arg samples, the error head, and the documenting
   skill if any.
4. **Flood cap.** Max 5 new gripes per run; overflow count logged in
   the run log.

## Out of scope

Call-pattern analytics over *all* calls (the gripe's item 5) — the
ledger already stores every call; a backlog note
(`mcp-call-pattern-analytics.md`) records the deferred ask. No ship,
no deploy, no prod run of the pass.

## Acceptance

- `tool_calls.client` populated for MCP calls; NULL otherwise.
- Over-threshold clusters file one gripe each (capped at 5/run);
  repeat runs bump the live gripe, never duplicate.
- `done`/`wontfix` clusters re-file on recurrence.
- One test per classification category + an ambiguous case; masked
  samples only — no raw secret reaches a gripe body.
