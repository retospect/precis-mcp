---
id: precis-files-help
title: precis — read and edit files (markdown, plaintext, code, …)
status: active
tier: 1
floor: any
applies-to: cross-cutting (file-rooted kinds)
last-updated: 2026-05-01
---

> **Status:** `markdown`, `plaintext`, `tex`, and `python` ship today
> (read + write, including the `mode='edit'` / `mode='insert'`
> surface). The first three share **one** env var — `PRECIS_ROOT`
> — a single directory under which all `.md`, `.txt`, `.log`, and
> `.tex` files live. `python` has its own multi-repo `PRECIS_PYTHON_ROOTS`
> (alias-prefixed paths). A build that doesn't set the relevant var
> won't register the corresponding kind(s). Use
> `get(kind='skill', id='precis-help')` to see which kinds are live
> in the server you're talking to.

# precis-files-help — file-rooted kinds, shared concepts

This skill covers what's the **same** across every file-rooted kind:

| Kind | Files | Env var |
|---|---|---|
| `markdown` | `.md` | `PRECIS_ROOT` (shared) |
| `plaintext` | `.txt`, `.log` | `PRECIS_ROOT` (shared) |
| `tex` | `.tex` | `PRECIS_ROOT` (shared) |
| `python` | `.py` (Python codebases) | `PRECIS_PYTHON_ROOTS` |

`PRECIS_ROOT` is the **single writable boundary** for the prose-file
trio. Every read and write goes through `Path.resolve()` followed by
`Path.relative_to(PRECIS_ROOT)` — a check that rejects `../` traversal
**and** symlink escapes (since `resolve()` follows symlinks). Files
from outside this directory are unreachable; writes outside it are
rejected with `BadInput: path traversal not allowed`.

The LLM sees `PRECIS_ROOT` as `./` — it doesn't know (and isn't told)
the absolute filesystem path of the root. Every error message and
index listing names the symbolic `PRECIS_ROOT`, never the absolute
path.

For kind-specific rules (block grammar, views, edits) see:

- `precis-markdown-help` — `.md` files
- `precis-plaintext-help` — `.txt` / `.log` files
- `precis-tex-help` — `.tex` files (section-aware blocks + recursive `/toc`)
- `precis-python-help` — Python codebases

## The `workspace` flag (auto-applied)

Every ref ingested via the prose-file handlers (`markdown`,
`plaintext`, `tex`) is stamped with the **`workspace` flag tag** on
ingest. The LLM uses it to scope searches to its working directory:

```python
# Everything in my working directory, no external refs.
search(q='kinetics',       tags=['workspace'])

# Restricted to a single file-kind.
search(kind='markdown', q='meeting', tags=['workspace'])
```

The tag is applied with `set_by='system'` (not `agent`) so audit
queries can distinguish the machine-applied scope tag from tags the
LLM or the user added themselves. It's idempotent — re-ingesting
the same file doesn't duplicate the tag.

Refs **outside** `PRECIS_ROOT` (papers, web caches, patents,
memories, todos, etc.) are not stamped, so `tags=['workspace']` is
an effective filter for "stuff in my working directory right now."

## Pre-warming the store (`precis jobs ingest`)

By default, handlers ingest files **lazily** — a `.md` / `.txt` /
`.tex` file doesn't enter the DB until the LLM opens it via
`get()` or touches it via `search(scope=...)`. That means on a
freshly-mounted `PRECIS_ROOT`, `search(kind='tex', q='…')` returns
nothing until each file has been individually read.

To pre-warm the store, run the CLI before starting the MCP server:

```bash
precis jobs ingest                           # all three prose kinds under $PRECIS_ROOT
precis jobs ingest /path/to/root             # override root
precis jobs ingest --kinds tex,plaintext     # scope to some kinds
precis jobs ingest --force                   # re-embed even unchanged files

precis jobs ingest && precis serve           # launcher-script prefix
```

The walker is **mtime-gated** — unchanged files are skipped (cheap
stat call). Run it as often as you like.

Output:

```
  ok    [md       ] notes--meeting  (8 blocks)
  ok    [tex      ] chapters--intro  (12 blocks)
  ok    [plaintext] logs--run-2026-05  (47 blocks)
ingest: total ingested=3  skipped=0  failed=0
  per-kind: md=1/1, plaintext=1/1, tex=1/1
```

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

## Root configuration

The prose-file trio (`markdown`, `plaintext`, `tex`) shares a single
root:

```
PRECIS_ROOT=/Users/bots/notes
```

All three kinds walk the same tree and filter by extension. A file
laid out as `chapters/intro.tex` becomes `tex:chapters--intro`;
`meeting.md` becomes `markdown:meeting`; `log/2026-05.txt` becomes
`plaintext:log--2026-05`. The directory layout is yours to choose —
the handlers don't care whether you organise by kind, by topic, or
flat.

The Python kind is different: code lives in real repos with their
own roots, so `PRECIS_PYTHON_ROOTS` is alias-prefixed and supports
multiple entries:

```
PRECIS_PYTHON_ROOTS=precis:/path/to/precis,cluster:/path/to/cluster
```

The alias becomes the prefix in addresses (`precis::pkg.mod:func`).

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

Four modes. Available on R/W kinds (`markdown`, `plaintext`, `python`).

`python` adds two extras on top of the shared modes: writes go
through `ast.parse` (mandatory) and `ruff check --fix` + `ruff
format` (mandatory) before the atomic write. If ruff modifies the
buffer the response says what it changed. See `precis-python-help`
for the validation table and recipes for replace-by-qualname /
replace-by-line / append / create / delete on Python files.

`plaintext` has no validation gate — writes go to disk verbatim
after a UTF-8 encode check. Block grammar is "one paragraph per
blank-line-separated run".

```python
# Create a new file.
put(kind='markdown', id='notes/new-file.md',
    text='# Title\n\nFirst paragraph.', mode='create')

# Append paragraph(s) to an existing file.
edit(kind='markdown', id='notes/meeting.md',
    text='Final thought.', mode='append')

# Replace one region — by slug, lines, or pos. Same op, three forms.
edit(kind='markdown', id='notes/meeting.md~conclusion',
    text='Updated content.', mode='replace')
edit(kind='markdown', id='notes/meeting.md~L42-58',
    text='Updated content.', mode='replace')

# Delete one region — same selector forms.
delete(kind='markdown', id='notes/meeting.md~conclusion')
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

The four-mode surface (`create` / `append` / `replace` / `delete`)
is joined by two sub-region modes for surgical changes:

| Mode | Region | Behavior |
|---|---|---|
| `edit` | optional (defaults to whole file) | find literal text and replace it — anchored, validated |
| `insert` | optional | insert text immediately before/after a found anchor |

The grammar is **identical across every R/W file kind**; per-kind
quirks (validation gates, format steps) live in each kind's
skill. The full universal grammar lives in `precis-edit-protocol`.

Ships today for `markdown`, `plaintext`, and `python`.

```python
# Surgical replacement: change one token, leave everything else alone.
# Content selects ('the'); anchors disambiguate; the selector bounds the search.
edit(kind='markdown', id='notes--foo~intro',
    mode='find-replace',
    find='the', before='over ', after=' fence',
    text='a',
    match='unique')

# Insert a paragraph adjacent to an anchor, no rewriting.
edit(kind='markdown', id='notes--foo',
    mode='insert',
    find='## Conclusion', where='before',
    text='\n## TL;DR\n\nQuick summary.\n\n')

# Bulk rename across one file. Python's AST + ruff gates apply.
edit(kind='python', id='r/src/precis/cli.py',
    mode='find-replace',
    find='X.legacy_method(',
    text='X.method(',
    match='all')
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
