---
id: precis-math-help
title: precis — facts and world data via Wolfram Alpha
summary: computational knowledge via Wolfram Alpha — facts, world data, unit conversion, math expressions
applies-to: get (kind='math')
status: active
---

# precis-math-help — facts and world data via Wolfram Alpha

`math` answers a natural-language `q=` with text from Wolfram Alpha.
Paid (~$0.002/call); cached automatically so repeats are free.

## Ask Wolfram a question
## Look up a fact or world-data value
## I need a number Wolfram would know

```python
get(kind='math', q='population of Ireland')
get(kind='math', q='speed of light in km/h')
get(kind='math', q='orbital period of Jupiter')
get(kind='math', q='10000 BTC in USD')
get(kind='math', q='integrate sin(x)*cos(x) dx')
get(kind='math', q='eigenvalues of [[2,1],[1,3]]')
```

`id=` and `q=` are equivalent. Queries are canonicalised (lowercase
+ whitespace collapse) so casing variants share one cache row.

## When to use math vs calc

| Use `math` for | Use `calc` for |
|---|---|
| World data (populations, GDP, distances) | Arithmetic, algebra on values you have |
| Physical constants, unit conversions | Symbolic SymPy work, no network |
| Anything wolframalpha.com handles | Anything SymPy handles |
| Paid (~$0.002/call, cached) | Free, local |

```python
get(kind='calc', q='42 * 365')                # local SymPy, free
get(kind='math', q='speed of light in km/h')  # Wolfram, paid
```

If the answer doesn't need world data, prefer `calc`.

## Re-fetch a math result
## Force a fresh Wolfram call
## How do I bypass the cache for one query?

Math results are pinned (deterministic for a fixed query), so
`get` never re-fetches on its own. To force a fresh call:

```python
delete(kind='math', id='<canonicalised-query>')
get(kind='math', q='<query>')
```

The canonicalised id is the lowercased, whitespace-collapsed query
text.

## When math fails

- Timeout (~20s upstream): rephrase more specifically.
- Wolfram doesn't parse the query: response includes `Did you mean: …`.
- Empty result: response says so explicitly.

Every response carries Wolfram's attribution footer with a verify
link and a paste-ready citation.

## Required env

`WOLFRAM_APP_ID` must be set. Free App ID:
<https://products.wolframalpha.com/api>.

## See also

```python
get(kind='skill', id='precis-overview')        # verbs and kinds
get(kind='skill', id='precis-cache')           # TTLs, pinning, force-refresh
get(kind='skill', id='precis-perplexity-help') # websearch / perplexity-reasoning / perplexity-research
```
