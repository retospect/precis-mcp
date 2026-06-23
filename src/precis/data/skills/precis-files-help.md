---
id: precis-files-help
title: precis — read and edit files (markdown, plaintext, tex, python)
summary: shared file-kind conventions — roots, workspace tag scoping, two-track addressing, write modes
applies-to: cross-cutting (file-rooted kinds)
status: active
---

# precis-files-help — file-rooted kinds, shared concepts

This skill covers what's the same across every file-rooted kind.
Per-kind specifics live in `precis-<kind>-help`.

| Kind | Files | Env var |
|---|---|---|
| `markdown` | `.md` | `PRECIS_ROOT` |
| `plaintext` | `.txt`, `.log` | `PRECIS_ROOT` |
| `tex` | `.tex` | `PRECIS_ROOT` |
| `python` | `.py` | `PRECIS_PYTHON_ROOTS` |

`PRECIS_ROOT` is the writable boundary for the prose-file trio.
Paths outside it are rejected (`BadInput: path traversal not allowed`).
The LLM sees the root as `./` — never the absolute filesystem path.

## How do I scope searches to my working directory?
## Filter results to files in PRECIS_ROOT only
## How do I exclude papers / web caches from my search?

Every file ingested via `markdown` / `plaintext` / `tex` carries a
system-applied `workspace` tag. Use it to scope:

```python
search(q='kinetics', tags=['workspace'])                # working dir only
search(kind='markdown', q='meeting', tags=['workspace'])
```

The tag is system-applied (`set_by='system'`), idempotent on
re-ingest, and absent on out-of-root refs (papers, web, patents,
memories, todos).

## How do I pre-warm the file store?
## Ingest every file before I start searching
## How do I avoid the lazy-on-first-touch delay?

Handlers ingest lazily — a `.md` / `.txt` / `.tex` file enters the
DB only when the LLM opens it. On a fresh `PRECIS_ROOT`, search
returns nothing until each file is touched. Pre-warm with:

```bash
precis jobs ingest                           # all prose kinds under $PRECIS_ROOT
precis jobs ingest /path/to/root             # override root
precis jobs ingest --kinds tex,plaintext     # scope to some kinds
precis jobs ingest --force                   # re-embed even unchanged files
precis jobs ingest && precis serve           # launcher-script prefix
```

The walker is mtime-gated — unchanged files are skipped.

## How do I address a file or one of its parts?
## What's the address grammar for files?
## How do I point at a specific block, line range, or view?

```
[<root-alias>/]<relative-path>[~<selector>][/<view>]
```

| Field | Examples |
|---|---|
| `<root-alias>` | `notes`, `precis`, `cluster` (omit when only one root configured) |
| `<relative-path>` | `meeting.md`, `src/precis/registry.py` |
| `<selector>` | `conclusion`, `Registry.get`, `L42-58`, `3` |
| `<view>` | `toc`, `outline`, `raw`, `source`, `callgraph` |

```python
get(kind='markdown', id='notes/meeting.md')             # overview
get(kind='markdown', id='notes/meeting.md~conclusion')  # one block by name
get(kind='markdown', id='notes/meeting.md~L42-58')      # by line range
get(kind='markdown', id='notes/meeting.md~3')           # by block pos (output shows the handle mc<id>; get(id='mc<id>') works too)
get(kind='markdown', id='notes/meeting.md/toc')         # full TOC
get(kind='markdown', id='notes/meeting.md/raw')         # source
get(kind='markdown', id='/Users/bots/notes/meeting.md') # absolute path also works
```

## Coordinate-form vs name-form addressing
## When should I use ~L<n>-<m> vs ~<name>?
## Two tracks: line-numbers vs durable slugs

| Track | Form | Use when |
|---|---|---|
| Coordinates | `~L<start>-<end>` | external pointer (stack trace, grep, IDE) |
| Names | `~<slug>` | durable reference; survives edits above |

Every selector-bearing response carries both forms together (block N,
lines A-B, slug). No round-trip needed:

```text
# notes/meeting.md~conclusion  (block 5, lines 42-58)
…
```

Track A (line numbers) shifts under edits above; track B (names)
does not.

## How do I read a file?
## Open a file and see its contents

```python
get(kind='markdown')                                    # index of all files
get(kind='markdown', id='notes/meeting.md')             # overview + TOC preview
get(kind='markdown', id='notes/meeting.md~conclusion')  # one block
get(kind='markdown', id='notes/meeting.md~L42-58')      # by lines
search(kind='markdown', q='deadline')                   # all files
search(kind='markdown', q='deadline', scope='notes/meeting.md')
```

## How do I create or rewrite a file?
## Write to a file (create / append / replace / delete)
## What modes does write support?

Available on R/W kinds (`markdown`, `plaintext`, `python`).
`python` validates writes via `ast.parse` + `ruff check --fix` +
`ruff format` before the atomic write (see `precis-python-help`).
`plaintext` writes verbatim after UTF-8 encode check.

```python
# Create a new file.
put(kind='markdown', id='notes/new-file.md',
    text='# Title\n\nFirst paragraph.', mode='create')

# Append to the end.
edit(kind='markdown', id='notes/meeting.md',
    text='Final thought.', mode='append')

# Replace one region — by slug, lines, or pos.
edit(kind='markdown', id='notes/meeting.md~note-on-reagents',
    text='Rewritten paragraph.', mode='replace')
edit(kind='markdown', id='notes/meeting.md~L42-58',
    text='Rewritten paragraph.', mode='replace')

# Delete a region.
delete(kind='markdown', id='notes/meeting.md~note-on-reagents')

# Whole-file delete via edit (the delete verb only handles regions
# with a selector in id=). Clear the file in one call:
edit(kind='markdown', id='notes/meeting.md', mode='replace', text='')
```

A block is one paragraph, one heading, one fenced code, one table, or
one list — whichever unit the parser found. `~<heading-slug>` names
the heading paragraph, not the section below it. To replace a whole
section, delete every block in the range and append new content, or
use `mode='find-replace'`.

Every write response names slug, block pos, block slug, and line
range together — chained edits don't need a `/toc` round-trip.

## How do I make a surgical edit?
## Find-replace within a block or file
## Anchor an insert before/after a specific line

Two sub-region modes for surgical changes:

| Mode | Behavior |
|---|---|
| `find-replace` | find literal text and replace it — anchored, validated |
| `insert` | insert text immediately before/after a found anchor |

```python
# Surgical replacement: content selects, anchors disambiguate.
edit(kind='markdown', id='notes--foo~intro',
     mode='find-replace',
     find='the', before='over ', after=' fence',
     text='a', match='unique')

# Insert adjacent to an anchor.
edit(kind='markdown', id='notes--foo',
     mode='insert',
     find='## Conclusion', where='before',
     text='\n## TL;DR\n\nQuick summary.\n\n')

# Bulk rename across one file (Python's AST + ruff gates apply).
edit(kind='python', id='r/src/precis/cli.py',
     mode='find-replace',
     find='X.legacy_method(', text='X.method(',
     match='all')

# Delete a matched span without rewriting the block.
edit(kind='markdown', id='notes--foo~intro',
     mode='find-replace',
     find='(draft) ', text='')
```

**Content selects, range bounds.** `id`-selector narrows where the
search runs (one block, one function, one line range); `find=` +
optional `before=` / `after=` chooses which match. `match='unique'`
(default) errors with every candidate listed when multiple match;
`match='all'` rewrites all; `match='first'` takes the first.

Full grammar in `precis-edit-help`.

## What if my slug went stale after an edit?
## A block's name changed — how do I reach it?

The handler maintains the bijection between line-form and name-form;
every response includes both. When a slug no longer exists (file
edited externally), `NotFound` carries `options=` listing the five
nearest matches.

| You have | You can call |
|---|---|
| line number | `~L<n>` resolves to the enclosing block + its name |
| absolute path | resolves to canonical |
| stale slug | recovered via rename map; or use `options=` from the NotFound |
| search hit | the hit's `id` field is already canonical |

## See also

```python
get(kind='skill', id='precis-overview')          # verbs and kinds
get(kind='skill', id='precis-edit-help')         # universal find-replace + insert grammar
get(kind='skill', id='precis-markdown-help')     # .md block grammar and recipes
get(kind='skill', id='precis-plaintext-help')    # .txt / .log specifics
get(kind='skill', id='precis-tex-help')          # .tex section-aware blocks
get(kind='skill', id='precis-python-help')       # Python navigation + AST-gated edits
get(kind='skill', id='precis-relations')         # typed links (file ↔ paper ↔ memory)
```
