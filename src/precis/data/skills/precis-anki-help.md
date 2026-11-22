---
id: precis-anki-help
family: memory
title: precis — Anki cloze cards
summary: author spaced-repetition cloze cards ({{c1::…}}) that live in the corpus and sync to AnkiWeb
answers:
  - how do I turn something I just read into a spaced-repetition card?
  - how do I find cards I keep forgetting / leeches?
  - how do I file a card under a deck or tag it?
  - how does a card sync to AnkiWeb?
  - what is the daily card forge and do I need to run it myself?
applies-to: get/search/put/delete/tag/link (kind='anki')
status: active
supersedes: precis-flashcard-help (flashcard kind retired 2026-07)
tags: [drafting, workflow]
kinds: [anki]
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
put(kind="anki", text="{{c1::Paris::city}}: capital of {{c2::France::country}}.")
# → returns integer id (e.g. 204)

put(
    kind="anki",
    text="The {{c2::mitochondrion::cellular organelle}} is the {{c1::powerhouse::popculture function name}} of the cell.",
    tags=["topic:cell-bio"],
    link="pc12",
    rel="derived-from",
)
```

Every card needs **at least one** `{{cN::…}}` deletion, or the put is rejected.
For *how to write good cards* (dedup-first, cN ordering, hints, deck naming), see
**`precis-cloze`** — this skill is the reference; that one is the craft.

### Cloze syntax

- **One deletion, one fact.** `{{c1::…}}` hides the span behind it. Keep the
  deletion minimal and unambiguous — test one idea, not a whole sentence.
- **Several cards from one note.** Different indices → separate cards:
  `{{c1::heart::organ}} pumps {{c2::blood}}.` makes two cards. The *same* index
  reveals together: `{{c1::A}} and {{c1::B}}` hides both on one card.
- **Hints.** `{{c1::answer::hint}}` shows *hint* on the front. The hint is
  dropped from the corpus search text (only the answer is indexed).

### Optional terse "Back Extra"

Add a short answer-side note — a source, a mnemonic, a gotcha — after a lone
`---` line at the end of the body. Keep it terse. Omit it only when the card
carries no symbol, variable or acronym: each one is defined there
([[precis-cloze]] rule 7):

```python
put(
    kind="anki",
    text="""\
The {{c1::Krebs}} cycle occurs in the mitochondrial matrix.
---
<ul><li>TCA = tricarboxylic acid</li></ul><hr>aka the citric-acid / TCA cycle""",
)
```

Everything before `---` is the cloze card; everything after is Back Extra.
Anki renders Back Extra as HTML: the list holds the definitions, `<hr>`
separates them from the note.

## Attach a photo to a card

```python
put(
    kind="anki",
    text=(
        "At the poster session, ask {{c1::Avery Chen}} (Example Lab) "
        "about replication data."
    ),
    args={"image": "https://example.org/speakers/avery-chen.jpg"},
    tags=["deck-conference"],
)
```

Use a direct public HTTPS URL serving a PNG or JPEG up to 1 MiB. The card
saves a copy when you create it. After AnkiWeb sync, the photo appears on
both sides of the cloze card. Anki clients need media sync enabled to
download it.

For conference prep, use a memory ref for a speaker or talk. Record the
speaker's name and affiliation, the talk title, the event source and the
direct portrait URL in the note. Pass the portrait URL as `args.image` when
creating the card and link the memory with `link=` and `rel="derived-from"`.
Put the question you want to ask in the card text.

## Showing a card in chat

When you tell the user about a cloze card — confirming what you just made,
answering "what does that card say?" — quote the **raw markup verbatim**:
`{{c1::^\top::symbol}}: {{c2::transpose::meaning}}`. Don't paraphrase it into a
`c1: … c2: …` bullet list; that summary hides the actual deletions and syntax
the user needs to verify. Prose *about* the card (deck, tags, why you chose
that split) is fine alongside it — just don't substitute for the raw text.

## Browse existing cards
## List recent Anki cards

```python
get(kind="anki")  # /recent (default)
get(kind="anki", id="/recent")  # 20 newest
get(kind="anki", id=204)  # one card — body + note meta
```

## Search across cards
## What anki cards do I have?
## Find anki cards about X
## Search my Anki collection
## Check whether I already made a card about X

```python
search(kind="anki", q="capital of france")
search(kind="anki", q="citric acid cycle", tags=["topic:cell-bio"])
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
get(kind="anki", id="/leeches")  # high-lapse / collapsed-ease cards, worst first
```

Reads the recall stats (`meta.anki_stats`, refreshed each sync) across the whole
collection. For each bad-recall card you then decide: **fix the cloze** (tag it
`precis-fix` in Anki — the LLM rewrites it) or **study it more**.

## Tag or link a card
## File a card under a deck

Tag `deck-<topic>` to file an authored card under the `Precis::<topic>` sub-deck
(no tag → the base `Precis` deck):

```python
put(kind="anki", text="{{c1::heart}} pumps blood.", tags=["deck-anatomy"])
tag(kind="anki", id=204, add=["topic:cell-bio"])
link(kind="anki", id=204, target="pc12", rel="derived-from")
```

To re-file a card, retag it — add the new `deck-<topic>` tag and remove the
old one in the same `tag()` call; a set of cards moves in one call with
`id=[204, 205, ...]` (up to 300, all or nothing). To select cards by tag,
page through `search(kind="anki", tags=["aixmat-2026"])` and pass the
matching card IDs to `tag()`. `meta.deck` follows the surviving deck tag
and the Anki move lands on the **next successful `anki-sync` push**. Keep one deck tag
per card: the filed deck wins while its tag remains, so a second deck tag
does nothing until the first is removed. A card you move by hand inside Anki
keeps its deck until a precis retag changes the filed deck. On rollout,
existing Anki cards keep their decks unless there is a recorded retag to
apply. If a push requires a full sync, the retag stays pending for retry.

## Edit or remove a card

Bodies are immutable (like all numeric-ref kinds): to reword a card,
`delete(kind='anki', id=204)` then `put` the new wording. Deleting a card
removes it from the corpus.

## Sync to AnkiWeb

Authoring a card stores it in precis; the **`precis anki-sync`** tick (a daily cadence on
the one designated runner, gated `PRECIS_ANKI_ENABLED`) pushes each web user's
own precis-authored cloze cards to *their own* AnkiWeb account — the
credentials you set on `/account` — by a stable per-ref guid (re-sync
*updates*, never duplicates) and reads each card's decay stats
(`interval/ease/reps/lapses/due`) back into `meta.anki_stats`. Every card
carries `refs.owner_login`, so one sync tick never touches another user's
account or mirror. The sync is account-safe: it will download to resolve a
divergence but **refuses any full upload** that would overwrite AnkiWeb.

**Sync now.** After a batch of edits, don't wait for the daily tick:
`put(kind='job', job_type='anki_sync', parent_id=<todo id>,
params={'user': '<your login>'})` syncs just your cards. `user` binds to
your own login (it may be omitted; you cannot sync another user's account,
and a session with no authenticated login is refused). It is refused within 10
minutes of your last manual sync and while a failed sync is backing off,
so it cannot hammer AnkiWeb; if another sync already holds the account's
lock the job says so instead of running.

**Boost a card (show it sooner).** The `/learning` page's *boost* button (and
`meta.anki_boost_due = <today's date>` written directly) queues a card to come
up today: on the next sync tick the mirror card's **due date moves to today**
— interval, ease, reps and lapses are never touched, so review history is
never rewritten. The marker clears only once a mirror card actually moved;
until then the card shows *boost queued*. A card whose note is not yet in
the mirror — or whose cards are all still new or mid-step, with no
day-numbered due to move — keeps its marker and boosts on a later tick.

## Fix a card with `precis-fix`

To have precis improve a card, in **Anki** add the tag **`precis-fix`** to it and
write what's wrong in a note field (e.g. `Back Extra`) — "answer should be left
ventricle", "too wordy", "add the year". On the next `precis anki-sync --fix`, an
LLM rewrites the card from your comment, writes it back, and retags it
`precis-fixed`. precis only ever edits a card **you tagged** — untagged cards are
never touched. Works on text notetypes (cloze/basic).

## The daily card forge (autonomous)

Every morning at 01:30 (before the reading brief) the **`card_forge`** job runs
the reading-prep card loop — installed by `precis cast schedule`:

1. **Mastery refresh** — each `concept`'s `represents`-linked cards' `anki_stats`
   fold into `meta.mastery` (0..1) + `state` (`mastered` ≥ 0.7 by default).
2. **Rework** — a forge-authored card ≥ `PRECIS_CARD_REWORK_MIN_DAYS` (4) days
   old that is still a leech gets a graph-informed decision: concept mastered →
   **retire** the card; unlearned prerequisite → **teach that first**; too many
   rewrites → **escalate** to you (surfaced in the morning brief); otherwise
   **rewrite** (new card, curve resets — the old one wasn't working anyway).
   Observe-first: with `PRECIS_CARD_FORGE_AUTONOMY=report` (default) it only
   logs what it *would* do; set `act` to let it write.
3. **Mint** — up to `PRECIS_READING_CARDS_PER_DAY` (5) cardless concepts get
   1–3 fresh cloze cards each, linked `concept —represents→ card`, deck
   `Precis::<cohort>`, riding the next `anki-sync` to your phone.

Retired (soft-deleted) precis-authored cards are removed from the Anki mirror on
the next sync — own-guid lookups only, so your hand-made cards are unreachable.

The `/learning` page's *I know this* retires your cards for that concept and
marks it mastered; its **Undo** (under *Marked as mastered*) restores those
cards and the prior mastery. If Anki already removed the notes, the next sync
recreates them with fresh review schedules. This is web-only; there is no
tool call for it.

## See also

- [[precis-cloze]] — HOW to write good cards (the craft)
- [[precis-overview]] — verbs and kinds
- [[precis-memory-help]] — prose notes that aren't recall targets
- [[precis-tags]] — tag axis conventions
- [[precis-relations]] — link relations (derived-from, …)
