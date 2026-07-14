---
id: precis-cloze
title: precis — authoring excellent Anki cloze cards
summary: how to write good spaced-repetition cloze cards — dedup first, one cluster per card, educational-priority cN ordering, hint types, terse; with a language-learning worked example
applies-to: put/search (kind='anki')
status: active
---

# precis-cloze — how to write cloze cards that actually stick

Reference (verbs, sync, precis-fix): `precis-anki-help`. This is the **craft**:
how to author cards worth reviewing. Cards are `kind='anki'`, body is cloze
markup (`{{cN::answer::hint}}`).

## How do I write good anki cards?
## Make a set of cloze cards on a topic
## Author flashcards for a subject

Follow the rules below, in order. The first is non-negotiable.

### 1. Search first — never duplicate

Your **whole** Anki collection is searchable in precis (it's projected in). So
before creating anything, check for an existing card:

```python
search(kind='anki', q='powerhouse of the cell')
search(kind='anki', q='mitochondrion ATP')   # a couple of angles
```

If a card already covers the fact, **don't add another** — extend or improve the
existing one (or, for a hand-made card, tag it `precis-fix` in Anki). Duplicate
cards split your reviews and corrode the schedule.

### 2. One semantic cluster per card

Each card tests **one** idea-cluster — an etymology, a mechanism, a definition, a
cultural contrast, a usage. Not two. If you're tempted to cram, make a second
card.

### 3. Order cN by educational priority

Assign cloze indices by what should be learned *first*:

- **Low indices (`c1`–`c3`)** — the core conceptual content (meaning, mechanism,
  the "why").
- **Mid indices** — supporting facts.
- **Highest indices** — the symbol/term/word *itself* and its pronunciation.

This way the label is only tested *after* its meaning-cluster has consolidated —
you learn the idea, then attach the name to it, not the reverse.

### 4. Hint the *type* of thing being recalled

`{{cN::answer::hint}}` — the hint names the category, so the card is answerable
but not a guess: `::meaning`, `::pronunciation`, `::domain`, `::number`,
`::date`, `::mechanism`, `::example`, `::term`, `::image`. Terse, one word.

### 5. No cloze gives away another on the same card

If hiding `{{c1::X}}` makes `{{c2::Y}}` obvious (or vice-versa), split them across
cards or rephrase. Each deletion must be independently earned.

### 6. Terse — but never at the cost of comprehension

No filler, no full sentences **unless needed for comprehension** (or grammar).
Drop articles and scaffolding words that don't carry meaning; keep whatever the
reader needs to actually understand the prompt.

### Aim for ~3–4 cards per concept

A typical concept: intro/definition, mechanism-or-etymology, a
contrast-or-nuance, a usage/example. More than ~4 usually means you're not
clustering (rule 2).

## Which deck does a card go in?

Tag `deck-<topic>` to file the card under the `Precis::<topic>` sub-deck (Anki
auto-creates it); no tag → the base `Precis` deck. Keep authored cards under the
`Precis::` namespace, away from hand-made decks:

```python
put(kind='anki', text='The {{c1::mitochondrion::term}} makes {{c1::ATP::molecule}}.',
    tags=['deck-cell-biology'])          # → deck Precis::cell-biology
```

## Worked example — a language-learning scheme (Chinese/Japanese characters)

A concrete application of the rules for character study. Pair the **character and
its pronunciation at the same cN index** so they hide/reveal together
(`{{cN::字::kanji}} ({{cN::pronunciation::CN/JP}})`), and put that pair at the
*highest* index so the meaning-cluster is tested first.

- **Intro card** (one per character — the exception: split char + pronunciation
  into separate indices for independent first exposure):
  ```
  The character {{c1::字::kanji}} is pronounced {{c2::pinyin::Mandarin}} in
  Chinese and {{c3::yomi::Japanese}} in Japanese.
  ```
- **Tone card** (Mandarin — isolate the tone):
  ```
  {{c1::2::tone number}} — {{c2::rising::tone shape}}
  ```
- **Concept cards** (etymology, philosophical/cultural meaning, compounds) — test
  the concept at low indices, pair char+pronunciation at the highest shared
  index. ~3–4 cards per character total.

The same principles generalize to any domain — the character scheme is just rules
2–4 made concrete.

## See also

```python
get(kind='skill', id='precis-anki-help')     # verbs, sync, precis-fix, /leeches
get(kind='skill', id='precis-tags')          # tag conventions (incl. deck-<topic>)
```
