---
id: precis-quest-help
title: precis — track long-running goals
applies-to: get/search/put/delete/tag/link (kind='quest')
status: active
---

# precis-quest-help — track long-running goals

A quest is a multi-session goal with a human-meaningful slug. Use it
for work bigger than a single next-action (those are `todo`).

## Open a new quest
## Start tracking a long-running goal
## How do I create a quest?

```python
put(kind='quest', text='ingest acheson 2026 corpus end-to-end')
# → created quest 'ingest-acheson-2026-corpus-end-to-end' (status: open)
```

Slug is auto-derived from the text (lowercased, hyphenated, ≤60 chars).
Collisions append `-2`, `-3`. `put` is create-only — passing `id=` is
rejected. To change the framing, soft-delete and re-open.

## See what I'm working on
## List my open quests
## What am I working toward this quarter?

```python
get(kind='quest')                # /recent (default)
get(kind='quest', id='/open')    # STATUS:open + doing + blocked
get(kind='quest', id='/doing')   # in progress only
get(kind='quest', id='/blocked') # waiting on something
get(kind='quest', id='/done')    # completed
get(kind='quest', id='<slug>')   # one quest with its tags
```

## Mark a quest done
## Move a quest through statuses
## How do I close out a quest?

```python
tag(kind='quest', id='<slug>', add=['STATUS:doing'])
tag(kind='quest', id='<slug>', add=['STATUS:blocked'])
tag(kind='quest', id='<slug>', add=['STATUS:done'])
tag(kind='quest', id='<slug>', add=['STATUS:won\'t-do'])
```

`STATUS:` is closed-prefix — setting one replaces the previous value
atomically. Same applies to `PRIO:` (`low`, `normal`, `high`, `urgent`).

## Link a todo to a quest
## Attach evidence or follow-ups to a quest
## How do I connect a task to a goal?

```python
# A todo that progresses this quest
put(kind='todo', text='extract chapter 3 figures')   # → id=204
link(kind='todo', id=204,
     target='quest:<slug>', rel='derived-from')

# A paper the quest is about
link(kind='quest', id='<slug>',
     target='paper:acheson2026automated', rel='cites')

# A memory that captured the framing
link(kind='quest', id='<slug>',
     target='memory:88', rel='supports')
```

Targets carry an explicit kind prefix. See `precis-relations` for the
vocabulary.

## Search quests by content

```python
search(kind='quest', q='ingest')
search(kind='quest', q='ingest', tags=['STATUS:open'])
```

`tags=` filters compose with AND. Cross-kind search
(`kind='quest,todo'`) merges quests with other queues.

## Delete a quest

```python
delete(kind='quest', id='<slug>')
```

Soft-delete — row retained, vanishes from list views.

## When to use quest vs todo vs memory

| Capture | Use |
|---|---|
| Discrete next-action | `todo` |
| Multi-session goal with status flow | `quest` |
| Decision, observation, lesson | `memory` |

A done todo disappears off the queue; a done quest stays as the durable
slug for retrospective queries.

## See also

```python
get(kind='skill', id='precis-todo-help')      # day-to-day tasks; same STATUS axis
get(kind='skill', id='precis-relations')      # derived-from / supports / cites
get(kind='skill', id='precis-tags')           # STATUS: / PRIO: vocabulary
get(kind='skill', id='precis-memory-help')    # capturing decisions and framings
```
