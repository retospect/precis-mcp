---
id: precis-memory-help
title: precis — capture notes, decisions, ideas, questions
status: draft
tier: 1
floor: any
applies-to: get/search (kind='memory'), put (kind='memory')
last-updated: 2026-04-26
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
    link='wang2020state~38')
```

## Record a decision

```python
put(kind='memory',
    text='Decided to drop mode-driven tag/link in favour of typed kwargs.',
    tags=['kind:decision', 'CONFIDENCE:strong', 'project:precis-v2'])
# → returns integer id (e.g. 73)
```

## Flag an open question

```python
put(kind='memory', text='Does CACHE: pinning play well with re-ingest?',
    tags=['kind:question', 'CONFIDENCE:tentative'])
```

## Browse memories

```python
search(kind='memory', q='kwargs vs modes', tags=['kind:decision'])
```

## Promote an `ask` cache to durable

```python
get(kind='ask', q='mechanism of NOxRR')         # generates cache
put(kind='memory',
    text='Distilled mechanism: three-electron pathway, see §2 of cache.',
    tags=['kind:summary', 'topic:noxrr', 'CONFIDENCE:moderate'],
    link='mechanism-of-noxrr')
```

## Bump confidence later

```python
put(kind='memory', id='73', tags=['CONFIDENCE:certain'])
# replaces previous CONFIDENCE:*
```

## Notes

- Server assigns an integer id on create; reference it thereafter.
- Use a memory for thoughts that stand alone; use `mode='note'` on an
  existing ref for commentary on that ref.

## See also

- `precis-overview` — verbs and kinds
- `precis-tags` — `kind:`, `CONFIDENCE:`, `topic:`
- `precis-relations` — `related-to`, `contradicts`
- `precis-cache` — when to promote an `ask` cache to a memory
