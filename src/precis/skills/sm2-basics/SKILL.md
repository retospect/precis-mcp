---
name: sm2-basics
description: >
  SM-2 spaced-repetition review workflow for flashcards.  Use when the
  agent or user is reviewing due flashcards (flashcard:/due) or recording
  recall quality with put(id='flashcard:<slug>', text='<0-5>', mode='review').
user-invocable: true
argument-hint: [slug, quality]
allowed-tools: [get, put]
applies-to: [flashcard]
kind-onboarding: flashcard
tags: [learning, spaced-repetition]
---

## When to Use

- User asks "review my flashcards" or "what's due today"
- You see a `flashcard:/due` notification at session start
- You're about to record a review with `put(id='flashcard:<slug>', text='<0-5>', mode='review')`

## SM-2 quality scale

Quality is an integer `0..5`. Record *honestly* — the algorithm reschedules based on it:

| Quality | Meaning                                 | Effect on interval       |
|---------|-----------------------------------------|--------------------------|
| `5`     | perfect recall, easy                    | interval grows fastest   |
| `4`     | correct but hesitant                    | normal growth            |
| `3`     | correct, hard                           | minimal growth           |
| `2`     | wrong, answer was close                 | reset to short interval  |
| `1`     | wrong, recognised answer when shown     | reset, more repetitions  |
| `0`     | blank, no recollection                  | reset from scratch       |

## Workflow

1. **Get the due list:**
   ```
   get(id='flashcard:/due')
   ```
   Returns items with `next_review <= today`, plus nearby-due (within 3 days).

2. **For each due item, quiz yourself or the user:**
   - Read the item: `get(id='flashcard:<slug>')`
   - Attempt recall *before* looking at the answer
   - Grade honestly on the 0–5 scale

3. **Record the review:**
   ```
   put(id='flashcard:<slug>', text='4', mode='review')
   ```
   The handler updates `easiness`, `interval`, `reps`, `next_review` automatically.

4. **Optionally add a note about difficulty:**
   ```
   put(id='flashcard:<slug>', text='4', mode='review', note='struggled on temperature')
   ```

## Tips

- **Don't batch-review without recall** — passing quality `5` to everything degenerates the algorithm. The point is honest self-grading.
- **`/stats` shows struggle spots.** Items with `easiness < 1.8` are marked — consider rewording the front or splitting into smaller concepts.
- **Overdue-by-a-lot** items lose all scheduling signal. Don't panic-review 100 cards after ignoring the deck for a month — just do what you can and let the algorithm recompute.
- **Create new items inline with a review session** when a question surfaces:
  ```
  put(id='flashcard:', text='The capital of Ireland is Dublin.', mode='append')
  ```

## Output format

After each `put(mode='review')`, the handler returns the updated schedule:

```
✓ flashcard:paris-capital  q=4
  easiness: 2.36 → 2.41
  interval: 3 → 7 days
  next review: 2026-04-28
```

Surface that summary to the user so they see progress.
