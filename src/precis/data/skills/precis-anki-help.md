---
id: precis-anki-help
title: precis — Anki cloze cards
summary: author spaced-repetition cloze cards ({{c1::…}}) that live in the corpus and sync to AnkiWeb
applies-to: get/search/put/delete/tag/link (kind='anki')
status: active
supersedes: precis-flashcard-help (flashcard kind retired 2026-07)
---

# precis-anki-help — Anki cloze cards

An `anki` card is a numeric-ref **cloze** card: one sentence with one or more
`{{cN::hidden}}` deletions. Cards live in the corpus (embedded + searchable)
and sync to your AnkiWeb collection. **Anki owns scheduling** — there is no
recall-rating or interval state in precis; the forgetting curve lives in Anki.

## Make a cloze card from a fact worth remembering
## Turn something I just read into a card
## Capture an atomic fact for spaced repetition

```python
put(kind='anki', text='Paris is the {{c1::capital}} of France.')
# → returns integer id (e.g. 204)

put(kind='anki',
    text='The mitochondrion is the {{c1::powerhouse}} of the cell.',
    tags=['topic:cell-bio'],
    link='paper:alberts2015molecular~12', rel='derived-from')
```

Every card needs **at least one** `{{cN::…}}` deletion, or the put is rejected.

### Cloze syntax

- **One deletion, one fact.** `{{c1::…}}` hides the span behind it. Keep the
  deletion minimal and unambiguous — test one idea, not a whole sentence.
- **Several cards from one note.** Different indices → separate cards:
  `The {{c1::heart}} pumps {{c2::blood}}.` makes two cards. The *same* index
  reveals together: `{{c1::A}} and {{c1::B}}` hides both on one card.
- **Hints.** `{{c1::answer::hint}}` shows *hint* on the front. The hint is
  dropped from the corpus search text (only the answer is indexed).

### Optional terse "Back Extra"

Add a short answer-side note — a source, a mnemonic, a gotcha — after a lone
`---` line at the end of the body. Keep it terse, or omit it:

```python
put(kind='anki', text='''\
The {{c1::Krebs}} cycle occurs in the mitochondrial matrix.
---
aka the citric-acid / TCA cycle''')
```

Everything before `---` is the cloze card; everything after is Back Extra.

## Browse existing cards
## List recent Anki cards

```python
get(kind='anki')                  # /recent (default)
get(kind='anki', id='/recent')    # 20 newest
get(kind='anki', id=204)          # one card — body + note meta
```

## Search across cards
## Check whether I already made a card about X

```python
search(kind='anki', q='capital of france')
search(kind='anki', q='citric acid cycle', tags=['topic:cell-bio'])
```

The search card is the cloze sentence with markup stripped, so `{{c1::capital}}`
matches a query for *capital*. Cards also surface in cross-kind search — handy
for checking you haven't already carded an idea before writing a note.

## Tag or link a card

```python
tag(kind='anki', id=204, add=['topic:cell-bio'])
link(kind='anki', id=204, target='paper:alberts2015molecular~12', rel='derived-from')
```

## Edit or remove a card

Bodies are immutable (like all numeric-ref kinds): to reword a card,
`delete(kind='anki', id=204)` then `put` the new wording. Deleting a card
removes it from the corpus.

## What this is NOT

- **No image occlusion / structured notetypes** — cloze only for now. The note
  shape (`meta.notetype`/`meta.fields`) is generic, so other notetypes can be
  added later without a migration.
- **No SM-2 / recall rating in precis** — Anki schedules; precis mirrors the
  decay stats back (once the sync slice lands) but never grades.

## See also

```python
get(kind='skill', id='precis-overview')     # verbs and kinds
get(kind='skill', id='precis-memory-help')  # prose notes that aren't recall targets
get(kind='skill', id='precis-tags')         # tag axis conventions
get(kind='skill', id='precis-relations')    # link relations (derived-from, …)
```
