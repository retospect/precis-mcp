---
id: precis-put-help
title: precis — the put verb (write or annotate)
status: active
tier: 1
floor: any
applies-to: put (every kind that supports it)
last-updated: 2026-05-24
---

# precis-put-help — write or annotate

`put` creates new refs and applies metadata to refs (tags, links).
For sub-region rewrites of an existing ref, reach for `edit`
instead.

```python
put(kind='memory', text='...')                  # create a numeric-ref
put(kind='markdown', mode='create',
    id='notes/foo.md', text='...')              # create a file
put(kind='memory', id=42, tags=['pinned'])      # annotate (tags only)
```

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `kind` | str | required | Which kind to write to. |
| `mode` | str | None | Operation hint. Kind-specific (see below). |
| `id` | str / int | None | Target ref. Omit on numeric-ref kinds to create new. Required on file kinds with `mode='create'`. |
| `text` | str | None | Content for create or text update. |
| `tags` | list[str] | None | Tags to **add** to the ref on creation. For tag removal use the `tag` verb. |
| `link` | str | None | Add a link `kind:identifier[~selector]` to a new ref. For link removal use the `link` verb. |
| `rel` | str | None | Relation slug for `link=`. Defaults to `related-to`. |

## `mode=` matrix

Each kind family has its own `mode=` discipline:

- **File kinds** (`markdown`, `plaintext`, `tex`, `python`):
  `put` is **creation-only** since the seven-verb cutover.
  `mode='create'` is required and the only accepted value.
  Region edits (`append` / `insert` / `replace` / `find-replace`)
  live on `edit`; whole-file deletes live on `delete`.
- **Numeric-ref kinds** (`memory`, `todo`, `gripe`, `conv`, `fc`,
  `quest`): omit `mode=` to create a new ref; `mode='delete'`
  soft-deletes (or use the `delete` verb directly).
- **`perplexity`**: `mode='import'` ingests a pre-generated report
  as a $0 cache entry.

Unknown modes are rejected with a clear error.

## Tags via put

`tags=` applies tags **at creation time** — the convenient one-call
shape when a new ref ships with metadata:

```python
put(kind='memory',
    text='Schedule the next sortie review for 2026-Q3.',
    tags=['topic-sortie', 'pinned'])
```

For retroactive tag changes (adding / removing on an existing ref)
use the `tag` verb:

```python
tag(kind='todo', id=158,
    add=['STATUS:done'],
    remove=['STATUS:open'])
```

Tag vocabulary follows the cross-kind convention — see
`precis-tag-help` for the full matrix (closed prefixes, flag tags,
open tags, per-kind axis gating).

## Links via put

`link=` attaches a link **at creation time** — the convenient
one-call shape when a new ref ships with an outbound edge:

```python
# Create a memory that already cites a paper.
put(kind='memory',
    text='Wang20 cites our 2024 result indirectly.',
    link='paper:wang2020state', rel='cites')
```

For retroactive link changes (adding / removing on an existing
ref) use the `link` verb:

```python
link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites')          # add

link(kind='memory', id=42,
     target='paper:wang2020state', rel='cites',
     mode='remove')                                       # remove one (target, rel)

link(kind='memory', id=42,
     target='paper:wang2020state', mode='remove')         # remove every link to target
```

`rel=` defaults to `related-to`. See `precis-link-help` for the
full relation vocabulary.

## Worked examples

### Memory, fresh

```python
put(kind='memory',
    text='Schedule the next sortie review for 2026-Q3.',
    tags=['topic-sortie', 'pinned'])
```

### Markdown file, create

```python
put(kind='markdown', mode='create',
    id='notes/proj-fbproj.md',
    text='# fbproj — project notes\n\n## Goals\n- ...\n')
```

### Patent: not supported

`put` doesn't apply to `patent` (read-only via OPS). To annotate a
patent, link a `memory` to it:

```python
put(kind='memory',
    text='Verification batch — see patent.',
    link='patent:ep4123456a1', rel='annotates')
```

## See also

- `precis-edit-help` — sub-region rewrites of existing refs
- `precis-delete-help` — soft-delete and selector-delete
- `precis-tag-help` — tag vocabulary and per-kind axis gating
- `precis-link-help` — link grammar and relation vocabulary
- `precis-files-help` — file-kind addressing (slug discipline)
