---
id: precis-math-help
title: precis — math via Wolfram Alpha
status: shipped
tier: 1
floor: any
applies-to: get (kind='math')
last-updated: 2026-04-26
---

# precis-math-help — facts and computation via Wolfram|Alpha

`math` answers natural-language and mathematical queries through
Wolfram|Alpha. Use it for:

- World data (populations, distances, dates, GDP, …)
- Physical constants and unit conversions
- Calculus, linear algebra, number theory
- Anything you'd type into wolframalpha.com

```python
get(kind='math', q='population of Ireland')
get(kind='math', q='orbital period of Jupiter')
get(kind='math', q='integrate sin(x)*cos(x) dx')
get(kind='math', q='derivative of x^3 + 2x')
get(kind='math', q='speed of light in km/s')
get(kind='math', q='10000 BTC in USD')
```

`id=` and `q=` are equivalent. Queries are canonicalized
(lowercase + whitespace collapse) so case/whitespace variants share a
cache row.

## Caching & cost

Every result is cached and **pinned** — Wolfram results are
deterministic for a fixed query, so we never re-fetch automatically.
Cache hits cost nothing (cost trailer reads `[cost: ~$0.002 — cached]`).
Misses cost ~$0.002 per call (charged against your Wolfram tier).

## Attribution

Every response carries Wolfram's mandatory attribution footer with a
deep-link to the verifiable result page and a paste-ready academic
citation:

```
— Computed by Wolfram|Alpha. Results © Wolfram Alpha LLC; ...
  Verify: https://www.wolframalpha.com/input?i=population+of+ireland
  Cite:   Wolfram|Alpha, WolframAlpha["population of ireland"] (accessed 2026-04-26).
```

## When `math` fails

- **Timeout** (~20s upstream): "try a more specific query"
- **Wolfram doesn't understand**: response includes `Did you mean: …`
- **Empty result**: response says so explicitly

For deterministic offline arithmetic that doesn't need world data, use
`kind='calc'` — sympy-backed, free, no network.

## Required env

`WOLFRAM_APP_ID` must be set. Get a free App ID from
<https://products.wolframalpha.com/api>.

## See also

- `precis-overview` — verbs and kinds
- `precis-cache` — how cache freshness, attribution, and cost trailers work
