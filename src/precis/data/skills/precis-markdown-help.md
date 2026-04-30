---
id: precis-markdown-help
title: precis — read and edit markdown files
status: phase-6a
tier: 1
floor: any
applies-to: get/search/put (kind='markdown')
last-updated: 2026-04-27
---

# precis-markdown-help — `.md` block grammar and recipes

For shared concepts (address grammar, two-track addressing, multi-
root config, write modes, reverse lookups), read `precis-files-help`
first. This skill covers what's specific to `markdown`.

## Block grammar

Each block is one heading, paragraph, code fence, table, or list:

| Block kind | Recognized form | Slug shape |
|---|---|---|
| `heading` | ATX only (`# H1` … `###### H6`) | from heading title (`# Hello World` → `hello-world`) |
| `paragraph` | runs of non-empty, non-special lines | first ~5 words + 6-hex hash |
| `code` | ``` ``` ``` ``` blocks (or `~~~`) | first words of code + hash; `lang` in `block.meta` |
| `table` | pipe tables with separator row | first cell + hash |
| `list` | ordered (`1.`, `1)`) or unordered (`-`, `*`, `+`) | first item + hash |

**Setext headings** (`===` / `---` underlines) are NOT recognized.
Use ATX style.

**Front-matter, footnotes, embedded images** stay in their home
block — no special handling.

### Slug stability

Heading slugs are deterministic on the heading text. Other-kind
slugs include a 6-hex hash so two paragraphs starting with the same
words don't collide. **Slugs survive editing of unrelated blocks.**

When you `replace` a block's content, its slug changes (because the
hash depends on content). The handler stores the old slug as an
alias in `block.meta.previous_slug` for one re-ingest cycle, so a
stale reference still resolves with a hint:

```
get(kind='markdown', id='notes/meeting.md~old-slug')
→ Block 'old-slug' was renamed to 'final-summary' after edit.
  # notes/meeting.md~final-summary  (block 5, lines 42-58)
  ...
```

## Recipes

### Find a thought across all notes

```python
search(kind='markdown', q='deadline')
search(kind='markdown', q='deadline', scope='notes/meeting.md')   # one file
search(kind='markdown', q='deadline', scope='notes/')             # one dir
```

### Read a file's structure before reading content

```python
get(kind='markdown', id='notes/meeting.md/toc')
# pick a section that looks relevant from the TOC, then:
get(kind='markdown', id='notes/meeting.md~architecture')
```

### Append a thought without reading the whole file

```python
put(kind='markdown', id='notes/meeting.md',
    text='Action item: review the plan with team.',
    mode='append')
```

### Edit one section, leave the rest alone

```python
# 1. Find the slug — either by reading the TOC or by line range.
get(kind='markdown', id='notes/meeting.md/toc')

# 2. Replace just that block.
put(kind='markdown', id='notes/meeting.md~conclusion',
    text='## Final thoughts\n\nReplaced.',
    mode='replace')
```

### Edit using a line range from an external tool

```python
# `grep -n deadline notes/*.md` → file:42 …
put(kind='markdown', id='notes/meeting.md~L42-58',
    text='Updated paragraph.',
    mode='replace')
# Response gives you the new stable slug for follow-up edits.
```

### Drafting a new file

```python
put(kind='markdown', id='notes/proposal.md',
    text="""# Proposal

This is the first cut.

## Goals

- Goal one
- Goal two
""",
    mode='create')
```

### Surgical edits inside one block

For changes smaller than a whole block (one citation, one date, one
typo), use `mode='edit'` with a literal `find=` plus optional
`before=` / `after=` anchors. The schema is identical across every
file kind; the universal grammar lives in `precis-edit-protocol`.

```python
# Swap one token. Anchors disambiguate when the same word appears
# multiple times.
put(kind='markdown', id='notes--foo~intro',
    mode='edit',
    find='the', before='over ', after=' fence',
    text='a')

# Insert a paragraph before a heading without rewriting the heading.
put(kind='markdown', id='notes--foo',
    mode='insert',
    find='## Conclusion', where='before',
    text='\n## TL;DR\n\nQuick summary here.\n\n')
```

Markdown-specific quirks:

- After an edit, the block's content-derived slug may change. The
  next `get` re-ingests the file and surfaces the new slug.
- `match='unique'` is the default. With ≥2 matches you get every
  candidate's line number plus a hint to add an anchor or pick a
  policy (`match='all'`, `match='nth'`).
- The selector decides the search region: `id='notes--foo'`
  searches the whole file; `id='notes--foo~intro'` searches just
  one block.
- Pass `dry_run=True` (or `dry_run='full'`) to preview the edit
  without writing — see `precis-edit-protocol`.

### Cross-kind: link a memory to a markdown block

```python
put(kind='memory',
    text='See agenda block in last meeting note.',
    tags=['kind:reference'],
    link='markdown:notes/meeting.md~agenda')
```

The block becomes citable from anywhere.

## Limits

- ATX headings only (no setext).
- Block slugs are **content-derived** — editing content changes the
  slug. Use the rename-recovery hint when in doubt.
- Track-changes is out of scope. Single-author atomic writes only.
- Files larger than ~10 MB are rejected with a hint to chunk
  externally first.

## See also

- `precis-files-help` — shared addressing model for all file kinds
- `precis-edit-protocol` — universal anchored-edit grammar (`op='edit'` / `op='insert'`)
- `precis-python-help` — code navigation (different parser, same shape)
- `precis-relations` — typed links between refs
