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

## Tags

Todos carry a free-form tag list (e.g. `urgent`, `kitchen`, `@work`, `q3-goal`) alongside state + priority. Tags are **additive labels** — they group todos across states without moving them. Removing a tag **does not delete the todo**; it only strips the label.

### List the tags already in use

```
get(id='todo:/tags')        # histogram: tag + count, sorted hottest first
```

Use this before inventing a new tag — existing vocabulary is almost always what you want.

### Create a todo with tags

```
put(type='todo', text='Fix parser crash', tags=['urgent', 'build'], mode='append')
```

`tags=` accepts a list or a comma-string (`tags='urgent, build'`).

### Add tags to an existing todo

```
put(id='todo:fix-parser-crash', text='urgent,build', mode='tag')
```

`mode='tag'` unions — tagging `['a', 'b']` on a todo already tagged `['b', 'c']` yields `['a', 'b', 'c']`.

### Remove tags (but keep the todo)

```
put(id='todo:fix-parser-crash', text='urgent', mode='untag')
```

`mode='untag'` strips only the listed tags. The todo, its state, its history, and any other tags remain untouched. To actually close the todo, use `mode='state'` with `done` or `cancelled` — that's the state machine, not tagging.

### Filter by tag

```
get(id='todo:', grep='tag:urgent')          # all todos with #urgent
```

The `tag:` grep prefix is first-class on the bare `todo:` list. Combine with plain keywords: `grep='tag:build parser'`. State-filtered views (`/open`, `/pending`, …) don't accept `grep=` — filter by tag first, then scan the result for state.

### Tag vs priority vs state — when to use which

- **state** — answers *is this done yet?*  (pending / in_progress / done / blocked / cancelled)
- **priority** — answers *how loud should this be?*  (low / medium / high)
- **tag** — answers *what bucket does this belong to?*  (project, area, context, theme)

A todo can have many tags but exactly one state and one priority. If you find yourself cramming a project name into the title (`[KitchenRemodel] Call plumber`), tag it instead.

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
