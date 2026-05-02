---
id: precis-gripe-help
title: precis — file friction, never look back
status: shipped
tier: 1
floor: any
applies-to: put (kind='gripe')
last-updated: 2026-05-02
---

# precis-gripe-help — file and forget

Capture friction without breaking flow. A gripe is a half-sentence
observation that something is worse than it should be. Write it
down and move on.

`gripe` is **write-only from the agent surface**. There is no
`get`, no `search`, no `tag`, no `link`. You cannot list, browse,
or read what's in the box. That's the point — it's a zero-friction
complaint slot, not a workflow queue. Triage happens out-of-band
(human review of the database; CLI tools); the agent's job is to
notice friction and drop the note.

## File one

```python
put(kind='gripe', text='paper slug NotFound does not surface near-match options')
# → created gripe id=42
```

That's the entire surface. No `tags=`, no `link`, no triage. 5
seconds of typing.

## What this is for

- **Notice in passing.** A skill said one thing, the runtime did
  another. File and keep working.
- **Surface tooling friction.** Error messages that don't help,
  examples that don't run, slugs that aren't accepted. File.
- **Unstructured complaints.** Don't know yet what's wrong, just
  that something feels off. File. The articulation can come later.

## When to use gripe vs other kinds

| Want to capture... | Use |
|---|---|
| "This annoyed me, don't know why yet" | `gripe` |
| "I will do this" | `todo` |
| "I noticed this structural thing" | `memory` with `kind:note` |
| "Here is what I decided" | `memory` with `kind:decision` |

The distinction: **gripe is pre-articulation**. If you already
know what to do, skip it and write a todo. If you understand why
it matters, skip it and write a memory.

## Failure modes

- **Trying to read your own gripes.** `get(kind='gripe', ...)`,
  `search(kind='gripe', ...)`, and friends raise
  `[error:Unsupported]` by design. The capture is one-way.
- **Over-tagging at write time.** `put(text='...')` only — `tags=`
  isn't accepted. Don't waste the capture budget on classification.
- **Gripes that are actually todos.** If you know the fix, write a
  todo, not a gripe.
- **Gripes as passive-aggressive communication.** Document on the
  thing you're criticising; don't file-and-forget something you
  wanted someone else to see.

## See also

- `precis-todo-help` — actionable items go here, not in gripe
- `precis-memory-help` — articulated insights go here, not in gripe
