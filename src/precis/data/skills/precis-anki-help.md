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
For *how to write good cards* (dedup-first, cN ordering, hints, deck naming), see
**`precis-cloze`** — this skill is the reference; that one is the craft.

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
## What anki cards do I have?
## Find anki cards about X
## Search my Anki collection
## Check whether I already made a card about X

```python
search(kind='anki', q='capital of france')
search(kind='anki', q='citric acid cycle', tags=['topic:cell-bio'])
```

Your **whole** Anki collection is searchable here — cards you authored in precis
*and* every card you made in Anki (they're projected in as read-only refs). The
search card is the cloze sentence with markup stripped, so `{{c1::capital}}`
matches a query for *capital*. **Search before you create** — it's how you avoid
duplicates (see `precis-cloze`).

## Which cards do I keep forgetting?
## Find bad-recall cards / leeches
## What should I fix or restudy?

```python
get(kind='anki', id='/leeches')   # high-lapse / collapsed-ease cards, worst first
```

Reads the recall stats (`meta.anki_stats`, refreshed each sync) across the whole
collection. For each bad-recall card you then decide: **fix the cloze** (tag it
`precis-fix` in Anki — the LLM rewrites it) or **study it more**.

## Tag or link a card
## File a card under a deck

Tag `deck-<topic>` to file an authored card under the `Precis::<topic>` sub-deck
(no tag → the base `Precis` deck):

```python
put(kind='anki', text='The {{c1::heart}} pumps blood.', tags=['deck-anatomy'])
tag(kind='anki', id=204, add=['topic:cell-bio'])
link(kind='anki', id=204, target='paper:alberts2015molecular~12', rel='derived-from')
```

## Edit or remove a card

```python
tag(kind='anki', id=204, add=['topic:cell-bio'])
link(kind='anki', id=204, target='paper:alberts2015molecular~12', rel='derived-from')
```

## Edit or remove a card

Bodies are immutable (like all numeric-ref kinds): to reword a card,
`delete(kind='anki', id=204)` then `put` the new wording. Deleting a card
removes it from the corpus.

## Sync to AnkiWeb

Authoring a card stores it in precis; the **`precis anki-sync`** tick (a cron on
the one designated runner, gated `PRECIS_ANKI_ENABLED`) pushes precis-authored
cloze cards to your AnkiWeb account by a stable per-ref guid (re-sync *updates*,
never duplicates) and reads each card's decay stats (`interval/ease/reps/lapses/
due`) back into `meta.anki_stats`. The sync is account-safe: it will download to
resolve a divergence but **refuses any full upload** that would overwrite AnkiWeb.

## Fix a card with `precis-fix`

To have precis improve a card, in **Anki** add the tag **`precis-fix`** to it and
write what's wrong in a note field (e.g. `Back Extra`) — "answer should be left
ventricle", "too wordy", "add the year". On the next `precis anki-sync --fix`, an
LLM rewrites the card from your comment, writes it back, and retags it
`precis-fixed`. precis only ever edits a card **you tagged** — untagged cards are
never touched. Works on text notetypes (cloze/basic).

## What this is NOT

- **No image occlusion / structured notetypes** — cloze only for now. The note
  shape (`meta.notetype`/`meta.fields`) is generic, so other notetypes can be
  added later without a migration.
- **No SM-2 / recall rating in precis** — Anki schedules; precis mirrors the
  decay stats back (once the sync slice lands) but never grades.

## See also

```python
get(kind='skill', id='precis-cloze')        # HOW to write good cards (the craft)
get(kind='skill', id='precis-overview')     # verbs and kinds
get(kind='skill', id='precis-memory-help')  # prose notes that aren't recall targets
get(kind='skill', id='precis-tags')         # tag axis conventions
get(kind='skill', id='precis-relations')    # link relations (derived-from, …)
```
