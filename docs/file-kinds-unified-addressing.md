# Unified addressing for file-rooted kinds

> Status: **design spec**, drafted after phase 6a (markdown) shipped
> and before phase 6b (plaintext, rmk) and `pycode` (Python codebase
> navigator) start. The goal is to lock down one address grammar
> that every kind backed by an on-disk file (or directory of files)
> agrees on, so the agent has a single mental model.

## Why

By the end of phase 6 + pycode + git we will have at least eight
file-rooted kinds:

| Kind | Backing | R/W | TOC unit | Sub-file unit |
|---|---|---|---|---|
| `markdown` | `.md` files | yes | headings | block (paragraph/code/list/table) |
| `plaintext` | `.txt` files | yes | n/a | block (paragraph) |
| `rmk` | RMK note files | yes | front-matter + headings | block |
| `docx` | Word `.docx` | yes | headings | paragraph (para id) |
| `tex` | LaTeX `.tex` + project | yes | sections | block + label |
| `book` | multi-file project | yes | chapter graph | per-file delegated |
| `pycode` | `.py` repo | read-only (v1) | class/method tree | symbol (qualname) or line range |
| `git` | any repo | read-only | branches/refs | commit/file/line |

Each was designed in isolation; their addressing schemes drifted:

- markdown encodes paths as `notes--meeting` (slug-safe `--`)
- pycode keeps paths as `precis/src/precis/registry.py` (with `/`)
- pycode uses `::` for symbols; markdown uses `~slug` for blocks
- markdown uses `~A..B` for ranges (paper-style); pycode spec uses `~A-B`
- markdown has one implicit root; pycode has many explicit ones

The agent has to remember three grammars to do the same thing across
three kinds. That's wasted context for no gain. This document picks
one grammar.

## Design principles

1. **Path format is `/`-native.** Pretrained models read `path/to/file.py`
   fluently; `path--to--file-py` is alien. Use the natural form.
2. **Stable names beat coordinates.** Block slugs (markdown) and
   qualnames (pycode) survive edits; line numbers don't. Line numbers
   are accepted as *input* (pointers) but stable names are what the
   handler returns and stores.
3. **One grammar across kinds.** The same `id` shape works for any
   file-rooted kind; only the *meaning* of the selector changes.
4. **Single-root case stays terse.** When only one root is configured
   for a kind, its alias may be omitted from the id.
5. **Read and write share a selector vocabulary.** Whatever points to
   a region for `get` also points to the same region for `put`.

## Address grammar

```
[<root-alias>/]<relative-path>[~<selector>][/<view>]
```

Five-token fields, all optional except `<relative-path>` (which is
empty for the index view):

| Field | Optional? | Examples |
|---|---|---|
| `<root-alias>` | when only one root configured | `notes`, `precis`, `cluster` |
| `<relative-path>` | empty for index view | `meeting.md`, `src/precis/registry.py` |
| `<selector>` | yes (default = whole file) | `conclusion`, `Registry.get`, `L42-58`, `3` |
| `<view>` | yes (default = overview) | `toc`, `outline`, `raw`, `source`, `callgraph` |

### Examples

```python
# markdown
get(kind='markdown')                                    # index of all files
get(kind='markdown', id='notes/meeting.md')             # file overview
get(kind='markdown', id='notes/meeting.md~conclusion')  # block by slug
get(kind='markdown', id='notes/meeting.md~L42-58')      # blocks at lines 42-58
get(kind='markdown', id='notes/meeting.md~3')           # block at pos 3
get(kind='markdown', id='notes/meeting.md/toc')         # TOC view
get(kind='markdown', id='notes/meeting.md/raw')         # source view

# pycode
get(kind='pycode', id='precis/src/precis/registry.py')             # file overview
get(kind='pycode', id='precis/src/precis/registry.py~Registry')    # symbol (local)
get(kind='pycode', id='precis/src/precis/registry.py~Registry.get')# nested symbol
get(kind='pycode', id='precis/src/precis/registry.py~L42-100')     # line range
get(kind='pycode', id='precis/src/precis/registry.py/outline')     # outline view
get(kind='pycode', id='precis/src/precis/registry.py/callgraph')   # call graph
```

### Single-root shortcut (token efficiency)

When a kind has only one root configured, its alias may be omitted:

```python
# config: PRECIS_MARKDOWN_ROOTS=notes:/Users/bots/notes  (one root)
get(kind='markdown', id='meeting.md')          # alias inferred → "notes"
get(kind='markdown', id='notes/meeting.md')    # explicit, also valid

# config: PRECIS_MARKDOWN_ROOTS=notes:/path,work:/other  (two roots)
get(kind='markdown', id='meeting.md')          # ERROR: ambiguous, hint with options
get(kind='markdown', id='notes/meeting.md')    # required
```

### Why `/` for path AND view

Same character is used for two roles: path segments and the trailing
view token. The view is whatever follows the *last* `/` after the
selector. Concretely the parser reads the id right-to-left:

1. **View** — if the trailing segment is a known view name for this
   kind (`toc`, `raw`, `outline`, `source`, `callgraph`, …), it's
   the view.
2. **Selector** — what's after the last `~` (and before the view).
3. **Path** — everything else.
4. **Root alias** — leading segment if it matches a configured alias.

This is unambiguous because:
- Views are a closed enum per kind.
- `~` doesn't appear in paths or aliases.
- File paths can't end in a known view name without an extension.

Edge case: a file literally named `toc.md` under a markdown root.
`notes/toc.md` parses as `path=notes/toc.md, view=overview` because
the path-vs-view check requires the trailing segment to come *after*
a path segment that ends in a recognized file extension OR after a
selector. If the path itself ends in `.md`, no view is consumed.

## Selector parsing

The `<selector>` field after `~` is a discriminated union, parsed in
priority order:

| # | Pattern | Meaning | Applies to |
|---|---|---|---|
| 1 | `^L\d+(-\d+)?$` | line range (1-indexed, inclusive) | all kinds |
| 2 | `^[a-z][a-z0-9_-]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$` | dotted symbol name | pycode (TODO: docx/tex labels) |
| 3 | `^\d+$` | block position (0-indexed) | markdown, plaintext, rmk |
| 4 | anything else | named slug | markdown (block slug), pycode (local symbol name), docx (paragraph id) |

The handler resolves the selector to a stable `(name, line_start,
line_end)` tuple. **The response always quotes the resolved stable
name** so the agent can re-address durably:

```
get(kind='markdown', id='notes/meeting.md~L42-58')
→ "Resolved L42-58 to block 'agenda'."
  ## agenda
  …
  Next:
    get(kind='markdown', id='notes/meeting.md~agenda')   # durable form
```

This pattern matters most for `replace`: an edit at `~L42-58` that
shifts later lines by ±N is fine because the response gives back the
stable slug for the *next* edit.

### Selector ambiguity rules

- A bare integer is **always** a position (markdown) or rejected with
  hint to use `L<n>` (pycode). Lines need the `L` prefix.
- A dotted name is **always** a symbol (pycode); markdown rejects
  with `BadInput` because block slugs don't contain dots.
- A name that could be a symbol *or* a slug (no dot, no leading L)
  is resolved per-kind: markdown → block slug; pycode → local
  symbol name (must be unique within the file, else
  `BadInput("ambiguous: matches X, Y; use full qualname")`).

## Views

Views are the *render mode* for the resolved selector. Per kind:

### Shared across all file kinds

- `raw` — the full source text of the file (or, with a selector, the
  text of the resolved region only).
- `source` — alias for `raw`. Pycode prefers `source`; markdown
  prefers `raw`. Both work everywhere.

### Markdown / plaintext / rmk / docx / tex / book

- (default) — file overview: header + heading TOC preview + `Next:`
  trailer.
- `toc` — full hierarchical TOC.
- `raw` — full source.

### Pycode (read-only v1)

- (default) — file overview: imports + class/function tree.
- `outline` — same as default but verbose (signatures + docstring
  heads + decorators + raises).
- `source` — full source for the resolved region.
- `callgraph` — entry-rooted static call tree (requires `entry=`).
- `runtrace` — dynamic call trace (gated by `PYCODE_ALLOW_EXEC=1`).
- `imports` — flat dependency map.
- `symbols` — flat symbol list (paginated).
- `blame`, `log`, `churn`, `owners`, `diff` — git overlays.

## TOC row format (per-symbol/per-block)

The unified TOC row carries four fields. Only fields that apply to a
kind appear:

| Field | Markdown | Pycode |
|---|---|---|
| **Stable name** | block slug | symbol qualname |
| **Position** | line number (`L42`) | line number |
| **Headline** | heading text (for headings); first 80 chars (for paragraphs) | signature `def f(x: int) -> str` |
| **Summary** | n/a | first line of docstring, ≤80 chars |

Pycode example:

```
class Registry                                src/precis/registry.py:103
  "Resolves a `kind=` string to a handler instance."
  ├── __init__(self, handlers)                L111
  ├── get(self, kind: str) -> Handler         L120
  │     "Look up a handler by kind name."  raises NotFound
  └── kinds(self) -> list[str]                L130
        "Return every registered kind name."
```

Markdown example (already shipped, unchanged):

```
# notes/meeting.md — TOC (8 blocks, 4 sections)

  ~0..7 (8)  ■ Welcome
    ~2..3 (2)  Features
    ~4..5 (2)  Architecture
    ~6..7 (2)  Closing thoughts
```

### What NOT to put in TOC rows

- Body line counts ("47 lines"). Agents don't navigate by length.
- Per-symbol call counts. That's the `callgraph` view's job.
- Argument default values. Noise; show in `outline`/`source`.
- Type-checked import resolution. Slow + uncertain; defer to jedi
  upgrade path.

## Write surface

Mutation is **per-kind opt-in**. R/W kinds (markdown, plaintext, rmk,
docx, tex, book) declare `supports_put=True` and accept four modes:

| Mode | id shape | text param | Effect |
|---|---|---|---|
| `create` | `<alias>/<path>` | required | create new file (fails if exists) |
| `append` | `<alias>/<path>` | required | append paragraph(s) at end |
| `replace` | `<alias>/<path>~<selector>` | required | swap text for resolved region |
| `delete` | `<alias>/<path>~<selector>` | (none) | drop resolved region |

The unified key insight: **`replace` accepts any selector form**.

```python
# replace by block slug (durable, preferred)
put(kind='markdown', id='notes/meeting.md~conclusion',
    text='## Final thoughts\n\nNew content.', mode='replace')

# replace by line range (when slug isn't known)
put(kind='markdown', id='notes/meeting.md~L42-58',
    text='Replacement text.', mode='replace')

# replace by pos (when iterating)
put(kind='markdown', id='notes/meeting.md~3',
    text='Updated paragraph.', mode='replace')
```

All three forms resolve to the same `(line_start, line_end)`; the
splice + atomic write + re-ingest path is identical.

### Stable name in response

After every write the response includes the stable name of what was
edited:

```
replaced block 'conclusion' (lines 42-58) in notes/meeting.md
```

So an agent that started with a line range knows the durable address
for follow-up edits.

### Pycode mutation (deferred, designed-in)

Pycode is read-only in v1 per its spec. But the unified base will
have all the write plumbing — adding `put(mode='replace',
id='precis/src/precis/registry.py~Registry.get', text='…')` later
is a subclass opt-in, not a re-architecture. The *policy* question
(AST-validate? run tests?) stays open; the *mechanism* is ready.

## Multi-root configuration

Replace single-root env vars with dict-shaped ones:

```env
# Old (phase 6a)
PRECIS_MARKDOWN_ROOT=/Users/bots/notes

# New (unified)
PRECIS_MARKDOWN_ROOTS=notes:/Users/bots/notes,work:/Users/bots/work-docs
PRECIS_PLAINTEXT_ROOTS=scratch:/tmp/scratch
PRECIS_PYCODE_ROOTS=precis:/Users/bots/.../precis-mcp-new,cluster:/Users/bots/.../openclaw-cluster
```

Format: `alias:path` pairs separated by commas. Empty list (or unset)
hides the kind. The handler is constructed once with all roots; it
dispatches by alias on every call.

### Why config-time, not runtime

The pycode spec had `put(mode='register', text='/path')` for
registering repos. Drop it. Roots are operator-level config, not
agent-level mutation. Benefits:

- Diffable in version control (config file or env file).
- Survives restarts deterministically.
- No "did we register that repo?" state-tracking confusion.
- Removes one handler mode (`register` / `unregister` go away).

### Default alias when only one root

When exactly one root is configured for a kind, the alias becomes
implicit. Two-or-more roots: alias is required (with a `BadInput`
options-hint when missing).

## Path-traversal safety

Same as phase 6a, generalized:

1. The id parser enforces the alias is in the configured set.
2. The relative path is concatenated to the root.
3. `Path(full).resolve().relative_to(root.resolve())` confirms no
   `..` escape. Any failure raises `BadInput`.
4. For Unix: the path must NOT contain `\0` (null byte).
5. Symlinks under the root are followed; symlinks pointing *outside*
   the root are rejected (caught by step 3).

This applies to read AND write. No exception for `put(mode='create')`.

## Migration from phase 6a

Phase 6a shipped less than a session ago, with no production users.
The migration is local-only:

1. Drop the `--` encoding in `precis/utils/md_parse.py`'s
   `file_slug_from_path`. Slugs become `notes/meeting`.
2. Audit `Store._validate_slug_for_kind` — confirm `/` passes for
   the `markdown` (and other file) kinds. The slug regex may need
   a per-kind override: file kinds allow `/` and `.`; ref kinds
   keep the strict `[a-z0-9_-]+` alphabet.
3. Update `MarkdownHandler._resolve_path` to no longer round-trip
   through `path_from_file_slug`.
4. Update tests (`test_md_parse.py::test_file_slug_from_path`,
   `test_markdown_handler.py::test_nested_dir_files`) to expect
   the new shape.
5. Wipe the local DB's `markdown` corpus (5 refs, none exported).
6. Update the `precis-markdown-help` skill.

Estimated cost: ~150 LOC churn, ~30 minutes including the schema
audit. No data migration required.

## What's NOT in this document

- The actual TOC tree-rendering code. That's per-kind, lives in each
  handler.
- The pycode indexer (AST passes, call resolution). See
  `docs/pycode-kind-spec.md`.
- The git integration (blame, log, churn). See `pycode-kind-spec.md`
  §"Git integration".
- The `book` kind's multi-file glue. Its `id` shape will follow this
  grammar; the *resolution* logic is the unique work.
- The `_FileHandlerBase` extraction. To be designed concretely when
  phase 6b lands the second R/W kind (`plaintext`).

## Open questions

1. **Should view names allow per-kind aliases?** e.g. `outline` is
   pycode's name for what markdown calls `toc`. Probably yes — keep
   per-kind primary names and let the parser accept the cross-kind
   alias as a hint with a "did you mean toc?" reply.

2. **Line-range output format on responses.** When I respond with
   `"resolved L42-58 to block 'agenda'"`, should the line range be
   echoed in the response body (so the agent sees how its query was
   interpreted)? Currently the markdown handler only includes the
   slug. Consider adding `(L42-58)` to the response title line.

3. **Symbol disambiguation.** When `~Registry` matches two classes
   in the same file (rare but possible with conditional imports),
   what's the right error shape? Probably `BadInput` with `options=`
   listing both qualnames.

4. **Block selector inheritance for inserted content.** When `replace`
   shrinks a block, the next block's slug doesn't change (content-
   derived). When `replace` *changes* the block's content, the slug
   changes (it's content-derived!) but the address used in the call
   referenced the *old* slug. The handler returns the *new* slug
   in the response. Document this loud — it's a footgun.

5. **`book` kind's position relative to `tex`.** A book is often a
   LaTeX project. Should `book` be a wrapper around `tex` with
   chapter glue, or a peer kind? Defer until tex lands.

## Implementation order

1. **Now (one commit)**: write this doc. Mark phase 6a's `--` slug
   encoding as deprecated in CHANGELOG.
2. **Next session**: markdown 6a → `/` slugs. Self-contained refactor,
   ~30 min, breaks no production data.
3. **Then 6b**: `plaintext` + `rmk` + extract `_FileHandlerBase`
   simultaneously, with the base shaped by the three concrete kinds.
4. **Then pycode**: implement against this spec from day 0. Drop
   `::` as primary separator (keep as kind-specific shortcut for
   qualname-only lookup when file is unknown).
5. **Then 6c**: `docx`, `tex`, `book`. Each is a session.
