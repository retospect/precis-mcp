---
id: precis-quest-help
title: precis — long-running goals as slug-addressed work units
status: shipped
tier: 1
floor: any
applies-to: get / search / put / delete / tag / link (kind='quest')
last-updated: 2026-05-02
---

# precis-quest-help — long-running goals

`quest` is a **slug-addressed work-unit kind**. One quest = one
goal that takes longer than a single session — something you'd
otherwise track in a stickies file or a separate issue queue.
Status flows on the same `STATUS:` axis as `todo`, but the unit
of work is bigger and the slug is human-meaningful.

Two things distinguish quest from todo:

- **Slug, not integer id.** Auto-derived from the text on create
  (`put(text='ingest acheson 2026')` → slug `ingest-acheson-2026`).
  Slugs are stable references you can paste into commits and prose.
- **Larger unit.** A todo is a discrete next-action; a quest is a
  multi-step goal. The body holds the framing; sub-tasks belong
  in linked `todo` refs.

## Open one

```python
put(kind='quest', text='ingest acheson 2026 corpus end-to-end')
# → created quest 'ingest-acheson-2026-corpus-end-to-end' (status: open)
```

The slug is auto-derived (lowercased, hyphenated, ≤60 chars). On
collision the handler appends `-2`, `-3`, … until unique.

`put` is **create-only** for quest. To mutate an existing quest use
the dedicated verbs: `tag`, `link`, `delete`. Re-issuing
`put(text='same')` does not edit the existing quest — it raises
`BadInput`.

## Browse

```python
get(kind='quest')                    # /recent (default)
get(kind='quest', id='/recent')      # 100 newest, any status
get(kind='quest', id='/open')        # STATUS:open
get(kind='quest', id='/doing')       # STATUS:doing
get(kind='quest', id='/blocked')     # STATUS:blocked
get(kind='quest', id='/done')        # STATUS:done
get(kind='quest', id='<slug>')       # one quest
```

## Status transitions

```python
tag(kind='quest', id='ingest-acheson-2026', add=['STATUS:doing'])
tag(kind='quest', id='ingest-acheson-2026', add=['STATUS:blocked'])
tag(kind='quest', id='ingest-acheson-2026', add=['STATUS:done'])
```

`STATUS:` is closed-prefix and replaces atomically — adding a new
status removes the previous one in the same call.

## Search

```python
search(kind='quest', q='ingest', tags=['STATUS:open'])
search(kind='quest', q='photocatalysis')
```

`tags=` filters compose with AND. Cross-kind search (`kind='*'` or
`kind='quest,todo,memory'`) includes quests in the merge.

## Link to evidence and follow-ups

Quests are the connective tissue between long-running goals and
the smaller refs that progress them:

```python
# A todo that's part of a quest
put(kind='todo', text='extract chapter 3 figures',
    tags=['PRIO:normal'])
# → todo id=204
link(kind='todo', id=204,
     target='quest:ingest-acheson-2026-corpus-end-to-end',
     rel='derived-from')

# A memory that captured the goal's framing
link(kind='quest', id='ingest-acheson-2026-corpus-end-to-end',
     target='memory:88', rel='supports')

# A patent watch that the quest spawned
link(kind='quest', id='ingest-acheson-2026-corpus-end-to-end',
     target='paper:acheson2026automated', rel='cites')
```

`view='links'` is not yet wired on slug kinds — to enumerate a
quest's links today, look at the linked numeric-ref refs (memory,
todo) via their own `view='links'`.

## Delete

```python
delete(kind='quest', id='ingest-acheson-2026-corpus-end-to-end')
# Soft-delete: row retained for audit, vanishes from list views.
```

No MCP undo — reverse via SQL by setting `deleted_at = NULL`.

## Vocabulary

- `STATUS:` values: `open` (default on create), `doing`, `blocked`,
  `done`, `won't-do`. Unknown values rejected at write time.
- `PRIO:` values: `low`, `normal`, `high`, `urgent`. Optional.

## When to use quest vs todo vs memory

| Want to capture... | Use |
|---|---|
| Discrete next-action | `todo` |
| Multi-step goal with status flow | `quest` |
| Decision / observation / lesson | `memory` |
| Friction with no articulation yet | `gripe` (write-only) |

The same `STATUS:` lifecycle applies to todo and quest, but a todo
disappears off the queue when done; a quest stays as the durable
slug for retrospective queries (`search(kind='quest', q='ingest',
tags=['STATUS:done'])`).

## Failure modes

- **`put(id='...')`** — rejected. To mutate, use `tag` / `link` /
  `delete`. Quest text is the framing on creation, not editable
  thereafter; if the framing needs to change, soft-delete and
  re-open.
- **Slug collision** — handled silently with `-2`, `-3`, … suffix.
  If you wanted to overwrite, soft-delete the existing slug first.
- **`STATUS:invented`** — closed-prefix axes reject unknown values
  with the canonical list in the error.

## See also

- `precis-todo-help` — discrete actions; same `STATUS:` axis
- `precis-relations` — `derived-from` / `supports` / `cites`
- `precis-tags` — `STATUS:` / `PRIO:` vocabulary
