---
description: One honest "what needs doing" across all three work stores — the OPEN-ITEMS backlog, open gripes, and open/doable todos.
argument-hint: "[optional focus, e.g. 'dark-factory' or 'drafts']"
allowed-tools: Read, Bash(grep:*), mcp__precis__get, mcp__precis__search
---

Work lives in three separate places and nothing unifies them automatically.
Pull all three, merge, and give one ranked list. Optional focus: `$ARGUMENTS`.

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
4. **Merge + rank.** Dedup across stores (a gripe may already have a todo).
   Present grouped by source, newest/highest-impact first, each with a
   one-line recommended next action. If `$ARGUMENTS` is set, scope to it.
5. **Call out the gap honestly.** Mark which items are **autonomous** (todos
   the loop will run) vs **inert** (OPEN-ITEMS entries and gripes that aren't
   todos yet, so nothing works them until promoted). End with the single
   highest-leverage next action.

Keep it tight — this is a triage view, not a full read of every item.
