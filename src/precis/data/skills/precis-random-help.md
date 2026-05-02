---
id: precis-random-help
title: precis — random values, dice, picks, neighbors
status: shipped
tier: 1
floor: any
applies-to: get (kind='random')
last-updated: 2026-05-02
---

# precis-random-help — dice, picks, and random draws

`random` is a stateless CSPRNG-backed value generator. Five DSL
forms, all passed as `id=`:

| Form | Example | Returns |
|---|---|---|
| **Dice** | `get(kind='random', id='3d6+3')` | Total of 3d6 rolls plus 3 |
| **Integer** | `get(kind='random', id='int(1..100)')` | Uniform integer in `[1, 100]` |
| **Choice** | `get(kind='random', id='choice(red\|green\|blue)')` | One pipe-separated option |
| **Neighbor** | `get(kind='random', id='neighbor(paper:slug~42)')` | Top-K vector-nearest blocks |
| **Chunk** | `get(kind='random', id='chunk(paper:slug)')` | One random block from a ref |

`id=` and `q=` are equivalent — both are accepted.

## Dice — `NdM[±K]`

Classic polyhedral notation. `N` defaults to 1 (so `d20` is
`1d20`); `K` is an optional integer modifier.

```python
get(kind='random', id='d20')       # 1d20, no modifier
get(kind='random', id='3d6')       # sum of three 6-sided dice
get(kind='random', id='3d6+3')     # + a fixed 3
get(kind='random', id='4d8-1')     # - 1
```

The response echoes each individual roll when `N > 1` so a GM can
spot-check or re-roll specific dice.

**Caps:** up to `1000d1000000` per request. Beyond the sides cap,
use `int(1..N)` for a single huge-range draw.

## Integer — `int(LO..HI)`

Uniform inclusive integer in `[LO, HI]`. Negative bounds are fine,
whitespace around the operators is ignored.

```python
get(kind='random', id='int(1..100)')
get(kind='random', id='int(-5..5)')
get(kind='random', id='int( 1 .. 10 )')  # whitespace OK
```

Reversed bounds (`int(10..1)`) are rejected with a hint to swap
them — no silent empty pick.

## Choice — `choice(A|B|C)`

Uniform pick from pipe-separated options. Whitespace around each
option is trimmed. Empty options are filtered out; a fully empty
list is rejected.

```python
get(kind='random', id='choice(heads|tails)')
get(kind='random', id='choice(yes|no|maybe)')
get(kind='random', id='choice( red | green | blue )')
```

## Neighbor — `neighbor(kind:id~pos)`

Top-K vector-nearest blocks to a given block. The selector is
**required** — refs themselves have no embedding, only their
blocks do. Default K is 5; override with `top_k=N` (up to 50).

```python
get(kind='random', id='neighbor(paper:wang2020state~42)')
get(kind='random', id='neighbor(oracle:rubric-rigor~3)', top_k=10)
```

The source block is excluded from results (its distance to itself
is always zero — not useful). Each row shows the canonical handle
plus cosine distance so the agent can gauge similarity, not just
rank.

Requires a wired store **and** embedder. Stateless deployments
get a clear `BadInput` pointing at the forms that do work.

## Chunk — `chunk(kind:id)`

Pick one random block from a ref. Useful for spot-checking a
corpus, generating quote-of-the-day style prompts, or sampling
training data.

```python
get(kind='random', id='chunk(paper:wang2020state)')
get(kind='random', id='chunk(oracle:iching)')
```

The body is the full block text plus a `kind:id~N` handle so the
agent can revisit the same block deterministically if desired.
Block-level targets (`chunk(paper:slug~0)`) are rejected — chunk
picks *from* a ref, not from a single block.

Requires a wired store.

## Randomness source

Every draw goes through [`secrets.randbelow`][secrets] — the
Python standard library's CSPRNG. Consistent with `oracle`'s
random-entry picker, and deliberately non-deterministic across
requests: the MCP surface does not accept a `seed=` argument.

Callers that need reproducible sequences should use their own
`random.Random(seed)` outside the MCP surface; `random` itself is
a dice-roller, not a replay tool.

[secrets]: https://docs.python.org/3/library/secrets.html#secrets.randbelow

## When `random` fails

- **Unknown form** — DSL didn't match any of the five regexes;
  the recovery hint lists all of them.
- **Out-of-bounds cap** — dice count or sides exceeds the DoS
  cap; the hint suggests the closest legal shape.
- **Reversed int range** — `int(HI..LO)` with `LO < HI`.
- **Neighbor on ref** — `neighbor(kind:id)` without a selector.
- **Chunk on block** — `chunk(kind:id~pos)` with a selector.
- **Stateless + ref form** — `neighbor` / `chunk` called on a
  deployment without a wired store.

All errors carry a `next=` hint pointing at the legal form.

## See also

- `precis-overview` — verbs and kinds
- `precis-paper-help` — where most `neighbor()` / `chunk()` targets live
- `precis-oracle` — the other CSPRNG-backed kind (random-entry default)
