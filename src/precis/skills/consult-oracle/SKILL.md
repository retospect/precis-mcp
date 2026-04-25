---
name: consult-oracle
description: >
  Pull one wisdom card from the oracle corpus to re-frame a stuck
  decision.  Optional ``tradition`` argument narrows to chengyu /
  proverbs / stoic / iching / engineering / talmudic / zen / personal.
  Not divination — a structured device for forcing a different angle
  on a hard call.
user-invocable: true
argument-hint: "[situation in one sentence] [optional: tradition=stoic]"
allowed-tools: [get, search]
applies-to: [oracle]
kind-onboarding: oracle
tags: [reframing, decision-making, strategy, wisdom, brainstorm]
---

## When to use

- Strategic decision the user is going in circles on.
- "I can't tell if I should commit or back out."
- "What kind of moment is this?"
- A high-context call where the *type of situation* matters more than
  the specific facts (waiting / accumulating / pivoting / overextending /
  exhausting / rebuilding).

Skip when the user wants ideation across the whole problem (use
`skill:ideation`), tactical detail (use `skill:find-paper` /
`web:`), or a numerical answer (use `calc:`).

This skill is a **prompt generator**, not an answer generator.  Use
when "what am I looking at" is more useful than "what should I do."

## Core idea — one card, one re-frame

Every oracle entry is a compact archetype: a chengyu, a proverb, a
stoic aphorism, an I-Ching hexagram, an engineering principle.  They
all do the same thing — name a recurring kind-of-situation in one
or two sentences.  The skill's job is to:

1. Anchor the user's situation in one sentence.
2. Pull one entry.
3. Try the entry as a re-frame.
4. Anchor back to the user's situation.

That's it.  The wisdom doesn't need to be Chinese or Stoic to land —
the wisdom is in the **forcing function** (sample distally, anchor
back).

## Workflow

```
1. STATE the user's situation in one sentence.  Write it down.
2. PULL one card:
       random:?corpus=oracle&n=1
   For a specific tradition:
       random:?corpus=oracle&tag=stoic&n=1
       random:?corpus=oracle&tag=chengyu&n=1
       random:?corpus=oracle&tag=engineering&n=1
   For reproducibility / journalling:
       random:?corpus=oracle&seed=42
   To exclude built-ins (only personal entries):
       random:?corpus=oracle&not-tag=built-in
3. READ the card.  Don't skip the original-language source if there is
   one — it's part of the texture.
4. TRY the card as a re-frame.  Ask: "If this were the pattern, what
   would the user do next?"
5. ANCHOR back to the user's situation.  Write a short paragraph that
   joins the user's original sentence to the card's frame.
6. (Optional) PULL a second card to compare.  If two cards converge on
   the same advice, that's a stronger signal.  If they diverge,
   surface both — let the user choose.
7. STOP.  Two cards is usually enough.  Three is digression.
```

## Specialised entry points

### By tradition

```
get(id='oracle:iching')         — read the I-Ching tradition overview
get(id='oracle:iching/12')      — hexagram 12 specifically
get(id='oracle:chengyu')        — chengyu tradition overview
get(id='oracle:chengyu/3')      — chengyu entry 3
get(id='oracle:stoic')          — stoic overview
get(id='oracle:engineering')    — engineering aphorisms
```

Use the *direct read* path when the user names a specific tradition or
entry.  Use the *random pull* path when the user wants to be surprised.

### By search

```
search(query='cascading failure', type='oracle')
search(query='waiting for signal', type='oracle', tag='i-ching')
search(query='premature commit', type='oracle', tag='engineering')
```

Vector search across all oracle traditions, optionally filtered by
tradition tag.  Returns the most semantically near entries.  Useful
when the user has a problem keyword in mind but doesn't know which
tradition it lives in.

### List traditions

```
get(id='oracle:')               — all traditions in the corpus
get(id='oracle:/by-tradition')  — same, alias
```

Useful as a discovery surface when the user asks "what kinds of
wisdom do you have?"

### Reproducibility

`?seed=<int>` makes a pull deterministic.  Use when:

- The user wants to journal the consultation (date + seed = stable
  reference).
- A second LLM session needs to re-examine the same card.

If you find yourself trying many seeds to find one that "fits",
switch to `search()` — you're looking for a specific frame, not a
random one.

### Personal vs. built-in

```
random:?corpus=oracle&not-tag=built-in    — only personal entries
random:?corpus=oracle                     — both pools
```

Built-in entries (chengyu, proverbs, stoic, etc.) are tagged
`['oracle', 'built-in', '<tradition>']`.  User-written entries are
tagged `['oracle']` (with whatever else the user adds).  The
`not-tag=built-in` filter is the cleanest way to scope to personal-only
when the user wants their own canon.

## Anti-patterns

- **Treating the pull as authoritative.**  The card is a prompt, not
  an answer.  An archetype that doesn't click should be discarded —
  pull again.
- **Adding fake interpretations.**  The card text says what it says.
  Don't extrapolate beyond what's printed.  Especially: don't invent
  classical sources or dress up modern aphorisms as ancient wisdom.
- **Pulling more than three.**  Two cards is the sweet spot.  Three is
  "scanning for a comforting answer," which is exactly the failure
  mode this skill is meant to break.
- **Skipping the anchor.**  Always re-state the user's situation
  before presenting the re-frame.  A card without an anchor is
  fortune-cookie content.
- **Ignoring the search affordance.**  When the user has a clear
  keyword (`"cascading failure"`, `"waiting"`, `"sunk cost"`),
  searching beats sampling.  Random sampling is for when the user
  *can't articulate* the keyword — they know they're stuck but
  not what kind of stuck.

## Worked example — short

> User: We've shipped a feature flag that 30% of users hate.
> Roll back, push through, or rebuild?

```
1. Anchor: "feature flag, 30% rejection — roll back, push, or rebuild?"
2. Pull:
       get(id='random:?corpus=oracle&seed=42')
       → oracle:iching/14 — 謙 Modesty
         Heritage: stay grounded, reduce ego.
         Modern (systems): operate without self-promotion;
                           credibility grows where ego shrinks.
         Cognitive: Dunning-Kruger (Inverse) — high competence
                    correlates with greater awareness of limits.

3. Try as re-frame:
   "If this were the pattern, the rejection isn't a vote against the
   feature — it's a vote against the *promotion* of the feature.
   30% rejection is signal that the team's mental model is off, not
   that the users are wrong.  Rebuild the *part of the team's model*
   that the rejection points at, ship a quieter iteration."

4. (Optional) second pull for sanity check:
       get(id='random:?corpus=oracle&seed=43')
       → oracle:chengyu/2 — Drawing legs on a snake
         Adding superfluous detail beyond the point of usefulness
         ruins the work.

   Confirms: don't double down on extra polish; ship a quieter
   iteration.

5. Stop.
```

## See also

- `skill:consult-iching` — same protocol, narrowed to the I-Ching
  tradition with three-layer rendering.  Use when the user
  specifically wants the Yi-Jing texture.
- `skill:ideation` — three random samples + analogy, broader than
  oracle (samples across all corpora).
- `oracle:/help` — reference card for the kind itself.
- `random:/help` — reference card for the random sampler.
