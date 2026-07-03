---
description: One honest "what needs doing" across the two work substrates — repo dev work (OPEN-ITEMS backlog + open gripes) and the prod factory queue (open/doable todos) — plus the latent LLM-confusion signal mined from prod agent transcripts.
argument-hint: "[optional focus, e.g. 'dark-factory' or 'drafts']"
allowed-tools: Read, Bash(grep:*), Bash(ssh:*), mcp__precis__get, mcp__precis__search
---

Work lives in **two different substrates** — do not merge them into one flat
list, that is the trap this view exists to avoid. Optional focus: `$ARGUMENTS`.

- **Repo dev work** — the `OPEN-ITEMS.md` backlog + the `gripe` bug tracker.
  These are about *this codebase / product*: MCP-surface bugs, features, infra
  fixes. You act on them by **editing code in a worktree → `/go`**. "Inert"
  here means: real, but no one is building it yet.
- **Prod factory queue** — `kind='todo'` rows in the **prod DB**, driven by the
  autonomous dispatch/planner loop on the cluster. These are *content/ops
  output* ("write section X", "morning briefing", "citation audit", "import
  book Y"), **not code**. You do **not** fix these by editing this repo — they
  self-run, or get retried / unblocked / halted **on prod**.
- **The bridge** — the only real overlap: a prod todo that keeps *failing
  because of a repo bug*. That failure is a symptom; the fix is dev work here.
  Call these out explicitly — they're where a `/go` clears a prod backlog.

The backlog + gripes are *declared* repo dev work. Beyond them is *latent*
repo dev work — bugs the LLM is hitting on prod right now that nobody has
filed yet (step 4, the bug-hunt). Every recurring tool-call error is a fix
waiting in a skill or the MCP surface; mining it feeds new items into
substrate 1 (as gripes).

Live backlog headings (repo `OPEN-ITEMS.md`):
!`grep -nE '^(## |- \*\*|- \[ \])' OPEN-ITEMS.md 2>/dev/null | head -60`

## Procedure

1. **Repo dev — backlog.** Read `OPEN-ITEMS.md`. Take only *open* items (skip
   shipped / done / deferred / retired). The dark-factory workstream is active.
2. **Repo dev — gripes.** `get(kind='gripe', id='/open')` (the bug tracker).
   Tracked but **not auto-worked** — flag stale or high-impact ones.
3. **Prod factory queue — todos.** `search(kind='todo', view='attention')`
   (asking-user + failed children) and `search(kind='todo', view='doable')`
   (what the loop picks up next). NB: these are `search(...)` calls, not
   `get(...)`. This is the only substrate that acts on itself.
4. **Latent repo dev — LLM-confusion mining (the bug hunt).** The server-side
   agent runs (`plan_tick`, dream, cad/structure propose) store their full
   `claude -p` tool-call transcript in `refs.meta.transcript` on the
   `kind='job'` ref. Every `[error:...]` in a transcript is the LLM getting a
   verb wrong — a fix waiting in a skill or in the MCP surface. There is **no**
   interactive tool-call ledger (the live `precis serve` path logs nothing), so
   these job transcripts are the signal. Prod-hop (`agent_rw` has SELECT; see
   CLAUDE.md "Peeking at prod"), pull the last 48h, and rank error shapes:
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
   (`put(kind='gripe', ...)`) so it enters substrate 1, or fix it directly.
5. **Group by substrate, then rank.** Keep the two substrates visually
   separate; within each, highest-impact first with a one-line next action.
   Latent bugs from step 4 fold into substrate 1 as new/unfiled repo dev work.
   Dedup the bridge (a gripe whose real cause is a failing todo, or vice
   versa). If `$ARGUMENTS` is set, scope to it.
6. **Call out the gap honestly.** Per substrate: which repo items are
   **actionable here** (fix → `/go`) vs blocked; which todos are **autonomous**
   (the loop will run) vs **stalled** (bubbled/halted, needing a prod unblock
   or a repo bugfix). End with the single highest-leverage next action — and
   say which substrate it lives in, so the reader knows whether it's a `/go` or
   a prod op.

Keep it tight — this is a triage view, not a full read of every item.
