---
id: precis-fc-help
title: precis — spaced-repetition flashcards
summary: spaced-repetition flashcards — atomic facts, scheduled review, recall rating
applies-to: get/search/put/delete/tag/link (kind='fc')
status: active
---

# precis-fc-help — spaced-repetition knowledge cards

Flashcards are numeric-ref knowledge statements scheduled for review.
One card = one atomic fact you want to remember.

## Create a card from something I just read
## Make a flashcard for a fact worth memorising
## Capture a knowledge statement as a card

```python
put(kind='fc', text='PIPS needs only 36 molecular configurations to generalise.')
# → returns integer id (e.g. 204)

put(kind='fc',
    text='PIPS needs only 36 molecular configurations to generalise.',
    tags=['topic:pips'],
    link='paper:acheson2026automated~8', rel='derived-from')
```

The `text=` is the **answer** — the statement you want to recall.
The reviewing agent reframes it into a question at quiz time. One
atomic fact per card gives the cleanest recall signal; long bodies
hurt both recall and the rating signal.

## What's due today
## Show the review queue
## List cards I need to review now

```python
get(kind='fc', id='/due')
```

```text
# 3 flashcard(s) due
   204  PIPS needs only 36 molecular configurations to generalise.
   187  Z-scheme photocatalysts pair two semiconductors with offset bands.
   142  Hybrid search rank-fuses lexical and semantic results.

  2 due within 3 days
   199  (2026-06-07)  Cu-doped TiO2 shifts absorption into the visible.
   201  (2026-06-08)  NOxRR three-electron pathway peaks near pH 6.
```

Untouched cards (never reviewed) count as due and surface
immediately. Empty queue returns `no flashcards due` with hints to
`/recent` or create.

## Browse existing cards
## List recent flashcards
## See what cards I already have

```python
get(kind='fc')                  # /recent (default)
get(kind='fc', id='/recent')    # 20 newest
get(kind='fc', id=204)          # one card — body + schedule meta
```

Both `id=204` and `id='fc:204'` are accepted.

## Review a card
## Quiz myself on a due card
## Rate how well I recalled a card

Read the card, recall the answer, check the body. The rating verb
(`grade=0..5` → next-interval update) is not yet wired; reviewed
cards stay on `/due` until it ships. The schedule meta
(`easiness`, `interval`, `reps`, `next_review`, `last_reviewed`)
is visible on `get(kind='fc', id=N)`.

## Search across cards
## Find a card by topic
## Check whether I already made a card about X

```python
search(kind='fc', q='rank fusion')
search(kind='fc', q='photocatalysis', tags=['topic:noxrr'])
```

Cards also surface in cross-kind search — useful when writing a
note to check for an existing card on the same idea.

## Tag or link a card

```python
tag(kind='fc', id=204, add=['topic:pips'])
link(kind='fc', id=204, target='paper:acheson2026automated~8', rel='derived-from')
```

## See also

```python
get(kind='skill', id='precis-overview')       # verbs and kinds
get(kind='skill', id='precis-memory-help')    # prose notes that aren't recall targets
get(kind='skill', id='precis-tags')           # tag axis conventions
get(kind='skill', id='precis-relations')      # link relations (derived-from, ...)
```
