---
name: rng-basics
description: >
  Random number generator — integers, floats, dice, list pick / shuffle,
  UUIDs, random bytes.  Stateless, free, stdlib-only.  Use any time the
  agent needs a sample, a coin flip, dice rolls, or to break a tie.
  Pass `?seed=<int>` for reproducibility.
user-invocable: true
argument-hint: [<rng-uri-tail>]
allowed-tools: [get]
applies-to: [rng]
kind-onboarding: rng
tags: [random, dice, sample, shuffle, uuid]
---

## When to use

- Coin flip, dice roll, random pick.
- Pick one item uniformly from a list, or shuffle a list.
- Generate UUIDs or random bytes.
- Sample integers / floats from a range.

For a vector-space-aware sampler that picks semantically near a seed
string, see `random:` (different kind, see `skill:random-basics`).

## Integers — primary currency, both ends inclusive

```
get(id='rng:')              → int [0, 1]   (coin flip)
get(id='rng:100')           → int [0, 100]
get(id='rng:1..6')          → int [1, 6]
get(id='rng:1..6x4')        → 4 samples
get(id='rng:3d6')           → three six-sided dice + sum
```

## Floats — opt-in, half-open [lo, hi)

```
get(id='rng:float')         → float [0.0, 1.0)
get(id='rng:float/0..1')    → same, explicit
get(id='rng:float/-1..1')   → float in any range
```

## Lists

```
get(id='rng:choice/a,b,c')      → uniform pick
get(id='rng:shuffle/a,b,c,d')   → random order
```

The whole tail after the slash is one expression — commas inside it are
list separators, **not** batch separators.  `rng:` is in
`_NO_COMMA_SPLIT_KINDS` so the URI is dispatched intact.

## Crypto-grade

```
get(id='rng:uuid')          → UUID4 (CSPRNG-backed)
get(id='rng:bytes/16')      → N random bytes, hex-encoded
```

Crypto modes ignore `?seed=` — seeding the CSPRNG is a footgun and
deliberately disabled.

## Reproducibility

Pass `?seed=<int>` to make any non-crypto call deterministic:

```
get(id='rng:?seed=42/3d6')
get(id='rng:?seed=7/1..6x10')
```

## See also

- `get(id='rng:/help')` — same content as this skill, inline.
- `skill:random-basics` — vector-space-aware sampling (different kind).
