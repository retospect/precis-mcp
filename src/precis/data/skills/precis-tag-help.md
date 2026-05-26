---
id: precis-tag-help
title: precis — the tag verb (add and remove tags)
status: active
tier: 1
floor: any
applies-to: tag (every kind that supports it)
last-updated: 2026-05-24
---

# precis-tag-help — manage tags on a ref

`tag` adds and / or removes tags on an existing ref. Both `add`
and `remove` are accepted in the same call so a transactional
STATUS bump is atomic:

```python
tag(kind='todo', id=158,
    add=['STATUS:done'],
    remove=['STATUS:open'])
```

For tag vocabulary that spans every kind, see `precis-tags`. This
skill covers the `tag` verb's mechanics and the per-kind axis
gating that determines which closed prefixes are accepted on which
kind.

## Arguments

| Arg | Type | Default | Meaning |
|---|---|---|---|
| `kind` | str | required | Kind owning the ref. |
| `id` | str / int | required | Ref id (slug for slug kinds, int for numeric kinds). |
| `add` | list[str] | None | Tags to add. |
| `remove` | list[str] | None | Tags to remove. |

Tags can also be applied **at creation time** via `put(... tags=...)`
when a fresh ref ships with metadata. The dedicated `tag` verb is
for retroactive changes — use `tag(remove=...)` to drop a tag.

## Tag vocabulary

Three flavours flow through the same surface:

- **Closed UPPERCASE prefixes** (`STATUS:`, `PRIO:`, `SRC:`,
  `CACHE:`) — replace within the prefix on add. Adding
  `STATUS:done` implicitly removes any existing `STATUS:*`. Each
  closed prefix is **gated per-kind** (matrix below).
- **Flag tags** (bare lowercase: `pinned`, `draft`,
  `awaiting-fulltext`) — toggle on / off. No prefix.
- **Open tags** (`topic-noxrr`, `cpc:B01J27/24`,
  `applicant:siemens-ag`, `2026-q2`, `fbproj`) — add and remove
  freely on any kind that supports tagging.

## Per-kind closed-prefix gating

| Kind family | Allowed closed prefixes |
|---|---|
| `todo` / `gripe` / `quest` | `STATUS`, `PRIO` (workflow kinds) |
| `memory` / `fc` / `conv` | none (use open tags like `confidence-strong`) |
| `paper` / `patent` | `SRC`, `CACHE` (provenance + cache freshness) |
| `web` / `research` / `think` / `websearch` / `youtube` | `CACHE` only |
| `oracle` / `skill` | none (read-only references) |
| `python` / `calc` / `math` | tag verb unsupported (read-only / stateless) |
| `markdown` / `plaintext` / `tex` | none on the closed-prefix axis (open tags only; the `workspace` flag is auto-applied at create) |

A closed prefix rejected on a kind raises
`[error:BadInput] axis not allowed on kind 'K'` — that's the
expected response for cross-axis attempts, not a bug.

For the authoritative per-axis matrix (which prefixes accept
multiple values, which are single-valued, etc.) see `precis-tags`.

## Worked examples

### Workflow STATUS bump (transactional)

```python
tag(kind='quest', id=42,
    add=['STATUS:done', 'PRIO:lo'],
    remove=['STATUS:open', 'PRIO:hi'])
```

### Topic tagging

```python
tag(kind='paper', id='wang2020state',
    add=['topic-noxrr', 'topic-photocatalysis'])
```

### Pin / unpin

```python
tag(kind='memory', id=42, add=['pinned'])
tag(kind='memory', id=42, remove=['pinned'])
```

### Cache freshness

```python
# Force a refetch on the next get(...) call (CACHE-supporting kind).
tag(kind='web', id='example-org-blog',
    add=['CACHE:stale'])
```

## Default tags via `PRECIS_DEFAULT_TAGS`

When the `PRECIS_DEFAULT_TAGS` env var is set, the active list
is surfaced as a hint on `tag` calls for note-like kinds —
reminding the operator about ambient project context without
auto-applying it (`tag` is explicit user intent; defaults never
override). See `precis-session-context-help` for details.

## See also

- `precis-tags` — cross-kind tag conventions and the full axis
  matrix
- `precis-paper-tag-axes` — paper-specific axes (topic, src, …)
- `precis-put-help` — applying tags during ref creation
- `precis-relations` — typed links (the *link* verb's vocabulary;
  distinct from tags)
