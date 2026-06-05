---
id: precis-memory-help
title: precis — capture notes, decisions, ideas, questions
applies-to: get/search (kind='memory'), put (kind='memory')
status: active
---

# precis-memory-help — capture notes, decisions, ideas, questions

Memory is a numeric-ref scratchpad for thoughts that stand alone.
Categorise with open tags (`topic:`, `project:`, `confidence-*`) and
bare flags (`pinned`, `wip`); there is no enforced sub-kind axis.

## Capture a note

```python
put(kind='memory',
    text='Wang2020 chunk 38 has the cleanest Z-scheme diagram.',
    tags=['topic:noxrr'],
    link='paper:wang2020state~38')
```

## Record a decision

```python
put(kind='memory',
    text='Dropped mode-driven tag/link in favour of typed kwargs.',
    tags=['confidence-strong', 'project:precis-v2', 'topic:api-design'])
# → returns integer id (e.g. 73)
```

## Flag an open question

```python
put(kind='memory', text='Does CACHE: pinning play well with re-ingest?',
    tags=['confidence-tentative', 'topic:caching'])
```

## Browse memories

```python
search(kind='memory', q='kwargs vs modes', tags=['topic:api-design'])
```

## Promote a research cache to durable

```python
get(kind='research', q='mechanism of NOxRR')    # generates cache
put(kind='memory',
    text='Distilled mechanism: three-electron pathway, see §2 of cache.',
    tags=['topic:noxrr', 'confidence-moderate'],
    link='research:mechanism-of-noxrr')
```

## Bump confidence later

Confidence is an open-tag axis (lowercase, hyphenated):
`confidence-tentative`, `confidence-moderate`, `confidence-strong`,
`confidence-certain`. Open tags don't replace each other — bump by
untagging the old value:

```python
tag(kind='memory', id=73,
    add=['confidence-certain'],
    remove=['confidence-moderate'])
```

## Notes

- Server assigns an integer id on create. Both `id=42` and
  `id='memory:42'` are accepted (the link-target form).
- Use a memory for thoughts that stand alone; for commentary on an
  existing ref, create the memory and `link=` it back (with
  `rel='related-to'` or a more specific relation — see
  `precis-relations`).

## See also

```python
get(kind='skill', id='precis-overview')       # verbs and kinds
get(kind='skill', id='precis-tags')           # topic:, axes, conventions
get(kind='skill', id='precis-relations')      # related-to, contradicts
get(kind='skill', id='precis-cache')          # promoting a research cache
```
