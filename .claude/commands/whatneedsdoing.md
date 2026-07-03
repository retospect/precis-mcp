---
description: One honest "what needs doing" across all work stores — the OPEN-ITEMS backlog, open gripes, open/doable todos, and the LLM-confusion signal mined from prod agent transcripts.
argument-hint: "[optional focus, e.g. 'dark-factory' or 'drafts']"
allowed-tools: Read, Bash(grep:*), Bash(ssh:*), mcp__precis__get, mcp__precis__search
---

Work lives in several separate places and nothing unifies them automatically.
Pull them all, merge, and give one ranked list. Optional focus: `$ARGUMENTS`.
Sources 1–3 are *declared* work; source 4 is *latent* work — bugs the LLM is
hitting right now that nobody has filed yet. Source 4 is the bug-hunt: every
recurring tool-call error is a fix waiting in a skill or in the MCP surface.

Live backlog headings (repo `OPEN-ITEMS.md`):
!`grep -nE '^(## |- \*\*|- \[ \])' OPEN-ITEMS.md 2>/dev/null | head -60`

## Procedure

1. **Backlog** — read `OPEN-ITEMS.md`. Take only *open* items (skip anything
   marked shipped / done / deferred / retired). The dark-factory workstream
   section is the active one.
2. **Gripes** — `get(kind='gripe', id='/open')` (the bug tracker). These are
   *tracked* but **not auto-worked** — flag any that are stale or high-impact.
3. **Todos** — `get(kind='todo', view='attention')` (asking-user + failed
   children) and `get(kind='todo', view='doable')` (what the autonomous
   dispatch/planner loop will actually pick up next). This is the only store
   the factory acts on by itself.
4. **LLM-confusion mining (the bug hunt).** The server-side agent runs
   (`plan_tick`, dream, cad/structure propose) store their full `claude -p`
   tool-call transcript in `refs.meta.transcript` on the `kind='job'` ref.
   Every `[error:...]` in a transcript is the LLM getting a verb wrong — a
   fix waiting in a skill or in the MCP surface. There is **no** interactive
   tool-call ledger (the live `precis serve` path logs nothing), so these job
   transcripts are the signal. Prod-hop (`agent_rw` has SELECT; see CLAUDE.md
   "Peeking at prod"), pull the last 48h, and rank error shapes:
   ```sql
   -- histogram of confusion, most-frequent first
   WITH tx AS (
     SELECT meta->>'transcript' t FROM refs
     WHERE kind='job' AND meta ? 'transcript'
       AND created_at > now() - interval '48 hours'),
   m AS (SELECT (regexp_matches(t,
           '\[error:[A-Za-z]+\][^"\\]{0,140}', 'g'))[1] err FROM tx)
   SELECT err, count(*) FROM m GROUP BY err ORDER BY 2 DESC LIMIT 40;
   ```
   For the top shapes, pull one offending transcript and pair each error with
   the tool-call that produced it (walk the stream-json: map `tool_use.id` →
   `input`, join to the `tool_result` carrying `[error:`). That call+error pair
   tells you whether the fix is a **skill** edit (the LLM was never told the
   contract), an **MCP** fix (misleading error message, or a genuine handler
   bug), or a **task-template** fix (a stored todo instruction teaches a broken
   call). Watch for *spin*: one parent re-minting the same failing tick for
   days (dozens of transcripts, same error) is both expensive and a loud bug
   signal — treat it as P0. File each distinct root cause as a `gripe`
   (`put(kind='gripe', ...)`) so it enters store 2, or fix it directly.
5. **Merge + rank.** Dedup across stores (a gripe may already have a todo).
   Present grouped by source, newest/highest-impact first, each with a
   one-line recommended next action. If `$ARGUMENTS` is set, scope to it.
6. **Call out the gap honestly.** Mark which items are **autonomous** (todos
   the loop will run) vs **inert** (OPEN-ITEMS entries and gripes that aren't
   todos yet, so nothing works them until promoted). End with the single
   highest-leverage next action.

Keep it tight — this is a triage view, not a full read of every item.
