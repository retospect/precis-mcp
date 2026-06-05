---
id: precis-gripe-help
title: precis — file friction, never look back
applies-to: put (kind='gripe')
status: active
---

# precis-gripe-help — file friction in one line, triage later

A gripe is a half-sentence note that something is worse than it
should be. Drop it and keep working; articulation can wait.

## File a gripe about something annoying
## Log friction I noticed in passing
## How do I record a niggle without breaking flow?

```python
put(kind='gripe', text='paper slug NotFound does not surface near-match options')
# → created gripe id=42 (write-only — gripes cannot be read, edited,
#   or deleted via the MCP surface; triage happens out-of-band)
```

`put(text=...)` is the entire surface. No `tags=`, no `link=`, no
follow-up read.

## Promote a gripe to a todo when I know the fix

Gripes can't be edited into todos in place. Create a new todo that
links back to the gripe via the numeric-ref form `gripe:<N>`:

```python
put(kind='todo',
    text='Add near-match suggestions to paper slug NotFound errors.',
    tags=['PRIO:normal'],
    link='gripe:42', rel='resolves')
```

The gripe stays on file for triage; the todo carries the action.

## Choose gripe vs todo vs memory

| Capture                                 | Use      |
|-----------------------------------------|----------|
| "This annoyed me, don't know why yet"   | `gripe`  |
| "I will do this"                        | `todo`   |
| "Here's a thought I want to keep"       | `memory` |

Gripe is pre-articulation. If you already know the fix, skip
straight to `todo`. If you understand why something matters and
want it findable, use `memory`.

## What not to expect from gripe

- `get(kind='gripe', ...)`, `search(kind='gripe', ...)`,
  `tag(kind='gripe', ...)`, `link(kind='gripe', ...)`, and
  `delete(kind='gripe', ...)` all return `[error:Unsupported]`.
  The capture is one-way from the agent surface.
- `put` rejects `tags=` and `link=` on gripe — keep the call to
  `text=` only.
- Don't file passive-aggressive notes meant for someone else to
  read; gripes are not a messaging channel.

## See also

```python
get(kind='skill', id='precis-todo-help')      # promote a gripe to an actionable todo
get(kind='skill', id='precis-memory-help')    # articulated thoughts go here, not in gripe
get(kind='skill', id='precis-put-help')       # the put verb across every kind
get(kind='skill', id='precis-link-help')      # rel= vocabulary for the promote flow
```
