---
id: precis-cloze
family: memory
title: precis — authoring excellent Anki cloze cards
summary: how to write good spaced-repetition cloze cards — refuse non-facts (the gate), dedup, one cluster per card, easiest-to-hardest cN ordering on new cards only (never renumber reviewed cards), hint/prose non-duplication (cut "The term"-style lead-ins), specific uniquely-recoverable answers, telegraphic with no in-sentence context (context lives in tags), contextual Back Extra; with language/math worked examples
answers:
  - how do I write a good spaced-repetition cloze card instead of a bad one?
  - how do I decide which deck a card goes in?
  - what belongs in Back Extra vs the card front?
  - how do I make cloze cards for language characters or math notation?
applies-to: put/search (kind='anki')
status: active
tags: [drafting]
kinds: [anki]
---

# precis-cloze — how to write cloze cards that actually stick

Reference (verbs, sync, precis-fix): `precis-anki-help`. This is the **craft**:
how to author cards worth reviewing. Cards are `kind='anki'`, body is cloze
markup (`{{cN::answer::hint}}`).

Cards are per user (`refs.owner_login`) — a card you author syncs only to
*your* AnkiWeb account, the one you configured on `/account`.

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

- **A to-do or gap about our own system** — refuse `SPACE:repo-dev` memories,
  `section:threads`, gap/backlog lists and lines like *"we lack"*, *"our
  spec has no"* or *"missing is"*. Use a factual source to teach any
  underlying concept on its own. Drop gap IDs and run rule 1 before minting.
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
- **Common vocabulary** — a word any educated reader already knows. Add
  vocabulary cards only for genuinely complex or domain-specific terms.
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

Definitional clozes of one concept share **one** combined card, not several
near-copies: fold *"X is {{c1::a}}"* and *"X is {{c2::b}}"* into one cloze
set. When the answer is one item of a long list, don't cloze a single item;
use the terse shape `{{c1::Topic}} includes {{c2::item1}} {{c3::item2}}`.

If the same answer text appears more than once in the sentence, cloze
**every** occurrence under the **same** cN. An unclozed repeat leaks the
answer (same failure as rule 5) — it isn't extra practice, it's required:
`{{c1::heart}} pumps blood through the arteries, capillaries, and veins back
to the {{c1::heart}}.`

## How do I write good anki cards? — rules 3–4

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

This is authoring guidance for a **new** card only — the index is a fresh
choice at authoring time. **Never renumber an existing card**: Anki keys
review history to the cN ordinal, so renumbering moves the learner's lapse
and interval history onto different text. A failing card gets a *new* card
(delete + put), not renumbered indices. And don't try to rank difficulty by
guessability at write time — the learner's own review grades are the
difficulty signal; easiest-first is a ramp for the first encounter, not a
measurement.

### 4. Hint the *type* of thing being recalled

`{{cN::answer::hint}}` — the hint names the category, so the card is answerable
but not a guess: `::meaning`, `::pronunciation`, `::domain`, `::number`,
`::date`, `::mechanism`, `::example`, `::term`, `::image`, `::latex`. Terse,
one word.

For a recognition card with a photo, attach it with
`put(kind="anki", args={"image": "<direct portrait URL>"}, ...)`. See
`precis-anki-help` for the image requirements and a conference example.

For an acronym or abbreviation, show the short form and cloze the expansion
or meaning. Never hide the short form with its expansion as the hint.
`RMSD ({{c1::root-mean-square deviation::expansion}})` tests recall of what
RMSD stands for. A put warning flags hints whose initials spell the answer.

**The hint and the prose must not duplicate each other.** The hint shows only
on its own card; every other word of the sentence shows on *every* sibling
card and on the fully revealed sentence. So:

- **Cut lead-in scaffolding that merely announces what the hint already
  names** — *"The term {{c1::available::term}} means …"* → *"{{c1::Available::term}}
  means …"*; *"The character {{c1::字::kanji}} …"* → *"{{c1::字::kanji}} …"*.
  "The term", "The character", "A X is" carry no fact — the learner reads them
  on every review of every sibling. (The put-time lint warns when a word right
  before a cloze repeats its hint.)
- **A hint that only restates what the grammar already forces adds nothing** —
  in *"capable of being {{c2::obtained::action}}"*, "action" is recoverable
  from "being …-ed" alone. Drop the hint, or make it carry something the
  sentence does not: the field, a distinction, a register.

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

## How do I write good anki cards? — rules 5–6

### 5. No cloze gives away another on the same card

If hiding `{{c1::X}}` makes `{{c2::Y}}` obvious (or vice-versa), split them across
cards or rephrase. Each deletion must be independently earned.

### 6. Terse — but never at the cost of comprehension

Telegraphic by default: no filler, no full sentences **unless needed for
comprehension** (or grammar). Drop articles and scaffolding words that don't
carry meaning — "The heart pumps the blood" → "Heart pumps blood." That
includes lead-in scaffolding that only announces a cloze's hint ("The term",
"The character", "A X is") — rule 4. Keep whatever the reader needs to
actually understand the prompt; cut until it's tight, not until it's
ambiguous.

**No context prefix inside the sentence.** An opening clause like "In cell
biology, …" is a tax on every review, and sentence text cannot be regrouped
in bulk — cards are kept forever, so anything you may want to re-group later
must not be baked into thousands of card bodies. Context lives in tags: the
Anki viewer shows tags, and tags can be re-grouped across the whole
collection at once. Keep the sentence the bare fact; file the card with
`deck-<topic>` and topic tags instead.

## How do I write good anki cards? — the final check

### Before you save: check each cN in turn

Walk the clozes one at a time and render each as the learner sees it — read
the sentence with only that `cN` hidden: *"Available means capable of being
[action] or accessed."* For each rendered prompt, ask: does hiding this test
a fact worth recalling, or is it filler/inferable-from-grammar? Does the
blank have **one** answer its context determines — a field-knowledgeable
learner could recover it — not a vague phrase, not one of many
equally-valid fillers? Drop the cloze (leave it as plain text) — or the
whole card — if any answer is no. If dropping the clozes leaves nothing
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
    text="{{c1::Mitochondrion::term}} makes ATP ({{c2::adenosine triphosphate::expansion}}).",
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
  {{c1::字::kanji}} is pronounced {{c2::pinyin::Mandarin}} and
  {{c3::yomi::Japanese}}.
  ```
  No "The character" — the `::kanji` hint already names it (rule 4); no "in
  Chinese"/"in Japanese" — the `::Mandarin`/`::Japanese` hints carry the
  language.
- **Tone card** (Mandarin — isolate the tone):
  ```
  Tone {{c1::2::number}} — {{c2::rising::shape}}
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
{{c2::^\top::latex}}.
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
- Nested LaTeX braces inside an answer are safe: `{{c1::\sqrt{x^{2}}::latex}}`
  is stored with a space between the touching `}`, so Anki shows the whole
  answer.

## See also

- [[precis-anki-help]] — verbs, sync, precis-fix, /leeches.
- [[precis-tags]] — tag conventions (incl. `deck-<topic>`).
