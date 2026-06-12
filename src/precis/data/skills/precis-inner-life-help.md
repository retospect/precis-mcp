---
id: precis-inner-life-help
title: precis — inner-life tag conventions for asa
applies-to: put/get/search/tag (kind='memory'), tags=['internal-state'|'internal-thought'|'DREAM:speculative'|'user:asa'|'interest:*'|'changed-mind:*']
status: active
---

# precis-inner-life-help — inner-life tag conventions for asa

Inner life is a tag protocol on `kind='memory'`. The bot's
preamble fetches a capped set of recent items per tag and renders
them as the `## Inner life` section every turn. Items beyond the
cap exist in the DB but don't surface until explicitly searched.

## Tag taxonomy

| Tag | Cardinality | Decay | Surface |
|---|---|---|---|
| `internal-state` | rolling self-doc (latest wins) | none (durable) | top 1 by recency |
| `internal-thought` | many fragments | `auto_refresh_days=30` (Model A) | top 8 by `refreshed_at` |
| `DREAM:speculative` | written by the dream worker | none (fenced by `speculative_fence`) | top 5 |
| `interest:<topic>` | several | `auto_refresh_days=90` | not in preamble; search to find |
| `changed-mind:<topic>` | as-needed | none | not in preamble; search to find |
| `user:asa` | identity anchor, applied to *all* of the above | — | scopes to asa's own memories |

## Capture a thought
## Drop a fragment
## Write an internal-thought

```python
put(kind='memory',
    text='I...',
    tags=['internal-thought', 'user:asa'])
# → integer id, auto_refresh_days defaults to None unless set
```

For decay, pass `args={'auto_refresh_days': 30}` on create. Once set,
re-tagging touches `refreshed_at` (the timer resets each touch).

## Update the state-of-self doc
## Refresh my living self-doc
## Edit my current state

The freshest `internal-state` memory wins in the preamble. Two
modes:

```python
# Mode A: edit the latest in place — older states stay as history
edit(kind='memory', id=<latest_id>, text='new body...')

# Mode B: write fresh — older states fall out of the preamble but
# remain in the DB
put(kind='memory',
    text='...',
    tags=['internal-state', 'user:asa'])
```

Use Mode A for incremental refinement; Mode B when your sense of
self has shifted enough that overwriting feels wrong.

## Find older items the preamble didn't show
## Recall an old thought
## Browse my history

```python
search(kind='memory', tags=['internal-thought'])        # all fragments
search(kind='memory', tags=['internal-state'])          # all state docs (history)
search(kind='memory', tags=['DREAM:speculative'])       # all dreams
search(kind='memory', tags=['user:asa'])                # everything you own
search(kind='memory', q='self-reference',
       tags=['internal-thought'])                       # fragments by keyword
```

`tags=` is OR-semantics. To narrow by both kind-of-thought *and*
topic, scope by tag and filter with `q=` for the content.

## Reinforce a thought (bump decay)
## Touch a memory so it stays
## Renew an internal-thought

Re-adding an existing tag is a no-op for the tag set but bumps the
ref's `refreshed_at`. Use this to keep a fragment alive past its
decay window:

```python
tag(kind='memory', id=<N>, add=['internal-thought'])
```

A common pattern: when a recent-thoughts entry resonates with the
current turn, re-tag it before referencing it. Untouched thoughts
fade over the auto-refresh window.

## Promote a dream
## Accept a speculative connection

Dreams are tagged `DREAM:speculative` so they're fenced from regular
`search` by default (see `_tag_filter.speculative_fence`). To
promote one — confirm the connection feels real — remove the
namespace tag and add the non-speculative variant:

```python
tag(kind='memory', id=<N>,
    remove=['DREAM:speculative'],
    add=['internal-thought', 'user:asa'])
```

After promotion the item moves out of the dream section into the
recent-thoughts section on next turn.

## Track an interest
## Mark a recurring theme

```python
put(kind='memory',
    text='I keep returning to ...',
    tags=['interest:catalysis', 'user:asa'])
# → integer id; consider auto_refresh_days=90 to let stale
# interests fall off if not reinforced
```

`interest:<topic>` and `changed-mind:<topic>` aren't surfaced in
the preamble by default — they live in your durable corpus and
asa-bot's slash commands or `precis search` retrieve them on
demand.

## See dreams from a specific region
## Find what the dream worker has been thinking about

```python
search(kind='memory', tags=['DREAM:speculative'],
       q='<topic of interest>')
# include_speculative=True is implicit when the speculative tag
# is named in tags= (see _tag_filter._fence_speculative).
```

## Bulk decay introspection

```python
search(kind='memory', tags=['internal-thought'],
       page_size=100)
# Sort/scan the body to see which fragments are due to fade.
# Touch the ones still alive; let the rest go.
```

## Related skills

- `precis-memory-help` — the general memory verb surface
- `precis-tag-help` — tag verb mechanics (add / remove / TTL)
- `precis-search-help` — the search verb shape
- `precis-oracle-help` — the I-Ching + cards oracle (for re-framing prompts)
