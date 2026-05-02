---
id: precis-fc-help
title: precis — spaced-repetition flashcards
status: phase-5 (partial)
tier: 1
floor: any
applies-to: get / search / put / delete / tag / link (kind='fc')
last-updated: 2026-05-02
---

# precis-fc-help — spaced-repetition knowledge cards

`fc` is **SM-2** spaced-repetition in an agent-native shape. One
card = one knowledge statement. Review schedule lives in
`ref.meta`.

> **Status:** phase-5 ships the thin surface — create, read, search,
> list-due. The **full SM-2 grader** (grade → next interval →
> update `next_review`) is queued for a follow-up. Tagging a card
> with `REVIEW:*` today records the tag but does **not** yet
> advance the schedule. Cards still surface as due based on
> `ref.meta.next_review`; until the grader lands, untouched cards
> simply stay due.

## Write a card

```python
put(kind='fc', text='SM-2 schedules reviews at intervals of 1, 6, and then interval * easiness days.')
# → created fc id=204
```

The card body is the **knowledge statement** you want to remember —
not a question. The agent asking you later reframes it into a Q&A
dynamically, using the body as the answer-key. One atomic fact per
card gives the best recall signal.

Cards accept optional tags and links at creation time:

```python
put(kind='fc',
    text='RRF combines rankings by sum of 1/(k + rank_i) where k is typically 60.',
    tags=['topic-search', 'topic-rrf'],
    link='paper:acheson2026automated~8')
```

## What's due today

```python
get(kind='fc', id='/due')       # cards whose next_review <= now
```

Response shows due cards plus anything due within the next 3 days,
with ref ids and short titles. Empty? `no flashcards due` with a
hint to browse `/recent` or create a new card.

Meta state (`easiness`, `interval`, `reps`, `next_review`,
`last_reviewed`, `review_log`) lives on each ref and is visible
when you `get(kind='fc', id=N)`. Untouched cards (no `next_review`
set) count as due — they surface immediately on the first `/due`
call after creation.

## Browse

```python
get(kind='fc')                  # /recent (default)
get(kind='fc', id='/recent')    # 20 newest, any due state
get(kind='fc', id=204)          # one card
```

## Review a card (today — manual)

Until the SM-2 grader ships, reviewing is informal: `get` the card,
quiz yourself, and move on. The full grader (`grade=0..5` →
recomputed `next_review`) is queued for a follow-up — when it
lands, a dedicated verb will advance the SM-2 state atomically.
The agent surface for logging a grade is deliberately absent until
then; the `/due` listing stays useful in the meantime.

## Search

```python
search(kind='fc', q='RRF')
search(kind='fc', q='rank fusion', tags=['topic-search'])
```

Cross-kind search also includes cards — useful when you're writing
a note and want to check whether you already made a card about the
same idea.

## Typical flow — capture from learning

```python
# Just read a paper block.
get(kind='paper', id='acheson2026automated~8')

# Found a fact worth remembering. Make a card, link it back.
put(kind='fc',
    text='PIPS needs only 36 molecular configurations to generalise.',
    tags=['topic-pips', 'source-acheson2026automated'],
    link='paper:acheson2026automated~8', rel='derived-from')
```

## Failure modes

- **Card body is a question, not the answer.** SM-2 expects the
  statement you want to memorise. Put the answer in `text=`; the
  agent generates the prompt shape.
- **Over-carding.** One card per atomic fact. Long bodies reduce
  recall probability and the reliability of the (eventual) grade
  signal.
- **Expecting the schedule to advance today.** Reviewing doesn't
  (yet) reset `next_review`; cards stay on `/due` indefinitely
  until the grader ships.

## See also

- `precis-memory-help` — for context/prose that isn't a recall target
- `precis-overview` — verbs and kinds
- Background: Piotr Woźniak, SuperMemo SM-2 algorithm (1987).
