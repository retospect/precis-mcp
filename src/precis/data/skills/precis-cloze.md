---
id: precis-cloze
title: precis — authoring excellent Anki cloze cards
summary: how to write good spaced-repetition cloze cards — refuse non-facts (the gate), dedup, one cluster per card, easiest-to-hardest cN ordering, specific uniquely-recoverable answers, hint types, ruthlessly terse, contextual Back Extra; with language/math worked examples
answers:
  - how do I write a good spaced-repetition cloze card instead of a bad one?
  - how do I decide which deck a card goes in?
  - what belongs in Back Extra vs the card front?
  - how do I make cloze cards for language characters or math notation?
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

Follow the rules below, in order. Rule 0 is the gate — most bad cards are bad
because they should never have been made at all; rule 1 is non-negotiable.

### 0. Is this even worth a card? — refuse if not

Before anything else, decide whether the source carries a **specific, durable
fact a curious learner would want to know cold**. If it doesn't, make **no
card**. A missing card costs nothing; a bad one wastes reviews forever *and*
buries the good cards under noise, which is the fastest way to make someone
abandon the deck. When in doubt, refuse.

In the automated `card_forge` mint path, refusing means returning an empty
cards list with a `skip_reason`; authoring by hand, it just means not writing
it. Refuse when the "concept" is:

- **A topic label or research-direction, not a fact** — *"new trends"*, *"new
  compounds"*, *"recent advances"*, *"critical evaluation"*. The card can only
  restate the label (*"a major new trend is novel compounds"*) — there is no
  hidden answer to recall, and nothing is learned.
- **A stock academic phrase** — *"extensively examined"*, *"widely studied"*,
  *"plays a key role"*. Teaching what a rhetorical phrase *signals* is not
  domain knowledge; it's trivia about prose.
- **Document front-matter or scaffolding** — a dedication, acknowledgements, an
  author/affiliation, a running header, figure-caption boilerplate. If it came
  from the front or back matter of a source, it is almost never a concept.
- **An incidental proper noun or one-of-many example** — a place, org, or name
  the source merely mentions in passing (a *"World Heritage Site"* named inside
  a dedication; an invented acronym like *"ESB"*). Not the subject you're
  learning.
- **So generic the definition is its own answer** — if every cloze you can
  imagine just echoes words already printed on the card, there's no fact under
  it.

The test: *cover the answer — could a motivated learner reconstruct it, and
would knowing it make them meaningfully smarter about the subject?* If either
answer is "no", refuse.

### 1. Search first — never duplicate

Your **whole** Anki collection is searchable in precis (it's projected in). So
before creating anything, check for an existing card:

```python
search(kind="anki", q="powerhouse of the cell")
search(kind="anki", q="mitochondrion ATP")  # a couple of angles
```

If a card already covers the fact, **don't add another** — extend or improve the
existing one (or, for a hand-made card, tag it `precis-fix` in Anki). Duplicate
cards split your reviews and corrode the schedule.

### 2. One semantic cluster per card

Each card tests **one** idea-cluster — an etymology, a mechanism, a definition, a
cultural contrast, a usage. Not two. If you're tempted to cram, make a second
card.

If the same answer text appears more than once in the sentence, cloze
**every** occurrence under the **same** cN. An unclozed repeat leaks the
answer (same failure as rule 5) — it isn't extra practice, it's required:
`{{c1::heart}} pumps blood through the arteries, capillaries, and veins back
to the {{c1::heart}}.`

### 3. Order cN by difficulty — easiest first, hardest last

Assign cloze indices so the card ramps up instead of opening on its hardest
recall:

- **c1** — easiest: the fact most inferable from context, or the core
  meaning/mechanism.
- **Mid indices** — supporting facts.
- **Highest index** — hardest: the exact symbol/term/word, a number, a name —
  whatever has to be recalled cold, no context shortcut.

Meaning-before-label is the common case of this: meaning is usually easier to
reconstruct from context than the exact term, so it earns the low index.

### 4. Hint the *type* of thing being recalled

`{{cN::answer::hint}}` — the hint names the category, so the card is answerable
but not a guess: `::meaning`, `::pronunciation`, `::domain`, `::number`,
`::date`, `::mechanism`, `::example`, `::term`, `::image`, `::latex`. Terse,
one word.

**But a hint can't rescue an answer that isn't specific.** Only cloze a span
that is load-bearing and has *one* right value the rest of the sentence points
to — a term, number, name, mechanism. Never cloze:

- **Vague filler** — *"improved properties"*, *"various applications"*, *"many
  advantages"*. You can't *recall* a vague phrase; hiding it tests nothing.
  Leave it as plain text — and if the vague phrase was the only candidate, the
  card shouldn't exist (rule 0).
- **One arbitrary member of a large open set** — hiding *"Machu Picchu"* in
  *"an example of a World Heritage Site is `{{c1::Machu Picchu}}`"* is
  unanswerable: a thousand sites fit equally, so there's no cued recall, only a
  blind guess. A cloze answer must be the thing the context *determines*, not
  one interchangeable instance of it. If the *category* is the point, cloze the
  category, not a sample member.

### 5. No cloze gives away another on the same card

If hiding `{{c1::X}}` makes `{{c2::Y}}` obvious (or vice-versa), split them across
cards or rephrase. Each deletion must be independently earned.

### 6. Terse — but never at the cost of comprehension

No filler, no full sentences **unless needed for comprehension** (or grammar).
Drop articles and scaffolding words that don't carry meaning — "The heart
pumps the blood" → "Heart pumps blood." Keep whatever the reader needs to
actually understand the prompt; cut until it's tight, not until it's
ambiguous.

### Before you save: check each cN in turn

Walk the clozes one at a time. For each `cN`, ask: does hiding this test a
fact worth recalling, or is it filler/inferable-from-grammar? Is the answer
**specific** enough that its context determines it — not a vague phrase, not
one of many equally-valid fillers? Drop the cloze (leave it as plain text) — or
the whole card — if any answer is no. If dropping the clozes leaves nothing
worth testing, the concept failed rule 0: refuse the card entirely.

### Aim for ~3–4 cards per concept

A typical concept: intro/definition, mechanism-or-etymology, a
contrast-or-nuance, a usage/example. More than ~4 usually means you're not
clustering (rule 2).

## Which deck does a card go in?

Tag `deck-<topic>` to file the card under the `Precis::<topic>` sub-deck (Anki
auto-creates it); no tag → the base `Precis` deck. Keep authored cards under the
`Precis::` namespace, away from hand-made decks:

```python
put(
    kind="anki",
    text="{{c1::Mitochondrion::term}} makes {{c2::ATP::molecule}}.",
    tags=["deck-cell-biology"],
)  # → deck Precis::cell-biology
```

## What goes in Back Extra

Not just a source/mnemonic footnote — use it to carry the **context** that
makes the card efficient to study standalone: a prerequisite fact, how this
connects to material you're already learning, a pointer to the paper/deck it
came from. Terse still applies (rule 6), but richer than "aka X" when the
card needs it. If a card only makes sense alongside something else you're
studying, put that bridge in Back Extra rather than stretching the front
sentence to carry it — the front stays a clean cloze, the context lives where
it belongs.

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

## Worked example — math notation (LaTeX)

Same pairing pattern as the character scheme: the **meaning** at a low index,
the **raw LaTeX** (what you'd type) at the highest index, hinted `::latex`.

```
\(A^{\top}\) denotes the {{c1::transpose::meaning}} of A, written
{{c2::^\top::latex}} in LaTeX.
```

- **To render**, wrap in Anki's MathJax delimiters: `\(...\)` inline,
  `\[...\]` display. Plain `$...$` is **not** parsed by default — it won't
  render, it just shows the literal dollar signs, and this bites hardest in a
  sentence with several math clauses, not just one symbol:
  `In $J_f(x)^{\top}$, ${}^{\top}$ means {{c1::transpose}}` renders literally;
  `In \(J_f(x)^{\top}\), \({}^{\top}\) means {{c1::transpose}}` renders. Fix
  **every** `$...$` span in the sentence, including ones inside a cloze
  answer, not just the first. Delimiters work whether they sit inside or
  outside a cloze; put them *inside* (`{{c2::\(^\top\)::latex}}`) if you want
  the reveal itself to render the glyph rather than show the raw source.
- **Gotcha — adjacent `}` truncates the cloze.** Anki (and precis) find a
  cloze's end by scanning for the first literal `}}`. If your LaTeX answer's
  own closing braces land next to each other — nested groups like
  `\sqrt{x^{2}}` or `\frac{a}{b}`-style constructs — that inner `}}` is
  indistinguishable from the cloze's own closer and the answer gets silently
  truncated (confirmed: `{{c1::\sqrt{x^{2}}::latex}}` stores as
  `\sqrt{x^{2` — a hint alone does **not** save you here, only the character
  spacing does). Break up any `}}` inside the answer with a space or `\,`:
  `\sqrt{x^{2} }` is safe, `\sqrt{x^{2}}` is not.

## See also

```python
get(kind="skill", id="precis-anki-help")  # verbs, sync, precis-fix, /leeches
get(kind="skill", id="precis-tags")  # tag conventions (incl. deck-<topic>)
```
