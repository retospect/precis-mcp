---
id: precis-memory-help
title: precis — capture notes, decisions, ideas, questions
status: active
tier: 1
floor: any
applies-to: get/search/put/delete/tag/link (kind='memory')
last-updated: 2026-05-02
---

# precis-memory-help — capture notes, decisions, ideas, questions

Sub-kind via `kind:` tag.  Pick by what you're capturing:

| Sub-kind | For |
|---|---|
| `kind:note` | General observation (default) |
| `kind:decision` | A choice you made, with reasoning |
| `kind:idea` | Speculative thought worth revisiting |
| `kind:question` | Open question to answer later |
| `kind:lessons-learned` | Retrospective insight |
| `kind:summary` | Distilled summary |

Coin new sub-kinds freely.

## Capture a note

```python
put(kind='memory',
    text='Wang2020 chunk 38 has the cleanest Z-scheme diagram.',
    tags=['kind:note', 'topic:noxrr'],
    link='paper:wang2020state~38')
```

## Record a decision

```python
put(kind='memory',
    text='Decided to drop mode-driven tag/link in favour of typed kwargs.',
    tags=['kind:decision', 'confidence-strong', 'project:precis-v2'])
# → returns integer id (e.g. 73)
```

## Flag an open question

```python
put(kind='memory', text='Does CACHE: pinning play well with re-ingest?',
    tags=['kind:question', 'confidence-tentative'])
```

## Browse memories

```python
search(kind='memory', q='kwargs vs modes', tags=['kind:decision'])
```

## Promote a research cache to durable

```python
get(kind='research', q='mechanism of NOxRR')    # generates cache
put(kind='memory',
    text='Distilled mechanism: three-electron pathway, see §2 of cache.',
    tags=['kind:summary', 'topic:noxrr', 'confidence-moderate'],
    link='research:mechanism-of-noxrr')
```

## Bump confidence later

Confidence is an open-tag axis today (lowercase, hyphenated):
``confidence-tentative``, ``confidence-moderate``, ``confidence-strong``,
``confidence-certain``. Open tags don't replace each other — to
bump from ``moderate`` to ``certain``, untag the old value:

```python
tag(kind='memory', id=73,
    add=['confidence-certain'],
    remove=['confidence-moderate'])
```

If ``confidence`` graduates to a registered closed prefix in a
later phase, the same call shape will swap to
``add=['CONFIDENCE:certain']`` and the replacement becomes atomic
(no separate ``remove=`` needed).

## Notes

- Server assigns an integer id on create; reference it thereafter.
- Use a memory for thoughts that stand alone; for commentary on an
  existing ref, create the memory and `link=` it back to the ref
  (with `rel='related-to'` or a more specific relation — see
  `precis-relations`).

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `kind:`, `topic:`, the registered closed axes
- `precis-relations` — `related-to`, `contradicts`
- `precis-cache` — when to promote a `research` / `think` cache to a memory
