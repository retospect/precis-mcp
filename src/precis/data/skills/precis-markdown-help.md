---
title: precis — read and edit local markdown files
---

# Markdown kind

Read and edit `.md` files under a configured root. Files appear as
slug-addressed refs; each paragraph / heading / code block / table /
list is one block. Block slugs are content-derived and stable across
re-ingests, so `notes--meeting~conclusion` keeps pointing at the same
chunk even after you reorder paragraphs around it.

The handler is hidden when `PRECIS_MARKDOWN_ROOT` is not set.

## Address shapes

```
markdown:notes--meeting          — file overview + heading TOC
markdown:notes--meeting~SLUG     — one block by slug
markdown:notes--meeting~3        — one block by 0-indexed position
markdown:notes--meeting/toc      — full hierarchical TOC
markdown:notes--meeting/raw      — raw source text
markdown:/                       — list every file under the root
```

File slugs encode the file's *relative* path under the root, with
`/` becoming `--` and `.md` stripped:

| File on disk                   | Slug                       |
|--------------------------------|----------------------------|
| `notes.md`                     | `notes`                    |
| `notes/meeting-2024.md`        | `notes--meeting-2024`      |
| `proposals/quarterly/Q1.md`    | `proposals--quarterly--q1` |

## Reading

```
get(kind='markdown')                                  # list every .md file
get(kind='markdown', id='notes--meeting')             # overview + headings
get(kind='markdown', id='notes--meeting/toc')         # full TOC
get(kind='markdown', id='notes--meeting~conclusion')  # one block
search(kind='markdown', q='deadline')                 # search across all
search(kind='markdown', q='deadline', scope='notes--meeting')   # scoped
```

Re-ingest is **lazy**: every `get` checks the source file's mtime.
If it differs from the last-seen value, the file is re-read,
re-hashed, and re-parsed before the response comes back. Block IDs
change but block slugs stay stable.

## Writing

Four modes are supported:

```
put(kind='markdown', id='new-file',
    text='# Title\n\nFirst paragraph.', mode='create')

put(kind='markdown', id='notes--meeting',
    text='Final summary paragraph.', mode='append')

put(kind='markdown', id='notes--meeting~old-slug',
    text='Updated content.', mode='replace')

put(kind='markdown', id='notes--meeting~obsolete-slug',
    mode='delete')
```

All writes are **atomic**: the file is rewritten via tmp + rename so
an interrupted write never leaves a half-written file. After a
successful write the handler force-re-ingests the file, so a
following `get` sees the new state immediately.

## Pre-warm with the CLI

The handler ingests lazily, but you can pre-warm a directory before
launching long-running searches:

```
precis jobs ingest-md /path/to/docs
precis jobs ingest-md --force            # force-re-ingest everything
```

`PRECIS_MARKDOWN_ROOT` is the default root if no path is passed.

## Block kinds

The parser recognises these block types:

- `heading` — ATX style only (`# H1` … `###### H6`); the slug is
  derived from the heading title.
- `paragraph` — runs of non-empty, non-special lines.
- `code` — fenced code blocks (```` ``` ```` or `~~~`); the language
  hint is captured in `block.meta.lang`.
- `table` — pipe tables with a separator row.
- `list` — ordered (`1.`, `1)`) or unordered (`-`, `*`, `+`).

Front-matter, footnotes, and embedded images stay in their home
block (no special handling). Setext headings (`===` / `---`
underlines) are **not** recognised as headings — use ATX style.

## Limits + safety

- The handler refuses to follow `..` or escape its configured root —
  any slug that resolves outside the root is rejected with `BadInput`.
- File slugs must be lowercase alphanumeric + hyphens + underscores,
  with `--` as the segment separator. `UPPERCASE` and `path/with/slashes`
  are both rejected.
- `mode='create'` refuses to overwrite an existing file.
- Track-changes / collaborative editing is **out of scope**. Phase 6
  delivers single-author atomic writes only.
