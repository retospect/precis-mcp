---
id: precis-edit-protocol
title: precis — anchored edits across every file kind
status: draft proposal — not yet implemented
tier: 2
floor: any
applies-to: put with op='edit' or op='insert' on any R/W file kind (markdown, plaintext, rmk, docx, tex, book, python)
last-updated: 2026-04-30
---

> **Heads up:** the `edit` and `insert` ops described here are a
> proposal (`docs/edit-protocol-spec.md`). Until they ship, use
> `mode='replace'` with the existing selector grammar.

# precis-edit-protocol — sub-region anchored search/replace

For changes smaller than a whole region, use `op='edit'` instead of
`mode='replace'`. The grammar is **identical across every file
kind**; only the validation gates differ. Per-kind quirks (cross-
region rules, AST gates, paragraph integrity) live in each kind's
skill — this one covers what's universal.

## When to reach for it

| You want to | Use |
|---|---|
| Rewrite a whole block / function / paragraph | `mode='replace'` (exists today) |
| Change one token, one cite, one literal | `op='edit'` |
| Add a paragraph next to an existing anchor | `op='insert'` |
| Bulk rename one identifier | `op='edit'` with `match='all'` |
| Apply N related changes atomically | `edits=[…]` batch |

The rule of thumb: **content selects, range bounds.** Line ranges
narrow where to look; the literal `find=` decides what to change.
This survives drift after prior edits.

## Schema

```python
put(kind='<kind>', id='<path>[~<selector>]',
    op='edit',           # or 'insert'
    find='<exact text>', # literal by default; regex=True opts in
    before='<anchor>',   # optional: text immediately preceding find
    after='<anchor>',    # optional: text immediately following find
    text='<new text>',   # for edit: the replacement
    where='before|after',# for insert only: where to put `text`
    match='unique',      # unique | first | all | nth
    nth=2,               # 1-indexed when match='nth'
    regex=False,         # default
    flags=['i','m','s'], # only when regex=True
    dry_run=False)       # show diff, don't write
```

Multi-edit batch (atomic):

```python
put(kind='markdown', id='notes/foo.md', edits=[
    {"op": "edit", "find": "draft",  "text": "final"},
    {"op": "insert", "find": "## Conclusion", "where": "after",
     "text": "\n\nAddendum: see appendix.\n"},
])
```

Either every edit applies and the file is rewritten once, or
nothing changes on disk.

## Resolution algorithm

Identical for every kind:

1. **Resolve the region** from `id` (whole file, block, qualname,
   or line range — the standard precis address grammar).
2. **Find candidate matches** of `find` inside the region.
3. **Anchor filter** — drop candidates whose surrounding bytes
   don't equal `before=` / `after=`. Whitespace is **strict**.
4. **Cross-region check** — kinds reject matches spanning a
   structural boundary (markdown block, docx paragraph) unless
   `allow_cross_region=True`.
5. **Apply `match` policy**:
   - `unique` (default) — exactly 1 match required.
   - `first` — earliest match wins.
   - `all` — every match.
   - `nth` — the Nth match (1-indexed).
6. **Splice → validate → format → atomic write → re-ingest.**

If any step fails, disk is untouched.

## Errors — every one is actionable

### `find` not found

```
edit failed: find='\\cite{foo2020}' not found in paper.tex~L120-180

Nearest matches in the region:
  L137  \\cite{foo2021}   (1-char diff)
  L152  \\cite{bar2020}   (3-char diff)

Try one of:
  - widen the range:   id='paper.tex'
  - update find=:      find='\\cite{foo2021}'
  - search first:      search(kind='tex', q='foo2020', scope='paper.tex')
```

### Ambiguous (`match='unique'` had ≥2 hits)

```
edit failed: find='the' has 3 matches in notes/foo.md~intro
  L42  "The fox jumps over the fence."
  L42  "...over [the] fence." ← match #2
  L43  "[The] morning was clear."

Disambiguate with:
  - anchor:   before='over ', after=' fence'   → unique
  - policy:   match='all'    → replace every occurrence
  - policy:   match='nth', nth=2
```

### Post-edit validation

```
edit failed: python AST parse error after edit
  in: precis/src/precis/cli.py
  at: line 142  (within the edited region)
  msg: SyntaxError: unexpected indent

The file on disk is unchanged. Inspect the proposed buffer with:
  put(... dry_run=True)
```

## Why anchors instead of regex

Two-anchor + literal `find` covers most cases regex covers, with
three advantages over regex:

- **No escape hazards.** Models do not reliably escape `.`, `*`,
  `(`, `\`. Literal anchors sidestep this.
- **Self-documenting.** `before='over '` reads as "after the word
  over"; `\boverthe\b` does not.
- **Bounded ambiguity.** Regex can match in surprising places;
  literal text + before/after cannot.

Regex stays available behind `regex=True` for genuine cases (version
bumps, structured renames). The handler refuses unbounded patterns
(`.*` with no anchor) with a hint.

## `dry_run=True`

Returns the unified diff that *would* be written, with all resolved
coordinates and validation results — without touching disk. Use
before any large multi-edit batch.

## Per-kind quirks

The schema is universal; the validation differs:

| Kind | Cross-region default | Validation gate | Format step |
|---|---|---|---|
| `markdown` | reject across blocks | re-parse blocks | trim trailing ws |
| `plaintext` | reject across paragraphs | none | none |
| `rmk` | reject across blocks; never edits front-matter | re-parse | trim trailing ws |
| `docx` | reject across paragraphs (run integrity) | re-parse | none |
| `tex` | reject across `\begin{}\end{}` envs | brace + env balance | none |
| `book` | delegated per child file | delegated | delegated |
| `python` | reject across top-level statements | `ast.parse` + qualname-stable | `ruff check --fix` + `ruff format` |

For kind-specific examples and recipes, see the kind's own skill
(`precis-markdown-help § Surgical edits`, `precis-python-help §
Anchored edits`, …).

## What this protocol does NOT do

- **vi-style modal commands.** No cursor state, no `:s/old/new/`,
  no counts. If a client wants that as sugar, it compiles to this
  schema before reaching the handler.
- **Cross-file edits.** One `id` per `put`. Sequence multiple calls
  (or use `quest` / `sortie`) for multi-file refactors.
- **Fuzzy `find`.** Exact match (or regex). Fuzzy lives in the
  *suggestion* leg of error messages, never in the executed match.
- **Cursor / position state across calls.** Stateless.

## See also

- `precis-files-help` — universal address grammar that `id=` uses
- `precis-markdown-help § Surgical edits` — markdown recipes
- `precis-python-help § Anchored edits` — python recipes (AST + ruff gates apply)
- `docs/edit-protocol-spec.md` — full design rationale and open questions
