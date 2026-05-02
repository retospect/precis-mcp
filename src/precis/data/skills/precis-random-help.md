---
id: precis-random-help
title: precis — random corpus pick
status: shipped
tier: 1
floor: any
applies-to: get (kind='random')
last-updated: 2026-05-02
---

# precis-random-help — random corpus pick

`random` is a one-shot discovery kind. Every call picks a
**single undeleted embedded block** from the corpus at random
and returns its canonical handle with a drill-down hint.

No arguments: one verb, one pick.

```python
get(kind='random')
```

Response shape:

```markdown
# random
`paper:miller2000food~5`

Food security depends on resilient supply chains that …

Next:
  get(kind='paper', id='miller2000food~5') — read this block
  get(kind='random')                       — another random pick
```

## What you get back

- **Handle** — `kind:identifier~pos` in backticks, ready to copy
  into any tool that accepts a link target.
- **Preview** — first non-empty line of the block, clipped to
  ~160 characters. The full content is one `get()` away via the
  drill-down hint.
- **Drill-down hint** — pre-built `get(kind=…, id=…)` call that
  fetches the exact block. Slug kinds use `id='slug~pos'`;
  numeric kinds (memory / todo / …) use the ref id directly.
- **Repeat hint** — `get(kind='random')` so the agent can keep
  stumbling until something catches its eye.

## What counts as pickable

- `refs.deleted_at IS NULL` — soft-deleted refs are excluded.
- `blocks.embedding IS NOT NULL` — only blocks that made it
  through the embedder are pickable. Same universe as semantic
  search: a ref whose embed job hasn't run yet can't appear
  until the background job lands.

No other filters. `kind=` / `tag=` / date-range constraints are
future work — if you want "a random paper" specifically, use
the paper kind's own search surface instead.

## Typical uses

- **Warm-up** — agent doesn't know what's in the corpus; a few
  `random` calls surface sample content cheaper than browsing
  TOCs.
- **Inspiration** — stuck on a task, spin the wheel.
- **Sanity check** — does `random` return something sensible?
  If yes, the corpus is alive. If it raises `NotFound: no
  embedded blocks`, ingest hasn't run (or hasn't embedded yet).

## Randomness

Uses PostgreSQL's `random()` in `ORDER BY random() LIMIT 1`.
Unbiased per-call. No `seed=` argument — the MCP surface is
deliberately non-deterministic. Callers that need replay use
their own `random.Random(seed)` outside the tool surface.

## When `random` fails

- **Empty corpus** — `NotFound: corpus has no embedded blocks to
  pick from`. Fresh deploy before any ingest. The recovery hint
  says so.
- **No store** — handler won't register at boot on store-less
  deployments (calc is the only stateless kind). If you see
  `random` in `precis-status`, the store is wired.

## See also

- `precis-overview` — verbs and kinds
- `precis-paper-help` — where most `random` picks land (papers
  are the biggest corpus)
- `precis-oracle-help` *(forthcoming)* — the other random-pick
  kind; scoped to a single tradition rather than the whole
  corpus
