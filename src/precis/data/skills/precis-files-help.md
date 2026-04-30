---
id: precis-files-help
title: precis — read and edit files (markdown, plaintext, code, …)
status: planned
tier: 1
floor: any
applies-to: cross-cutting (file-rooted kinds)
last-updated: 2026-04-28
---

> **Heads up:** none of the file kinds documented here (`markdown`,
> `plaintext`, `rmk`, `docx`, `tex`, `book`, `python`, `git`) are
> wired in this build's registry — they're queued for a later phase.
> Filtered from the default skill index until at least one of them
> ships. Read for design intent, not as a runtime recipe.

# precis-files-help — file-rooted kinds, shared concepts

This skill covers what's the **same** across every file-rooted kind:

| Kind | Files |
|---|---|
| `markdown` | `.md` |
| `plaintext` | `.txt` |
| `rmk` | `.rmk` (markdown + YAML front-matter) |
| `docx` | `.docx` (Word) |
| `tex` | `.tex` + LaTeX project files |
| `book` | multi-file project glue |
| `python` | `.py` (Python codebases) |
| `git` | any repo (language-agnostic) |

For kind-specific rules (block grammar, views, edits) see:

- `precis-markdown-help` — `.md` files
- `precis-python-help` — Python codebases

## Two tracks of addressing

Every file-rooted kind exposes the same dual-track addressing model.
Pick whichever track is cheaper for the input you have:

| Track | Form | When to use it |
|---|---|---|
| **A — coordinates** | `~L<start>-<end>` | external pointer (test failure, grep, IDE) |
| **B — headers** | `~<name>` | durable storage; cross-session references |

```python
# Track A: I have a line number from a stack trace.
get(kind='python',   id='precis/src/precis/cli.py~L142')
get(kind='markdown', id='notes/meeting.md~L42-58')

# Track B: I have a stable name from a previous response or a search hit.
get(kind='python',   id='precis/src/precis/cli.py~_cmd_serve')
get(kind='markdown', id='notes/meeting.md~conclusion')
```

**The handler always returns both forms in the response header**, so
your next call can use whichever is more durable. Track A shifts
under edits above; Track B does not.

```
# notes/meeting.md~conclusion  (block 5, lines 42-58)
…
```

Every selector-bearing response carries `~name`, `block N`, and
`lines A-B` together. No round-trip needed.

## Address grammar (one shape, all kinds)

```
[<root-alias>/]<relative-path>[~<selector>][/<view>]
```

| Field | Examples |
|---|---|
| `<root-alias>` | `notes`, `precis`, `cluster` (omit when only one root configured) |
| `<relative-path>` | `meeting.md`, `src/precis/registry.py` |
| `<selector>` | `conclusion`, `Registry.get`, `L42-58`, `3` |
| `<view>` | `toc`, `outline`, `raw`, `source`, `callgraph` |

Examples worked end to end:

```python
get(kind='markdown', id='notes/meeting.md')             # overview
get(kind='markdown', id='notes/meeting.md~conclusion')  # one block
get(kind='markdown', id='notes/meeting.md~L42-58')      # by lines
get(kind='markdown', id='notes/meeting.md/toc')         # full TOC
get(kind='markdown', id='notes/meeting.md/raw')         # source
get(kind='markdown')                                    # index of all files
```

### Absolute paths work too

If you have an absolute path (from `find`, an IDE, a stack trace),
pass it straight in. The handler matches it against configured roots
and normalizes:

```python
get(kind='markdown', id='/Users/bots/notes/meeting.md')
# → resolved to notes/meeting.md
```

## Multi-root configuration

Each kind can have multiple roots, named by alias:

```
PRECIS_MARKDOWN_ROOTS=notes:/Users/bots/notes,work:/Users/bots/work-docs
PRECIS_PYTHON_ROOTS=precis:/path/to/precis,cluster:/path/to/cluster
```

When **only one root is configured**, the alias is implicit:

```python
get(kind='markdown', id='meeting.md')          # alias inferred
get(kind='markdown', id='notes/meeting.md')    # also valid
```

When **multiple roots** are configured, the alias is required:

```python
get(kind='markdown', id='notes/meeting.md')    # required
get(kind='markdown', id='meeting.md')          # BadInput: ambiguous
```

## Read

```python
# Index — every file under every configured root for this kind.
get(kind='markdown')

# File overview — header + heading TOC preview + Next: trailer.
get(kind='markdown', id='notes/meeting.md')

# One block by name, lines, or pos.
get(kind='markdown', id='notes/meeting.md~conclusion')
get(kind='markdown', id='notes/meeting.md~L42-58')
get(kind='markdown', id='notes/meeting.md~3')

# Search — block-level lexical + semantic, fused.
search(kind='markdown', q='deadline')
search(kind='markdown', q='deadline', scope='notes/meeting.md')
```

## Write

Four modes. Available on R/W kinds (`markdown`, `plaintext`, `rmk`,
`docx`, `tex`, `book`, `python`). `git` is read-only.

`python` adds two extras on top of the shared modes: writes go
through `ast.parse` (mandatory) and `ruff check --fix` + `ruff
format` (mandatory) before the atomic write. If ruff modifies the
buffer the response says what it changed. See `precis-python-help`
for the validation table and recipes for replace-by-qualname /
replace-by-line / append / create / delete on Python files.

```python
# Create a new file.
put(kind='markdown', id='notes/new-file.md',
    text='# Title\n\nFirst paragraph.', mode='create')

# Append paragraph(s) to an existing file.
put(kind='markdown', id='notes/meeting.md',
    text='Final thought.', mode='append')

# Replace one region — by slug, lines, or pos. Same op, three forms.
put(kind='markdown', id='notes/meeting.md~conclusion',
    text='Updated content.', mode='replace')
put(kind='markdown', id='notes/meeting.md~L42-58',
    text='Updated content.', mode='replace')

# Delete one region — same selector forms.
put(kind='markdown', id='notes/meeting.md~conclusion',
    mode='delete')
```

All writes are **atomic** (tmpfile + rename). After every write the
file is re-ingested so the next `get` sees the new state. The
response includes the **resolved stable name** of what was edited:

```
replaced block 'conclusion' (now lines 42-62) in notes/meeting.md
```

If you addressed by line range, the response gives you the slug.
If you addressed by slug, the response gives you the line range.
Round-trip is free.

## Anchored edits

> **Status:** the `edit` and `insert` ops are a proposal — see
> `precis-edit-protocol`. Until they ship, use `mode='replace'`
> with the existing selector grammar.

The four-mode surface (`create` / `append` / `replace` / `delete`)
gains two more for sub-region edits:

| Op | Region | Behavior |
|---|---|---|
| `edit` | optional (defaults to whole file) | find literal text and replace it — anchored, validated |
| `insert` | optional | insert text immediately before/after a found anchor |

The grammar is **identical across every R/W file kind**; per-kind
quirks (cross-region rules, validation gates) live in each kind's
skill. The full universal grammar lives in `precis-edit-protocol`.

```python
# Surgical replacement: change one token, leave everything else alone.
# Content selects ('the'); anchors disambiguate; line range is just a bound.
put(kind='markdown', id='notes/foo.md~intro',
    op='edit',
    find='the', before='over ', after=' fence',
    text='a',
    match='unique')

# Insert a paragraph adjacent to an anchor, no rewriting.
put(kind='markdown', id='notes/foo.md',
    op='insert',
    find='## Conclusion', where='before',
    text='\n## TL;DR\n\nQuick summary.\n\n')

# Atomic batch — both edits succeed or neither applies.
put(kind='python', id='precis/src/precis/cli.py', edits=[
    {"op": "edit", "find": "from .old import X",
     "text": "from .new import X"},
    {"op": "edit", "find": "X.legacy_method(",
     "text": "X.method(", "match": "all"},
])

# Preview a change without writing.
put(kind='python', id='precis/src/precis/cli.py',
    op='edit', find='old', text='new', match='all',
    dry_run=True)
```

The rule of thumb: **content selects, range bounds.** Use the `id`
selector to narrow where the search runs (one block, one function,
one line range); use literal `find=` plus optional `before=` /
`after=` to choose which match to edit. `match='unique'` (the
default) means "must be exactly one match — else error with every
candidate listed."

## Reverse lookups

The handler maintains the bijection between Track A and Track B.
Every response includes both forms. You never need to compute a
mapping by hand.

| You have | You can call |
|---|---|
| line number | `~L<n>` (resolves to enclosing block + name) |
| absolute path | full path (resolves to canonical) |
| stale slug after replace | `~<old-slug>` (handler recovers via rename map) |
| search hit | the hit's `id` field (already canonical) |

When a slug no longer exists (file edited externally, content
changed), you get `NotFound` with `options=` listing the five
nearest matches.

## Lazy re-ingest

Every `get` checks the file's mtime. If unchanged, cached blocks
are served. If changed, the handler re-hashes + re-parses before
responding. Stable names survive re-ingest.

You do not have to ingest manually. The first `get` or `search`
that touches a file pulls it through the parser; subsequent calls
hit the cached blocks.

## Limits + safety

- Slugs are validated against the configured root(s); `..` and
  out-of-root paths are rejected with `BadInput`.
- `mode='create'` refuses to overwrite an existing file.
- Track-changes / multi-author concurrent editing is **out of scope**.
- Binary files are not read; only text formats per the per-kind
  parser.

## See also

- `precis-overview` — verbs and kinds
- `precis-edit-protocol` — universal anchored-edit grammar (`op='edit'` / `op='insert'`)
- `precis-markdown-help` — `.md` block grammar and recipes
- `precis-python-help` — Python codebase navigation
- `precis-relations` — typed links between refs (file ↔ paper ↔ memory)
- `precis-navigation` — recipes for common cross-kind flows
