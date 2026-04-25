---
name: consult-iching
description: >
  Consult the I-Ching as a re-framing prompt for a stuck strategic
  decision.  Pull a single hexagram from oracle:iching, examine its
  three layers (heritage Yi-Jing / modern systems / cognitive lens),
  and write a one-paragraph re-frame anchored to the user's actual
  situation.  Specialised cousin of consult-oracle.
user-invocable: true
argument-hint: [situation or decision in one sentence]
allowed-tools: [get, search]
applies-to: [oracle]
kind-onboarding: oracle
tags: [reframing, decision-making, strategy, archetype, brainstorm, i-ching]
---

## When to use

- Strategic decision the user is going in circles on.
- "I can't tell if I should commit or back out."
- "What kind of moment is this?"
- Specifically: when the user wants the Yi-Jing texture (Chinese
  archetype, three-layer reading) rather than a random oracle pull.

For the broader version that mixes traditions, use
`skill:consult-oracle`.  This skill is the narrower, richer cousin
specialised to oracle:iching.

## Why three layers

Each I-Ching hexagram in oracle:iching ships with three baked
interpretations in its body:

- **Heritage** — classical Yi-Jing situation framing.
- **Modern (systems)** — modern systems / decision-theoretic translation.
- **Cognitive** — a named heuristic, bias, or model.

The point is *not* to pick the "right" layer.  Read all three, then
write a re-frame that anchors back to the user's situation.  The
layer that best clicks for them is the one you anchor in.

## Workflow

```
1. STATE the user's situation in one sentence.  Write it down.
2. PULL one hexagram:
       random:?corpus=oracle&tag=i-ching&n=1
   For a specific hexagram by id:
       get(id='oracle:iching/N')           — N is 0..63 (chunk index)
   For reproducibility / journalling:
       random:?corpus=oracle&tag=i-ching&seed=42
3. READ all three layers.  Don't skip any.
4. TRY each layer as a re-frame.  For each, ask: "If this were the
   pattern, what would the user do next?"  Write a one-liner per layer.
5. PICK the layer whose re-frame is most surprising AND tractable.
   Write a short paragraph anchoring it to the user's situation.
6. (Optional) PULL a second hexagram to compare.  If two converge on
   the same advice, that's a stronger signal.  If they diverge,
   surface both — let the user choose.
7. STOP.  Two hexagrams is usually enough.
```

## Specialised entry points

### Direct hexagram read

```
get(id='oracle:iching/0')      — first hexagram (chunk 0)
get(id='oracle:iching/12')     — chunk 12 (which is hexagram 13 in 1-based count)
get(id='oracle:iching/0..9')   — first 10 hexagrams
get(id='oracle:iching/toc')    — full table of contents
```

Note: chunk indices in oracle:iching are **0-based** (paper-style
chunk addressing), so `oracle:iching/0` is the first hexagram (Hexagram
1 — Creative Force) and `oracle:iching/63` is the last.  The 1-based
"Hexagram N" is in the chunk's title, not the chunk index.

### Search by symptom

```
search(query='cascading failure', type='oracle', tag='i-ching')
search(query='waiting for signal', type='oracle', tag='i-ching')
search(query='premature commit', type='oracle', tag='i-ching')
```

Vector search restricted to the I-Ching tradition.  Returns the most
semantically near hexagrams.  Useful when the user has a problem
keyword in mind.

### Cognitive-lens lookup

The I-Ching tradition tags each hexagram with a named cognitive lens
(Goodhart's Law, Pareto Principle, Action Bias, etc.) in the
`section_path`.  To find a hexagram by the lens it carries:

```
search(query="Goodhart's Law", type='oracle', tag='i-ching')
search(query="Pareto Principle", type='oracle', tag='i-ching')
```

## Anti-patterns

- **Treating the pull as authoritative.**  The hexagram is a prompt,
  not an answer.  An archetype that doesn't click should be discarded
  — pull again.
- **Mystic vocabulary.**  Don't tell the user what the trigrams
  "represent" in the cosmological sense.  The output is an LLM-tuned
  description of a *kind of situation*; use the modern + cognitive
  layers as the workhorses, treat heritage as flavour.
- **Adding fake interpretations.**  The data ships exactly three layers
  per hexagram.  Don't synthesise a fourth.  Don't extrapolate the
  classical text beyond what's printed.
- **Pulling more than three.**  Two hexagrams is the sweet spot.
  Three is "scanning the I-Ching for a comforting answer," which is
  the failure mode this skill is meant to break.
- **Skipping the anchor.**  Always re-state the user's situation
  before presenting the re-frame.  A hexagram without an anchor is
  fortune-cookie content.

## Worked example — short

> User: We've shipped a feature flag that 30% of users hate.  Roll back,
> push through, or rebuild?

```
1. Anchor: "feature flag, 30% rejection — roll back, push, or rebuild?"
2. Pull:
       random:?corpus=oracle&tag=i-ching&seed=42
       → oracle:iching/14 — Hexagram 15 · 謙 Modesty
         Heritage: stay grounded, reduce ego.
         Modern (systems) Ego Reduction: operate without self-promotion;
                          credibility grows where ego shrinks.
         Cognitive lens (heuristic) Dunning-Kruger (Inverse):
                          high competence correlates with greater
                          awareness of limits.

3. Per-layer re-frames:
   - Heritage: "Withdraw the loud version of the feature; keep the
     quiet substrate."
   - Modern: "Stop *promoting* it — keep shipping iterations.  Make it
     easy to opt out without making a public retreat."
   - Cognitive: "30% rejection is high-information feedback.
     Treat it as a signal of where the team's mental model is wrong,
     not where the *users* are wrong."

4. Pick: cognitive lens.  Anchor:
   "30% rejection is signal, not failure.  Don't roll back (kills
   signal).  Don't push through (signal already arrived).  Rebuild the
   *part of the team's mental model* that the rejection points at,
   then ship a quieter iteration."

5. (Optional) second pull for sanity check:
       random:?corpus=oracle&tag=i-ching&seed=43
   If second hexagram agrees → confidence up.  If diverges → present
   both.

6. Stop.
```

## See also

- `skill:consult-oracle` — same protocol, broader (any tradition).
- `skill:ideation` — three random samples across all corpora.
- `oracle:iching` — read the I-Ching tradition overview directly.
