# Unified addressing for file-rooted kinds

> Status: **design spec**, drafted after phase 6a (markdown) shipped
> and before phase 6b (plaintext, rmk) and `python` (Python codebase
> navigator) start. The goal is to lock down one address grammar
> that every kind backed by an on-disk file (or directory of files)
> agrees on, so the agent has a single mental model.
>
> **2026-05 amendment:** the per-kind multi-root proposal
> (`PRECIS_MARKDOWN_ROOTS`, `PRECIS_PLAINTEXT_ROOTS`, etc.) was
> dropped. Markdown / plaintext / tex now share **one** env var,
> `PRECIS_ROOT`, walked separately by extension. The "multi-root with
> alias" design lives only in `PRECIS_PYTHON_ROOTS` (where it makes
> semantic sense — code repos are inherently distinct workspaces).
> The address grammar (`~`, `~L`, `--` separators, etc.) is
> unchanged. Sections referring to `PRECIS_*_ROOTS` for prose-file
> kinds are obsolete.

## Why

By the end of phase 6 + python + git we will have at least eight
file-rooted kinds:

| Kind | Backing | R/W | TOC unit | Sub-file unit |
|---|---|---|---|---|
| `markdown` | `.md` files | yes | headings | block (paragraph/code/list/table) |
| `plaintext` | `.txt` files | yes | n/a | block (paragraph) |
| `rmk` | RMK note files | yes | front-matter + headings | block |
| `docx` | Word `.docx` | yes | headings | paragraph (para id) |
| `tex` | LaTeX `.tex` + project | yes | sections | block + label |
| `book` | multi-file project | yes | chapter graph | per-file delegated |
| `python` | `.py` repo | yes | class/method tree | symbol (qualname) or line range |
| `git` | any repo | read-only | branches/refs | commit/file/line |

Each was designed in isolation; their addressing schemes drifted:

- markdown encodes paths as `notes--meeting` (slug-safe `--`)
- python keeps paths as `precis/src/precis/registry.py` (with `/`)
- python uses `::` for symbols; markdown uses `~slug` for blocks
- markdown uses `~A..B` for ranges (paper-style); python spec uses `~A-B`
- markdown has one implicit root; python has many explicit ones

The agent has to remember three grammars to do the same thing across
three kinds. That's wasted context for no gain. This document picks
one grammar.

## The two-track model

Every file-rooted kind exposes the **same dual-track addressing**.
This is the central organising principle; everything else falls
out of it.

| Track | Form | What it is | Stable? |
|---|---|---|---|
| **A — coordinates** | `~L<start>-<end>` | line range (1-indexed, inclusive) | shifts under edits above |
| **B — headers** | `~<name>` | named region (block slug, symbol qualname, paragraph id) | yes |

Track A is what external tools speak: `grep -n`, test failures,
stack traces, IDE "go to line". Track B is what stays valid across
edits and across sessions. Both work as `<selector>` in any address.

The handler's job is to maintain the bijection between the two
tracks. **Every response carries both forms** in its header:

```
# notes/meeting.md~agenda  (block 3, lines 42-58)
```

The agent never has to compute the mapping. Whatever you put in,
you get the canonical name + line range out.

### Why two tracks (and not one)

A name-only system breaks when external tools hand you a line
number. A coordinate-only system breaks under any edit. The
two-track model accommodates both inputs and gives the agent a
durable handle (Track B) to use across calls.

For markdown the headers are content-derived block slugs. For
python they are qualnames — and python headers carry **graph
metadata** the other kinds don't have (parent, callers, callees,
inherits). See `precis-python-help` for the graph-aware navigation.

## Design principles (derived)

1. **Path format is `/`-native.** Pretrained models read `path/to/file.py`
   fluently; `path--to--file-py` is alien. Use the natural form.
2. **Track B beats Track A for storage.** Block slugs and qualnames
   survive edits; line numbers don't. Line numbers are accepted as
   *input* (pointers) but stable names are what the handler returns
   and stores.
3. **One grammar across kinds.** The same `id` shape works for any
   file-rooted kind; only the *meaning* of Track-B selectors changes.
4. **Single-root case stays terse.** When only one root is configured
   for a kind, its alias may be omitted from the id.
5. **Read and write share a selector vocabulary.** Whatever points to
   a region for `get` also points to the same region for `put`.
6. **The handler is a normaliser.** Accept multiple input forms
   (canonical, absolute, line range, stale slug); emit one canonical
   form.

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

# python
get(kind='python', id='precis/src/precis/registry.py')             # file overview
get(kind='python', id='precis/src/precis/registry.py~Registry')    # symbol (local)
get(kind='python', id='precis/src/precis/registry.py~Registry.get')# nested symbol
get(kind='python', id='precis/src/precis/registry.py~L42-100')     # line range
get(kind='python', id='precis/src/precis/registry.py/outline')     # outline view
get(kind='python', id='precis/src/precis/registry.py/callgraph')   # call graph
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

## Line-number convention

Line numbers in selectors and responses are **1-indexed and
inclusive on both ends**. This matches every user-facing Unix tool:

| Tool | Convention |
|---|---|
| `ed` / `vi` / `sed` (`1,5d`, `1,5p`) | 1-indexed inclusive |
| `head -n 5`, `grep -n` | 1-indexed |
| `git blame -L42,58` | 1-indexed inclusive |
| Python tracebacks (`File "x.py", line 42`) | 1-indexed |
| GitHub permalinks (`#L42-L58`) | 1-indexed inclusive |

So `~L42-58` means lines 42, 43, …, 58 — 17 lines total. `~L42`
means just line 42 (1 line). `~L42-42` is the same as `~L42`. An
empty range like `~L58-42` (end before start) is `BadInput`.

Rationale:

- This is what the agent reads everywhere else — stack traces,
  compiler errors, IDE jump-to-line, GitHub URLs are all 1-indexed.
- Half-open ranges (`[42, 58)` Python-slice style) are great for
  programmer APIs but unfamiliar in user-facing addressing.
- Inclusive ranges round-trip with copy-paste from blame / log /
  diff output.

Responses always quote both the input range and the resolved range
when they differ (e.g. when a line input gets snapped to a block
boundary):

```
# notes/meeting.md~L42-58  (resolved to block 'conclusion', lines 40-65)
```

So the agent sees what it asked for *and* the canonical address.

### Programmer-API mismatch (one wart, accepted)

Python's `ast.lineno` is 1-indexed but `ast.col_offset` is
0-indexed; LSP positions are 0-indexed; tree-sitter ranges are
0-indexed. We do the conversion at the boundary in each indexer
and never expose 0-indexed lines to the agent. Internal code
should use named fields (`start_line` / `end_line`) and never carry
raw integers around without a comment about which convention they
follow.

## Selector parsing

The `<selector>` field after `~` is a discriminated union, parsed in
priority order:

| # | Pattern | Meaning | Applies to |
|---|---|---|---|
| 1 | `^L\d+(-\d+)?$` | line range (1-indexed, inclusive) | all kinds |
| 2 | `^[a-z][a-z0-9_-]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$` | dotted symbol name | python (TODO: docx/tex labels) |
| 3 | `^\d+$` | block position (0-indexed) | markdown, plaintext, rmk |
| 4 | anything else | named slug | markdown (block slug), python (local symbol name), docx (paragraph id) |

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
  hint to use `L<n>` (python). Lines need the `L` prefix.
- A dotted name is **always** a symbol (python); markdown rejects
  with `BadInput` because block slugs don't contain dots.
- A name that could be a symbol *or* a slug (no dot, no leading L)
  is resolved per-kind: markdown → block slug; python → local
  symbol name (must be unique within the file, else
  `BadInput("ambiguous: matches X, Y; use full qualname")`).

### Multi-block line ranges

A line-range selector that spans multiple Track-B regions is valid:

- For **read** (`get`): the response contains every region that
  overlaps the range, each with its stable name.
- For **write** (`put` `mode='replace'` or `'delete'`): the entire
  spanning range is replaced/deleted as a single splice; the
  response lists every dropped slug:

  ```
  replaced lines 42-58 in notes/meeting.md
    (dropped 2 blocks: 'agenda', 'next-steps')
    (new block: 'updated-section')
  ```

  This is **intentional** — a coordinate-range edit is a coarse
  operation by nature. If you want surgical block-level edits,
  address by Track B (slug or qualname) instead.

### Absolute paths as input

The id parser accepts an absolute filesystem path and normalises
it against configured roots:

```python
get(kind='markdown', id='/Users/bots/notes/meeting.md')
# → normalised to canonical 'notes/meeting.md' (alias 'notes')
```

If the path is outside every configured root, `BadInput`. This is
free tool-chain integration: agent runs `find` / `grep` / IDE,
pastes the absolute path straight in.

### Stale-slug recovery

When `replace` changes a block's content, its content-derived slug
changes. The handler stores the old slug as an alias in
`block.meta.previous_slug` until the next replace of that block.
A query for the stale slug returns the current content with a hint:

```
get(kind='markdown', id='notes/meeting.md~old-slug')
→ Block 'old-slug' was renamed to 'new-slug' after a replace edit.
  # notes/meeting.md~new-slug  (block 5, lines 42-58)
  ...
```

If the stale slug isn't in any rename map, `NotFound` with
`options=` listing the five nearest slugs by edit distance.

## Views

Views are the *render mode* for the resolved selector. Per kind:

### Shared across all file kinds

- `raw` — the full source text of the file (or, with a selector, the
  text of the resolved region only).
- `source` — alias for `raw`. Python prefers `source`; markdown
  prefers `raw`. Both work everywhere.

### Markdown / plaintext / rmk / docx / tex / book

- (default) — file overview: header + heading TOC preview + `Next:`
  trailer.
- `toc` — full hierarchical TOC.
- `raw` — full source.

### Python

- (default) — file overview: imports + class/function tree.
- `outline` — same as default but verbose (signatures + docstring
  heads + decorators + raises).
- `source` — full source for the resolved region.
- `callgraph` — entry-rooted static call tree (requires `entry=`).
- `runtrace` — dynamic call trace (gated by `PRECIS_PYTHON_ALLOW_EXEC=1`).
- `imports` — flat dependency map.
- `symbols` — flat symbol list (paginated).
- `blame`, `log`, `churn`, `owners`, `diff` — git overlays.

## Response header format

Every selector-bearing response opens with a one-line header that
carries **both Track A and Track B forms** of the resolved address:

```
# notes/meeting.md~agenda  (block 3, lines 42-58)
# precis-mcp::precis.registry.Registry.get  (lines 120-128)
# precis-mcp/src/precis/cli.py~L142  (resolved to _cmd_serve, lines 138-150)
```

Format rules:

- **Path or qualname** comes first — whichever is the canonical form
  for that kind. (Python prefers qualname for symbols; file-path
  for files.)
- **Coordinates** in parentheses afterwards — block pos for
  markdown/plaintext, line range for everything.
- **"Resolved to" hint** when the input form differs from the
  canonical (line-range input → named block; absolute path →
  canonical alias; stale slug → current slug).

The agent's next call can use either form. No round-trip needed.

### Python canonical-response choice

Python has two equivalent address forms for symbols:
`repo/path/to/file.py~Symbol.method` and `repo::pkg.mod.Symbol.method`.
The handler **emits the qualname form** in responses (shorter,
file-move-resistant, idiomatic Python). It **accepts both forms**
as input.

## TOC row format (per-symbol/per-block)

The unified TOC row carries four fields. Only fields that apply to a
kind appear:

| Field | Markdown | Python |
|---|---|---|
| **Stable name** | block slug | symbol qualname |
| **Position** | line number (`L42`) | line number |
| **Headline** | heading text (for headings); first 80 chars (for paragraphs) | signature `def f(x: int) -> str` |
| **Summary** | n/a | first line of docstring, ≤80 chars |
| **Edges** (python only) | n/a | `calls:`, `called by:`, `inherits:`, `raises:` |

Python example:

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
docx, tex, book, python) declare `supports_put=True` and accept four
modes:

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

### Atomic and immediate

When `put` returns successfully, the new bytes are on disk. There
is no in-memory buffer, no deferred batch, no async settle. The
sequence per write:

1. Splice replacement text into the file buffer.
2. Run all kind-specific gates (AST + format for python; block
   parse for markdown; etc.). Failure → revert, return `BadInput`.
3. Write to `<file>.tmp.<pid>.<rand>` in the same directory as the
   target (required for `os.replace` to be atomic on the same
   filesystem).
4. `fsync(tmp)` — force buffered writes to physical storage.
5. `os.replace(tmp, target)` — atomic rename on POSIX; single inode
   swap.
6. `fsync(directory)` — ensures the directory entry change itself
   is durable across power loss.
7. Force re-ingest under a write lock so a concurrent `get` waits.
8. Return.

Concurrent readers see exactly the old content or exactly the new
content, never a partial mix. On crash mid-write the tmpfile may
linger (a startup pass cleans up `*.tmp.*` orphans); the target is
always intact.

### Stable name in response

After every write the response includes the stable name of what was
edited:

```
replaced block 'conclusion' (lines 42-58) in notes/meeting.md
```

So an agent that started with a line range knows the durable address
for follow-up edits.

### Python writes

Python is **R/W in v1**. The unified base provides splice + atomic
write + re-ingest; python adds three policy gates on top:

- **AST validation.** `ast.parse(new_content)` must succeed.
  Mandatory invariant. A syntax-broken file would break every
  import.
- **No-qualname-drop.** Any qualname that lived inside the
  addressed region before the edit must still live in the file
  after. Catches accidental rename (`Class.method_a` →
  `Class.method_b`) and accidental drop (replace whole `Class` and
  forget to keep its other methods). Override with
  `allow_rename=True` when the rename or drop is intentional.
- **Ruff (fix + format).** Always runs. Two subprocess calls per
  write: `ruff check --fix --exit-zero --stdin-filename <path>`
  applies safe autofixes (unused imports, sorted `__all__`,
  `is None` over `== None`, etc.); `ruff format --stdin-filename
  <path>` normalises layout. Both walk up for
  `pyproject.toml` / `ruff.toml` so writes follow the project's
  pinned style. ~70ms total. If ruff modified the buffer, the
  response includes a one-line summary of what it changed (so the
  agent learns rather than being silently fixed). If ruff is
  missing or errors, the write proceeds with a warning header.
  Ruff never blocks a write.

See `python-kind-spec.md § Write surface` for the pipeline order
and response shape, and `precis-python-help § Editing code` for the
recipes.

## Multi-root configuration

Replace single-root env vars with dict-shaped ones:

```env
# Old (phase 6a)
PRECIS_MARKDOWN_ROOT=/Users/bots/notes

# New (unified)
PRECIS_MARKDOWN_ROOTS=notes:/Users/bots/notes,work:/Users/bots/work-docs
PRECIS_PLAINTEXT_ROOTS=scratch:/tmp/scratch
PRECIS_PYTHON_ROOTS=precis-mcp:/Users/bots/.../precis-mcp,cluster:/Users/bots/.../openclaw-cluster
```

### Alias hygiene

Aliases are operator-chosen. Pick names that **don't collide with
the first directory under the root**, otherwise the canonical
addresses get an awkward stutter:

```
✗  precis/src/precis/cli.py        # alias=precis, src/precis is the package dir
✓  precis-mcp/src/precis/cli.py # alias=precis-mcp (the repo name)
```

For python, **use the repo name** (the directory name) as the alias.
For markdown / plaintext, any descriptive label is fine.

Format: `alias:path` pairs separated by commas. Empty list (or unset)
hides the kind. The handler is constructed once with all roots; it
dispatches by alias on every call.

### Why config-time, not runtime

The python spec had `put(mode='register', text='/path')` for
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
- The python indexer (AST passes, call resolution). See
  `docs/python-kind-spec.md`.
- The git integration (blame, log, churn). See `python-kind-spec.md`
  §"Git integration".
- The `book` kind's multi-file glue. Its `id` shape will follow this
  grammar; the *resolution* logic is the unique work.
- The `_FileHandlerBase` extraction. To be designed concretely when
  phase 6b lands the second R/W kind (`plaintext`).

## Open questions

1. **Should view names allow per-kind aliases?** e.g. `outline` is
   python's name for what markdown calls `toc`. Probably yes — keep
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
4. **Then python**: implement against this spec from day 0. Drop
   `::` as primary separator (keep as kind-specific shortcut for
   qualname-only lookup when file is unknown).
5. **Then 6c**: `docx`, `tex`, `book`. Each is a session.
