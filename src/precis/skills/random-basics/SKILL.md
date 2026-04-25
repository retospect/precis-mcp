---
name: random-basics
description: >
  Vector-space-aware content sampler — picks refs uniformly from a
  corpus, or semantically near a seed string ("blast radius").  Free,
  read-only, backed by `acatome-store`.  Use to break out of local
  attractors, surface forgotten material, or seed ideation.
user-invocable: true
argument-hint: [<seed?>]
allowed-tools: [get]
applies-to: [random]
kind-onboarding: random
tags: [random, sample, ideation, blast-radius]
---

## When to use

- Surface a forgotten paper / memory / web bookmark — uniform pick.
- "What does my corpus have to say about X" — blast-radius pick by seed.
- Seed ideation by combining unrelated material.

For raw random *numbers* (dice, coin flip, UUIDs, bytes) use `rng:`
instead — see `skill:rng-basics`.

## Uniform pick — no seed

```
get(id='random:')                              → one ref, any corpus
get(id='random:?n=3')                          → three refs
get(id='random:?corpus=papers')                → filter to one corpus
get(id='random:?corpora=papers,memories')      → multi-corpus
get(id='random:?corpus=oracle&n=3')            → three oracle chunks
get(id='random:?corpus=oracle&tag=stoic')      → stoic tradition only
get(id='random:?corpus=oracle&not-tag=built-in') → personal traditions
get(id='random:?seed=42')                      → reproducible
```

## Blast radius — sample near a seed string

The seed goes in the path slot of the URI.  Results are picked from
the cosine-similarity neighbourhood of the seed.

```
get(id='random:my problem?n=5')
get(id='random:cascading failure?corpus=oracle')
get(id='random:refactor?radius=0.4&n=3')
get(id='random:cross-pollinate?corpora=papers,memories')
```

## Knobs

| Param        | Meaning                                       |
|--------------|-----------------------------------------------|
| `?n=<int>`   | Number of results (1–20; default 1)           |
| `?corpus=<id>` | Single-corpus filter                        |
| `?corpora=<a,b>` | Multi-corpus filter                       |
| `?radius=<0..1>` | Cosine distance ceiling (blast only)      |
| `?seed=<int>` | Reproducible (uniform only)                  |
| `?tag=<t>` / `?not-tag=<t>` | Tag filters                    |

## Distinct from

- `rng:` — raw random numbers, no corpus, no embedding.
- `search()` — deterministic top-K over an explicit query.
- `random:` — sampled / filtered content, designed to surprise.

## See also

- `get(id='random:/help')` — same content as this skill, inline.
- `skill:rng-basics` — pure-stdlib RNG (different kind).
