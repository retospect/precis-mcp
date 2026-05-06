---
id: precis-random-help
title: precis — random corpus pick
status: shipped
tier: 1
floor: any
applies-to: get (kind='random')
last-updated: 2026-05-02
---

# precis-random-help — random corpus pick + slug minting

`random` has two modes:

1. **Default (no `view`)** — picks a single undeleted embedded
   block from the corpus at random and returns its canonical
   handle with a drill-down hint. Useful for warm-up, discovery,
   stumbling-into-content.
2. **`view='slug'`** — mints a fresh random short identifier
   (default 4 chars, Crockford alphabet — lowercase letters and
   digits with visually ambiguous chars 0/o/1/l excluded). Useful
   for tags, correlation ids, opaque handles where a semantic
   name isn't available.

```python
get(kind='random')                                       # corpus pick
get(kind='random', view='slug')                          # 4-char slug
get(kind='random', view='slug', args={'len': 8})         # 8 chars
get(kind='random', view='slug', args={'alphabet': 'lower'})    # a-z only
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

## Slug view (`view='slug'`)

Mints a fresh random alphanumeric identifier. Pure function;
no corpus dependency, works on stateless deploys too.

```python
get(kind='random', view='slug')                                  # → 'k7m3'
get(kind='random', view='slug', args={'len': 8})                 # → 'k7m3pq2v'
get(kind='random', view='slug', args={'alphabet': 'alnum'})      # a-z + 0-9
get(kind='random', view='slug', args={'alphabet': 'lower'})      # a-z only
get(kind='random', view='slug', args={'alphabet': 'xyz'})        # custom literal
```

Defaults: `len=4`, `alphabet='crockford'` (32 chars). Length is
clamped to `[1, 64]`. Custom alphabets must be a string of ≥ 2
distinct characters.

Response body is the slug itself — no markdown wrapper, no
trailer. Compose it directly into a tag value, a correlation
id, etc.

Use cases that fit:

- A tag value when no semantic name applies (`topic:k7m3`).
- A correlation id stamped onto a memory and the prose marker
  that points at it.
- Any opaque handle where uniqueness matters more than meaning.

Where you should *not* use it:

- Identifiers that humans will read (use slugs that encode
  meaning instead — `topic:dopamine-d2-pharmacology`, not
  `topic:k7m3`).
- Cryptographic keys (use a longer length and the `alnum`
  alphabet, not the 32-char Crockford).

## Randomness

Default block pick uses PostgreSQL's `random()` in
`ORDER BY random() LIMIT 1`. Unbiased per-call.

Slug minting uses Python's :mod:`secrets` module (CSPRNG-backed)
for character selection.

No `seed=` argument on either path — the MCP surface is
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
