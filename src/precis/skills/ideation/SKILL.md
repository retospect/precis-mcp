---
name: ideation
description: >
  Generate fresh ideas for a stuck problem by sampling distant points
  in vector space and forcing analogies.  Iterative — try a sample,
  evaluate, sample again with different parameters until a useful idea
  surfaces or the search budget is exhausted.  Reaches for `random:`
  (vector sampler) primarily; `iching:` (archetype reframing) and
  `rng:` (Monte-Carlo branching) are secondary tools.
user-invocable: true
argument-hint: [problem statement]
allowed-tools: [get, search]
applies-to: [random, iching, rng]
kind-onboarding: random
tags: [ideation, brainstorm, lateral, creativity, problem-solving]
---

## When to use

- The user is stuck.  They've already tried direct approaches.
- "I'm spinning on this — give me a fresh angle."
- "What am I not seeing?"
- "Brainstorm three different ways to attack X."
- Anywhere the *deterministic* tools (`search`, `calc`, `paper`) keep
  returning the same answer the user already considered.

Skip this skill when the user wants a *correct* answer (use `calc:`,
`math:`, `web:`).  Ideation is for *novel* answers.  The two are
different jobs.

## Core idea — distal sampling

Stuck thinking is local.  The fix is to look far away.  Pull random
points from a corpus the user *isn't already in*, and force an analogy.
The further the sample, the more likely the analogy is non-obvious;
the closer, the more likely it's already been considered.

**Prefer distance over relevance.**  A blast-radius pick three corpora
away (e.g. a chengyu while debugging a deploy) tends to yield more
than a near neighbour in the same domain.  Reach for `random:?n=3`
without a seed string before reaching for `random:<seed>?radius=0.3`.

## Workflow

```
1. STATE the anchor problem in one sentence.  Write it down explicitly.
2. SAMPLE three distant points:
       get(id='random:?n=3&corpus=wisdom')
   or, if no wisdom corpus is loaded:
       get(id='random:?n=3&corpora=papers,memories,books')
3. FORCE an analogy.  For each sample, ask: "If this were the answer,
   what would the answer look like?  How does this re-frame the
   problem?"  Write a one-liner per sample.
4. EVALUATE.  Use the rubric below.  If none of the three pass — go
   back to step 2 with different parameters (different corpus, larger
   n, or a blast-radius shot near the anchor).
5. STOP when one analogy passes the rubric, OR after 3 sampling
   rounds.  Anchor in the original problem in the answer — don't drift.
```

## Evaluation rubric (per sampled idea)

A useful sampled idea satisfies at least three of these:

- **Surprising** — wasn't on the user's earlier list.
- **Tractable** — concrete enough to act on within the next hour.
- **Anchor-preserving** — still about the original problem, not a
  digression.
- **Falsifiable** — the user can test it and know within a day if it
  worked.

Surprising-but-vague is a failure mode.  So is tractable-but-on-list.
Surprising + tractable is the target.

## Sampling moves (in order of preference)

### Move 1 — distant uniform pick (preferred)

```
get(id='random:?n=3&corpus=wisdom')
get(id='random:?n=3&corpora=papers,memories')
get(id='random:?n=5')               # widest possible — any corpus
```

Why first: zero coupling to the problem.  Forces the largest cognitive
leap in the analogy step.  Most reliable for breaking stuck loops.

### Move 2 — i-ching reframing

```
get(id='iching:?layer=cognitive')   # random hexagram, modern lens
```

Why second: the 64 hexagrams are an opinionated archetype set,
pre-curated for "general types of situation."  Useful when the user is
in a *kind* of stuck (e.g. waiting, accumulating, pivoting) more than
a *content* of stuck.

### Move 3 — blast-radius near the problem

```
get(id='random:<problem text>?radius=0.5&n=3')
```

Why last: results are semantically *near* the anchor, which is exactly
what the user already explored.  Useful only when moves 1 and 2
returned irrelevant samples (corpus too sparse, or the problem is
genuinely niche).  Tighten ``?radius=0.3`` for closer hits, loosen
``?radius=0.7`` for stranger ones; default is "no cap."

### Move 4 — Monte-Carlo branching (rare)

```
get(id='rng:choice/feature,refactor,defer,delegate')
```

Use only when the user has already enumerated K options and just
needs a forced commit.  This is decision support, not ideation.

## Anti-patterns

- **Chasing a sample**.  Don't grab one sample and run with it
  immediately.  Always pull at least three so the user has a
  comparative basis.
- **Over-blasting**.  Five rounds of `random:<problem>?radius=…` will
  burn the user's attention without producing distance.  Switch
  corpus or move to `iching:` instead.
- **Cheating the seed**.  `?seed=42` makes ideation reproducible (good
  for documentation), but if results aren't surprising, *change the
  seed* — don't keep retrying with the same one.
- **Drifting from the anchor**.  Always re-state the original problem
  before presenting analogies.  An analogy that requires changing the
  question is a digression, not an idea.
- **Ignoring the rubric**.  "It's surprising" is necessary but not
  sufficient.  If the user can't act on it within a day, it's not an
  ideation result — it's a research project.

## Worked example — short

> User: I keep hitting a deadlock between two services on deploy.

```
1. Anchor: "intermittent two-service deploy deadlock"
2. Sample (move 1):
     get(id='random:?n=3&corpus=wisdom')
     → 1. zhao-san-mu-si — "morning three, evening four" — the same
          quantity, framed two ways, looks different.
     → 2. blind-men-elephant — partial views of the same thing all
          contradict.
     → 3. principle-of-least-astonishment — UI design rule.

3. Force analogies:
     1: are the two services pricing the *same operation* differently
        depending on which one starts the deploy first?  Probably no
        — but worth checking deploy-time configs.
     2: each service sees a partial view of the lock state.  Is there
        a missing health-check that would surface partial blocking?
     3: which side surprises which?  The *first* service to come up
        completes; the *second* gets blocked.  Why?  Maybe the
        first holds a lease the second waits for.

4. Evaluate:
     1 — tractable but probably stretched, score 2/4 — drop.
     2 — surprising, tractable, anchor-preserving, falsifiable — 4/4.
     3 — surprising, anchor-preserving, less tractable — 3/4 — keep.

5. Stop.  Present 2 (primary) and 3 (alternate).  Anchor: "the
   deadlock — the second service to come up always blocks."
```

## See also

- `skill:iching-consult` — heavy reframing through one specific
  archetype.  Good for high-context strategic decisions.
- `skill:find-paper` — when the right move is *more* search rather
  than ideation.
- `random:/help` and `iching:/help` — reference cards for the
  underlying kinds.
