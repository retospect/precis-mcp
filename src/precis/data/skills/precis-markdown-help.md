---
id: precis-markdown-help
title: precis — read and edit markdown files
summary: markdown files — block grammar, available views, slug stability, line and name selectors
applies-to: get/search/put/edit/delete (kind='markdown')
status: active
---

# precis-markdown-help — `.md` block grammar and recipes

Markdown files under `PRECIS_ROOT`. Read `precis-files-help` for the
shared address grammar, write modes, and two-track addressing. This
skill covers markdown-specific behaviour: block kinds, available
views, and slug stability under edits.

## What does a markdown id look like?
## How do I address a markdown file or block?
## Path form vs slug form — which one do I use?

Either form works; the handler accepts both.

```python
get(kind='markdown', id='notes/meeting.md')        # path form
get(kind='markdown', id='notes--meeting')          # slug form ('/' ↔ '--')
get(kind='markdown', id='notes/meeting.md~L42-58') # line range (1-indexed inclusive)
get(kind='markdown', id='notes/meeting.md~conclusion')  # name selector
get(kind='markdown', id='notes/meeting.md~3')      # by block pos (output shows the handle mc<id>; get(id='mc<id>') works too)
get(kind='markdown', id='/Users/me/notes/meeting.md')   # absolute path also accepted
```

`/` and `--` are interchangeable in the file portion. `..` parent
traversal is rejected. Full address grammar lives in
`precis-files-help`.

## Block grammar

Each block is one heading, paragraph, code fence, table, or list:

| Block kind | Recognised form | Slug shape |
|---|---|---|
| `heading` | ATX only (`# H1` … `###### H6`) | from heading text (`# Hello World` → `hello-world`) |
| `paragraph` | runs of non-empty, non-special lines | first ~5 words + 6-hex hash |
| `code` | fenced (``` ``` ``` ``` or `~~~`) | first words of code + hash; `lang` in `block.meta` |
| `table` | pipe tables with separator row | first cell + hash |
| `list` | ordered (`1.`, `1)`) or unordered (`-`, `*`, `+`) | first item + hash |

Setext headings (`===` / `---` underlines) aren't parsed — use ATX.
Front-matter, footnotes, and inline images stay in their home block.

Heading slugs are deterministic on the heading text. Other-kind slugs
include a 6-hex content hash, so editing a block changes its slug.
The handler stores the old slug as an alias for one re-ingest cycle;
stale references resolve with a rename hint.

```text
get(kind='markdown', id='notes/meeting.md~old-intro')
→ Block 'old-intro' was renamed to 'final-intro' after edit.
  # notes/meeting.md~final-intro  (block 5, lines 42-58)
  ...
```

## See the structure of a file before reading it
## Get the table of contents for a markdown file
## What's in this note?

```python
get(kind='markdown', id='<slug>', view='toc')   # heading-driven TOC
get(kind='markdown', id='<slug>/toc')           # path form is equivalent
get(kind='markdown', id='<slug>/raw')           # untouched source text
get(kind='markdown', id='<slug>')               # overview with TOC preview
```

Views: `toc`, `raw`. The overview already shows a heading-TOC preview;
`/toc` gives the full table. `/raw` returns the file bytes verbatim.

## Edit one section, leave the rest alone
## Replace a single block by name or line range
## How do I rewrite just one paragraph?

```python
edit(kind='markdown', id='notes/meeting.md~conclusion',
     text='## Final thoughts\n\nReplaced.', mode='replace')

edit(kind='markdown', id='notes/meeting.md~L42-58',   # from grep -n
     text='Updated paragraph.', mode='replace')

edit(kind='markdown', id='notes/meeting.md',
     text='Action item: review the plan.', mode='append')
```

Block kinds (paragraph / heading / code / table / list) are addressed
the same way — the parser tells you what unit you hit; the editor
rewrites that unit. The response carries the new slug, block pos,
and line range together so chained edits skip the `/toc` round-trip.

`~<heading-slug>` names the heading paragraph, not the section
beneath it. To replace a whole section, delete each block in the
range and append new content, or use `mode='find-replace'` on the
file with the heading as an anchor.

## Make a surgical change inside a block
## Find-replace a token, citation, or date
## How do I change one word without rewriting the paragraph?

```python
edit(kind='markdown', id='notes--foo~intro',
     mode='find-replace',
     find='the', before='over ', after=' fence',
     text='a')

edit(kind='markdown', id='notes--foo',
     mode='insert',
     find='## Conclusion', where='before',
     text='\n## TL;DR\n\nQuick summary.\n\n')

edit(kind='markdown', id='notes--foo~intro',
     mode='find-replace',
     find='(draft) ', text='')   # empty text = delete the span
```

Selector bounds the search region: `id='notes--foo'` searches the
whole file; `id='notes--foo~intro'` searches just that block. Full
find-replace + insert grammar lives in `precis-edit-help`.

## Drafting a new file

```python
put(kind='markdown', id='notes/proposal.md',
    text='# Proposal\n\nFirst cut.\n\n## Goals\n\n- One\n- Two\n',
    mode='create')
```

## Limits

- ATX headings only.
- Block slugs are content-derived — editing content changes the
  slug; the rename alias survives one re-ingest cycle.
- Files larger than ~10 MB are rejected with a hint to chunk
  externally first.

## See also

```python
get(kind='skill', id='precis-files-help')      # shared address grammar, write modes
get(kind='skill', id='precis-edit-help')       # find-replace + insert grammar
get(kind='skill', id='precis-plaintext-help')  # .txt / .log — no block grammar
get(kind='skill', id='precis-tex-help')        # .tex section-aware blocks
get(kind='skill', id='precis-python-help')     # code navigation, AST-gated edits
get(kind='skill', id='precis-relations')       # typed links between files and refs
```
