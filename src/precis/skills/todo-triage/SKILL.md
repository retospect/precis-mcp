---
name: todo-triage
description: >
  Bulk triage for an accumulated todo list.  Use when you have 5+ open
  items and want to prune / reprioritise / close stale ones rather than
  work on them one-by-one.
user-invocable: true
allowed-tools: [get, put, search]
applies-to: [todo]
tags: [productivity, triage]
---

## When to Use

- `get(id='todo:/today')` returns ≥ 5 items
- User asks "what's pending" or "clean up my todos"
- A session starts with a "N todos need attention" notification

## The triage loop

Four moves, in order of preference: **close**, **defer**, **reprioritise**, **split**.

### 1. Close obvious no-longer-relevant ones

```
get(id='todo:/today')             # list pending
put(id='todo:<slug>', text='cancelled', mode='state')
```

Ask: *is this still a thing?*  Sprints, side projects, meeting follow-ups often accumulate dead items.  If closing, add a one-line note so future-you remembers why:

```
put(id='todo:<slug>', text='why I closed this', mode='note')
```

### 2. Defer items that aren't this week

There's no built-in `snoozed` state — use priority + a comment:

```
put(id='todo:<slug>', priority=1, mode='replace')
put(id='todo:<slug>', text='deferred — review in 2 weeks', mode='note')
```

Items at `priority=1` sink to the bottom of `/today`; you'll notice them when you next triage.

### 3. Reprioritise the rest

Scale: `1` (low), `5` (medium, default), `9` (urgent).  Typical rebalance:

- 1–2 items at `9` — actually urgent this session
- 3–5 items at `5` — this week
- Everything else at `3` or below

```
put(id='todo:<slug>', priority=9, mode='replace')
```

### 4. Split what's too big

If a todo is really a project, split it:

```
# Close the umbrella
put(id='todo:<old-slug>', text='cancelled', mode='state')
put(id='todo:<old-slug>', text='split into <new-slug-1>, <new-slug-2>', mode='note')

# Spawn children
put(type='todo', text='concrete step 1', priority=5, mode='append')
put(type='todo', text='concrete step 2', priority=3, mode='append')
```

## After triage

Report the delta to the user:

- `N before → M after`
- Top 3 remaining high-priority items
- Any splits that created new work

```
get(id='todo:/today')  # confirm state
```

## Rules

- **Don't auto-close items on the user's behalf** without confirming.  Propose closures and ask.
- **Cancelled ≠ done.**  Use `done` only for things you actually completed; `cancelled` for things that stopped mattering.
- **Notes over state changes for context.**  "Why did I have this todo" is more useful than grave-digging a cancelled item a month later.
